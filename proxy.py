#!/usr/bin/env python3
"""
aproxy — Anthropic-compatible reverse proxy for Ollama.
Provides token authentication and usage audit while passing
requests through to Ollama's native /v1/messages endpoint.

Listens on port 4001. Authenticated users are validated against
a static key file (keys.json).

Architecture:
  Claude Code -> :4001 (aproxy) -> :11434 (Ollama)

Claude Code sends auth via:
  1. x-api-key header
  2. Authorization: Bearer header (ANTHROPIC_AUTH_TOKEN)

When both are present, x-api-key takes priority for user identification.
All tokens are validated against keys.json -- no bypass or fallback.
"""

import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# --- Configuration ---
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
PORT = int(os.environ.get("ANTHROPIC_PROXY_PORT", "4001"))

# Max request body size in bytes. Requests larger than this are rejected before
# the body is read, protecting the proxy from memory pressure and oversized
# audit log writes. Default: 50 MiB.
MAX_BODY_SIZE = int(os.environ.get("APROXY_MAX_BODY_SIZE", str(50 * 1024 * 1024)))

# --- Key store ---
# Supports two formats in keys.json:
#   Legacy (plaintext):  {"sk-xxx": "user1", "sk-yyy": "user2"}
#   Hashed:             {"_salt": "hex...", "users": {"sha256$hex...": "user1", ...}}
# Hashed format stores SHA-256(salt + token) so plaintext tokens never touch disk.
# The validate_key function tries both formats for backward compatibility.

_keys_file = os.environ.get("API_KEYS_FILE", "/home/sergey/Projects/aproxy/keys.json")
SALT_LEN = 32  # bytes


def _hash_token(salt: str, token: str) -> str:
    """Compute SHA-256(salt_hex + token)."""
    return hashlib.sha256((salt + token).encode()).hexdigest()


def _load_keys() -> tuple[dict, dict, str | None]:
    """Load key file. Returns (plain_keys, hashed_users, salt).

    plain_keys:  {token: username} for legacy format
    hashed_users: {hash_hex: username} for hashed format
    salt: hex string for hashed format (None for legacy-only files)
    """
    if not os.path.exists(_keys_file):
        return {}, {}, None
    with open(_keys_file) as f:
        data = json.load(f)

    # Legacy format: flat dict of token -> username
    if "_salt" not in data:
        return data, {}, None

    salt = data["_salt"]
    users = {k: v for k, v in data["users"].items() if k.startswith("sha256$")}
    return {}, users, salt


class _KeyStore:
    """In-memory key cache with lazy reload on keys.json modification.

    Reloading is cheap enough that it runs once per request, but mtime is only
    checked every KEY_RELOAD_INTERVAL seconds to avoid stat storms.
    """

    def __init__(self):
        self.api_keys: dict = {}
        self.hashed_users: dict = {}
        self.salt: str | None = None
        self._mtime: float = 0.0
        self._last_check: float = 0.0
        self._load()

    def _load(self):
        try:
            self.api_keys, self.hashed_users, self.salt = _load_keys()
            self._mtime = os.path.getmtime(_keys_file)
            log.info(f"Loaded {self.key_count()} key(s) from {_keys_file}")
        except Exception as e:
            log.error(f"Failed to load keys from {_keys_file}: {e}")
            # Keep previous keys on error; fail-closed would lock everyone out.

    def key_count(self) -> int:
        return len(self.api_keys) + len(self.hashed_users)

    def reload_if_changed(self):
        now = time.time()
        if now - self._last_check < KEY_RELOAD_INTERVAL:
            return
        self._last_check = now
        try:
            current_mtime = os.path.getmtime(_keys_file)
        except FileNotFoundError:
            if self.key_count() > 0:
                log.warning(f"Key file {_keys_file} disappeared; keeping cached keys")
            return
        except Exception as e:
            log.error(f"Cannot stat key file: {e}")
            return

        if current_mtime != self._mtime:
            log.info(f"Key file changed (mtime {self._mtime} -> {current_mtime}), reloading")
            self._load()


# Minimum seconds between mtime checks for keys.json.
KEY_RELOAD_INTERVAL = float(os.environ.get("APROXY_KEY_RELOAD_INTERVAL", "1.0"))


def validate_key(api_key: str | None) -> str:
    """Validate authentication token. Returns username.
    Tries legacy plaintext lookup first, then hashed lookup.
    Reloads keys.json if it has been modified since the last check."""
    if not api_key:
        raise HTTPException(status_code=401, detail={"type": "authentication_error", "message": "Authentication required. Set ANTHROPIC_AUTH_TOKEN or provide a valid key."})

    _key_store.reload_if_changed()

    # Legacy: direct token lookup
    if api_key in _key_store.api_keys:
        return _key_store.api_keys[api_key]

    # Hashed: compute SHA-256(salt + token) and look up
    if _key_store.salt:
        token_hash = "sha256$" + _hash_token(_key_store.salt, api_key)
        if token_hash in _key_store.hashed_users:
            return _key_store.hashed_users[token_hash]

    raise HTTPException(status_code=401, detail={"type": "authentication_error", "message": "Invalid authentication token. Check your ANTHROPIC_AUTH_TOKEN or key value."})

# Audit log
AUDIT_LOG = os.environ.get("AUDIT_LOG", "/var/log/aproxy/audit.jsonl")
AUDIT_ENABLED = bool(AUDIT_LOG)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aproxy")

# File logging (in addition to journald via StandardOutput/StandardError)
PROXY_LOG = os.environ.get("PROXY_LOG")
if PROXY_LOG:
    _fh = logging.FileHandler(PROXY_LOG)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(_fh)

# Initialise the key store now that the logger is configured.
_key_store = _KeyStore()

# --- Metrics (Prometheus) ---
REQUEST_COUNT = Counter(
    "aproxy_requests_total",
    "Total proxied requests",
    ["user", "method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "aproxy_request_duration_seconds",
    "Request latency in seconds",
    ["user", "method", "path"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)
TOKENS_IN = Counter(
    "aproxy_tokens_input_total",
    "Total input tokens proxied",
    ["user", "model"],
)
TOKENS_OUT = Counter(
    "aproxy_tokens_output_total",
    "Total output tokens proxied",
    ["user", "model"],
)
ACTIVE_CONNECTIONS = Gauge(
    "aproxy_active_connections",
    "Currently active proxied connections",
)

# --- App ---

client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage httpx client lifecycle across startup/shutdown."""
    global client
    client = httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=httpx.Timeout(300.0))
    log.info(f"httpx client ready for Ollama at {OLLAMA_BASE}")
    try:
        yield
    finally:
        await client.aclose()
        log.info("httpx client closed")


app = FastAPI(title="aproxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def make_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Return Anthropic-compatible error response."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def error_body_to_content(error_body: bytes, content_type: str) -> dict:
    """Convert upstream error body into a safe JSON-compatible response body."""
    if content_type.startswith("application/json"):
        try:
            return json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            pass
    text = error_body.decode("utf-8", errors="replace")
    return {"type": "error", "error": {"type": "api_error", "message": text[:500]}}


def audit(user_key: str, method: str, path: str, model: str | None = None,
          status: int | None = None, tokens: dict | None = None, error: str | None = None):
    """Write audit record and update Prometheus metrics."""
    if tokens and model:
        inp = tokens.get("input_tokens", 0)
        out = tokens.get("output_tokens", 0)
        if inp:
            TOKENS_IN.labels(user=user_key, model=model).inc(inp)
        if out:
            TOKENS_OUT.labels(user=user_key, model=model).inc(out)
    if not AUDIT_ENABLED:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "key": user_key[:8] + "..." if len(user_key) > 8 else user_key,
        "method": method,
        "path": path,
    }
    if model:
        record["model"] = model
    if status:
        record["status"] = status
    if tokens:
        record["tokens"] = tokens
    if error:
        record["error"] = error
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"Audit write failed: {e}")


def extract_api_key(x_api_key: str | None, authorization: str | None) -> str | None:
    """Extract API key from x-api-key or Authorization header.
    
    Priority: x-api-key > Authorization Bearer.
    When both are present, x-api-key takes precedence.
    """
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        if authorization.startswith("Bearer "):
            return authorization[7:].strip()
        if authorization.startswith("Basic "):
            return authorization[6:].strip()
    return None


async def validate_key_async(api_key: str | None, request: Request | None = None) -> str:
    """Validate token and store user in request.state for metrics."""
    user = validate_key(api_key)
    if request is not None:
        request.state.user = user
    return user


def _body_too_large(request: Request) -> tuple[bool, int | None]:
    """Check Content-Length against MAX_BODY_SIZE. Returns (rejected, limit)."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        # Reject chunked or otherwise unbounded bodies to avoid unbounded reads.
        return True, MAX_BODY_SIZE
    try:
        length = int(content_length)
    except ValueError:
        return True, MAX_BODY_SIZE
    if length > MAX_BODY_SIZE:
        return True, MAX_BODY_SIZE
    return False, None


@app.middleware("http")
async def add_cors_and_timing(request: Request, call_next):
    # Paths that are skipped from proxied-request metrics
    _SKIP_METRICS = {"/health", "/metrics"}
    path = request.url.path

    # Reject oversized or unbounded request bodies before reading them.
    if request.method in {"POST", "PUT", "PATCH"}:
        rejected, limit = _body_too_large(request)
        if rejected:
            log.warning(f"Request body too large or unbounded: {path} (limit={limit})")
            return JSONResponse(
                status_code=413,
                content={
                    "type": "error",
                    "error": {
                        "type": "request_too_large",
                        "message": f"Request body exceeds maximum allowed size of {limit} bytes.",
                    },
                },
            )

    if path in _SKIP_METRICS:
        start = time.time()
        response = await call_next(request)
        elapsed = time.time() - start
        response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    start = time.time()
    ACTIVE_CONNECTIONS.inc()
    try:
        response = await call_next(request)
    except Exception:
        ACTIVE_CONNECTIONS.dec()
        raise
    elapsed = time.time() - start

    # Detect SSE by content-type header. Starlette's internal _StreamingResponse
    # may have media_type=None; the real type is only in response headers.
    ct = response.headers.get("content-type", "")
    is_streaming = ct.startswith("text/event-stream")

    if is_streaming:
        # For SSE, metrics and gauge are managed by the stream generator:
        # wrap the body iterator so that ACTIVE_CONNECTIONS is decremented
        # only after the client finishes consuming the stream, and request
        # metrics (count, latency) are recorded at that point too.
        #
        # Use upstream status code from request.state.stream_status (set by
        # _stream_response) rather than the outer StreamingResponse status
        # which is always 200 regardless of upstream errors.
        original_body = response.body_iterator

        async def wrapped_body():
            try:
                async for chunk in original_body:
                    yield chunk
            finally:
                # Read upstream status from request.state — set by _stream_response's
                # generate() by the time we reach here (stream is fully consumed).
                upstream_status = str(getattr(request.state, "stream_status", 500))
                ACTIVE_CONNECTIONS.dec()
                stream_elapsed = time.time() - start
                user = getattr(request.state, "user", "anonymous")
                normalized = _normalize_path(path)
                REQUEST_COUNT.labels(user=user, method=request.method, path=normalized, status_code=upstream_status).inc()
                REQUEST_LATENCY.labels(user=user, method=request.method, path=normalized).observe(stream_elapsed)

        response.body_iterator = wrapped_body()
    else:
        ACTIVE_CONNECTIONS.dec()
        user = getattr(request.state, "user", "anonymous")
        normalized = _normalize_path(path)
        status_code = str(response.status_code)
        REQUEST_COUNT.labels(user=user, method=request.method, path=normalized, status_code=status_code).inc()
        REQUEST_LATENCY.labels(user=user, method=request.method, path=normalized).observe(elapsed)

    response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


# Known path buckets for Prometheus labels (bounded cardinality)
_KNOWN_PATHS = {
    "/v1/messages", "/v1/models", "/v1/organizations",
    "/v1/messages/batches",
}

def _normalize_path(path: str) -> str:
    """Normalize URL path to a fixed set of buckets for metrics labels."""
    if path in _KNOWN_PATHS:
        return path
    if path.startswith("/v1/"):
        return "/v1/other"
    return "/other"


@app.exception_handler(HTTPException)
async def anthropic_error_handler(request: Request, exc: HTTPException):
    """Convert HTTPException to Anthropic-compatible error format."""
    if isinstance(exc.detail, dict) and "type" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content={"type": "error", "error": exc.detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={"type": "error", "error": {"type": "api_error", "message": str(exc.detail)}},
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions."""
    log.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"type": "error", "error": {"type": "api_error", "message": "Internal server error"}},
    )


# --- Endpoints ---

@app.get("/metrics")
async def metrics(request: Request, x_api_key: str | None = Header(None),
                  authorization: str | None = Header(None)):
    """Prometheus metrics endpoint (requires authentication)."""
    api_key = extract_api_key(x_api_key, authorization)
    try:
        user = validate_key(api_key)
    except HTTPException:
        return PlainTextResponse(
            "Unauthorized\n",
            status_code=401,
            media_type="text/plain",
            headers={"WWW-Authenticate": 'Bearer realm="aproxy"'},
        )
    log.info(f"[{user}] GET /metrics")
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    try:
        r = await client.get("/api/version")
        ollama_version = r.json().get("version", "unknown") if r.status_code == 200 else "unreachable"
    except Exception:
        ollama_version = "unreachable"
    return {"status": "ok", "ollama": {"version": ollama_version}, "proxy": "aproxy/1.6"}


@app.get("/v1/models")
async def list_models(request: Request, x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)
    audit(user, "GET", "/v1/models")

    try:
        r = await client.get("/v1/models")
        return r.json()
    except Exception as e:
        log.error(f"Ollama /v1/models error: {e}")
        return JSONResponse(status_code=502, content={"type": "error", "error": {"type": "api_error", "message": str(e)}})


@app.post("/v1/messages")
async def messages(request: Request, x_api_key: str | None = Header(None),
                   authorization: str | None = Header(None)):
    """Main Anthropic Messages API endpoint - proxied to Ollama."""
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)

    body = await request.body()
    body_json = json.loads(body) if body else {}

    model = body_json.get("model", "unknown")
    stream = body_json.get("stream", False)

    # Build headers for Ollama
    # Always send Authorization: Bearer ollama so Ollama accepts the request
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer ollama",
    }

    # Forward anthropic-version header
    av = request.headers.get("anthropic-version")
    if av:
        headers["anthropic-version"] = av

    # Forward anthropic-beta header
    ab = request.headers.get("anthropic-beta")
    if ab:
        headers["anthropic-beta"] = ab

    log.info(f"[{user}] POST /v1/messages model={model} stream={stream}")

    try:
        if stream:
            return await _stream_response(request, client, user, model, body, headers)
        else:
            r = await client.post("/v1/messages", content=body, headers=headers)

            resp_body = r.json() if r.status_code == 200 else None
            tokens = None
            if resp_body and "usage" in resp_body:
                tokens = resp_body["usage"]

            audit(user, "POST", "/v1/messages", model=model, status=r.status_code, tokens=tokens)

            if r.status_code != 200:
                log.warning(f"[{user}] Ollama error {r.status_code}: {r.text[:500]}")
                audit(user, "POST", "/v1/messages", model=model, status=r.status_code, error=r.text[:200])

            return JSONResponse(
                status_code=r.status_code,
                content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text},
            )
    except httpx.ReadTimeout:
        log.error(f"[{user}] Ollama timeout")
        audit(user, "POST", "/v1/messages", model=model, status=504, error="timeout")
        return make_error(504, "timeout_error", "Ollama request timed out")
    except Exception as e:
        log.error(f"[{user}] Proxy error: {e}", exc_info=True)
        audit(user, "POST", "/v1/messages", model=model, status=500, error=str(e))
        return make_error(500, "api_error", f"Proxy error: {e}")


async def _stream_response(request: Request, http_client, user: str, model: str, body: bytes, headers: dict):
    """Stream response from Ollama back to client.

    We open the upstream stream first and inspect the status code before
    committing to a StreamingResponse. This guarantees that an upstream error
    is returned to the client with the matching HTTP status code, instead of
    wrapping the error body in a misleading HTTP 200 StreamingResponse.
    """
    request.state.stream_status = 500

    try:
        async with http_client.stream("POST", "/v1/messages", content=body, headers=headers) as resp:
            request.state.stream_status = resp.status_code

            if resp.status_code != 200:
                error_body = await resp.aread()
                error_text = error_body.decode("utf-8", errors="replace")
                audit(user, "POST", "/v1/messages", model=model, status=resp.status_code, error=error_text[:200])
                return JSONResponse(
                    status_code=resp.status_code,
                    content=error_body_to_content(error_body, resp.headers.get("content-type", "")),
                )

            total_tokens = {}

            async def generate():
                try:
                    async for line in resp.aiter_lines():
                        # Preserve upstream SSE framing. Ollama's Anthropic-compatible
                        # stream emits multi-line events such as:
                        #   event: message_start
                        #   data: {...}
                        #
                        # Emitting "\n\n" after every line turns the event line into
                        # a standalone empty event, which Claude Code then tries to
                        # parse as JSON. Add one newline per upstream line and let
                        # upstream blank lines terminate each SSE event.
                        yield line + "\n"
                        # Try to extract token counts from stream data events
                        if line.startswith("data: ") and ('"type":"message_delta"' in line or '"type": "message_delta"' in line):
                            try:
                                event = json.loads(line.removeprefix("data: ").strip())
                                if "usage" in event:
                                    total_tokens.update(event["usage"])
                            except (json.JSONDecodeError, KeyError):
                                pass
                finally:
                    audit(user, "POST", "/v1/messages", model=model, status=200, tokens=total_tokens if total_tokens else None)

            return StreamingResponse(generate(), media_type="text/event-stream")

    except httpx.ReadTimeout:
        log.error(f"[{user}] Ollama stream timeout")
        request.state.stream_status = 504
        audit(user, "POST", "/v1/messages", model=model, status=504, error="stream timeout")
        return make_error(504, "timeout_error", "Ollama stream timed out")
    except Exception as e:
        log.error(f"[{user}] Stream error: {e}", exc_info=True)
        request.state.stream_status = 500
        audit(user, "POST", "/v1/messages", model=model, status=500, error=str(e))
        return make_error(500, "api_error", f"Stream error: {e}")


@app.post("/v1/messages/batches")
async def messages_batches(request: Request, x_api_key: str | None = Header(None),
                            authorization: str | None = Header(None)):
    """Message batches - return not supported."""
    api_key = extract_api_key(x_api_key, authorization)
    await validate_key_async(api_key, request)
    return make_error(404, "not_found", "Message batches are not supported by this proxy")


# --- Organization/User info endpoints that Claude Code expects ---

@app.api_route("/v1/organizations", methods=["GET"])
async def organizations(request: Request, x_api_key: str | None = Header(None),
                         authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)
    audit(user, "GET", "/v1/organizations")
    return {"data": [], "has_more": False}


@app.api_route("/v1/organizations/{org_id}/users", methods=["GET"])
async def org_users(org_id: str, request: Request, x_api_key: str | None = Header(None),
                    authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)
    audit(user, "GET", f"/v1/organizations/{org_id}/users")
    return {"data": [], "has_more": False}


# --- Catch-all: proxy unrecognised paths to Ollama with auth ---


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str, x_api_key: str | None = Header(None),
                     authorization: str | None = Header(None)):
    """Proxy any unmatched paths to Ollama with authentication."""
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)
    audit(user, request.method, f"/{path}")

    # Build headers for Ollama
    headers = {"Authorization": "Bearer ollama"}
    for h in ["anthropic-version", "anthropic-beta", "content-type"]:
        v = request.headers.get(h)
        if v:
            headers[h] = v

    try:
        r = await client.request(request.method, f"/{path}", content=await request.body(), headers=headers)
        return JSONResponse(status_code=r.status_code, content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"data": r.text})
    except Exception as e:
        log.error(f"[{user}] Catch-all proxy error for /{path}: {e}")
        return make_error(502, "api_error", f"Ollama proxy error: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "keys":
        # --- CLI key management ---
        _cmd = sys.argv[2] if len(sys.argv) > 2 else ""

        def _read_keyfile():
            if os.path.exists(_keys_file):
                with open(_keys_file) as f:
                    return json.load(f)
            return {}

        def _write_keyfile(data):
            """Write keys.json atomically with restrictive permissions.

            Uses a temporary file in the same directory and os.replace() so a
            crash or disk-full event never leaves keys.json empty or partial.
            """
            key_dir = os.path.dirname(os.path.abspath(_keys_file))
            os.makedirs(key_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=key_dir, prefix=".keys.json.tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, _keys_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
                raise

        if _cmd == "add":
            if len(sys.argv) < 4:
                print("Usage: proxy.py keys add <username> [token]")
                print("  If token is omitted, a random one is generated.")
                sys.exit(1)
            username = sys.argv[3]
            token = sys.argv[4] if len(sys.argv) > 4 else f"sk-{secrets.token_urlsafe(32)}"
            data = _read_keyfile()

            # If file is in legacy format, migrate first
            if "_salt" not in data:
                salt = secrets.token_hex(SALT_LEN)
                new_users = {}
                for plain_token, user in data.items():
                    new_users["sha256$" + _hash_token(salt, plain_token)] = user
                data = {"_salt": salt, "users": new_users}

            data["users"]["sha256$" + _hash_token(data["_salt"], token)] = username
            _write_keyfile(data)
            print(f"Added user '{username}' with token '{token}'")
            print(f"Token hash: sha256${_hash_token(data['_salt'], token)[:16]}...")
            print("Keys will be picked up automatically by the running service.")

        elif _cmd == "migrate":
            data = _read_keyfile()
            if "_salt" in data:
                print("keys.json is already in hashed format. Nothing to do.")
                sys.exit(0)
            if not data:
                print("keys.json is empty. Nothing to migrate.")
                sys.exit(0)
            salt = secrets.token_hex(SALT_LEN)
            new_users = {}
            for plain_token, user in data.items():
                h = "sha256$" + _hash_token(salt, plain_token)
                new_users[h] = user
            new_data = {"_salt": salt, "users": new_users}
            _write_keyfile(new_data)
            print(f"Migrated {len(new_users)} token(s) to hashed format.")
            print("Plaintext tokens have been replaced with SHA-256 hashes.")
            print("Keep a backup of the old tokens if needed — they cannot be recovered from hashes.")
            print("Keys will be picked up automatically by the running service.")

        elif _cmd == "list":
            data = _read_keyfile()
            if "_salt" in data:
                print("Format: hashed (SHA-256 with salt)")
                print(f"Salt: {data['_salt'][:16]}...")
                for h, user in data.get("users", {}).items():
                    print(f"  {h[:24]}...  ->  {user}")
            else:
                print("Format: legacy (plaintext tokens)")
                for token, user in data.items():
                    print(f"  {token[:8]}...  ->  {user}")
            print(f"Total: {len(data.get('users', data))} key(s)")

        elif _cmd == "remove":
            if len(sys.argv) < 4:
                print("Usage: proxy.py keys remove <username>")
                sys.exit(1)
            username = sys.argv[3]
            data = _read_keyfile()
            if "_salt" in data:
                to_remove = [h for h, u in data["users"].items() if u == username]
                if not to_remove:
                    print(f"User '{username}' not found.")
                    sys.exit(1)
                for h in to_remove:
                    del data["users"][h]
                _write_keyfile(data)
                print(f"Removed {len(to_remove)} key(s) for user '{username}'.")
            else:
                to_remove = [t for t, u in data.items() if u == username]
                if not to_remove:
                    print(f"User '{username}' not found.")
                    sys.exit(1)
                for t in to_remove:
                    del data[t]
                _write_keyfile(data)
                print(f"Removed {len(to_remove)} key(s) for user '{username}'.")
            print("Keys will be picked up automatically by the running service.")

        else:
            print("Usage: proxy.py keys <command> [args]")
            print()
            print("Commands:")
            print("  add <username> [token]   Add a user (generate token if omitted)")
            print("  migrate                  Convert plaintext keys.json to hashed format")
            print("  list                     List known keys (hashes only)")
            print("  remove <username>        Remove a user's keys")
            sys.exit(1)
    else:
        log.info(f"Starting aproxy on :{PORT} -> {OLLAMA_BASE}")
        uvicorn.run(app, host="0.0.0.0", port=PORT)
