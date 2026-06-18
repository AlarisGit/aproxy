#!/usr/bin/env python3
"""
aproxy — authenticated reverse proxy for Ollama.
Provides token authentication and usage audit while passing
Anthropic-compatible and allowlisted native Ollama API requests upstream.

Listens on port 4001. Authenticated users are validated against
a static key file (keys.json).

Architecture:
  Claude Code / Ollama clients -> :4001 (aproxy) -> :11434 (Ollama)

Claude Code sends auth via:
  1. x-api-key header
  2. Authorization: Bearer header (ANTHROPIC_AUTH_TOKEN)

When both are present, x-api-key takes priority for user identification.
All tokens are validated against keys.json -- no bypass or fallback.
"""

import base64
import binascii
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
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse, Response
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# --- Server configuration ---
# All server settings live in a single JSON config file (default aproxy.json).
# Users remain in keys.json. Model mapping remains in models.json and is the only
# part of the configuration that is hot-reloaded without a service restart.
# Changes to port, paths, Ollama URL, etc. require a service restart.

_CONFIG_FILE = os.environ.get("APROXY_CONFIG", "/home/sergey/Projects/aproxy/aproxy.json")


def _load_config(path: str) -> dict:
    """Load server configuration from JSON file with fallback defaults."""
    defaults = {
        "ollama_base_url": "http://127.0.0.1:11434",
        "port": 4001,
        "keys_file": "/home/sergey/Projects/aproxy/keys.json",
        "models_file": "/home/sergey/Projects/aproxy/models.json",
        "audit_log": "/var/log/aproxy/audit.jsonl",
        "proxy_log": "/var/log/aproxy/proxy.log",
        "max_body_size": 50 * 1024 * 1024,
        "key_reload_interval": 1.0,
    }
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"aproxy: config file {path} not found, using defaults", file=sys.stderr)
        return defaults
    except Exception as e:
        print(f"aproxy: failed to load config {path}: {e}, using defaults", file=sys.stderr)
        return defaults

    # Merge with defaults so missing keys do not break the server.
    merged = {**defaults, **data}
    return merged


CONFIG = _load_config(_CONFIG_FILE)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aproxy")

# File logging (in addition to journald via StandardOutput/StandardError)
if CONFIG.get("proxy_log"):
    _fh = logging.FileHandler(CONFIG["proxy_log"])
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(_fh)

# --- Anthropic -> Ollama model mapping ---
# Claude Code ships with hard-coded Anthropic model IDs (claude-opus-*,
# claude-sonnet-*, claude-haiku-*). When running against Ollama those names do
# not exist, so aproxy translates them to local model names. The mapping lives
# in a dedicated models.json file which is reloaded without a service restart.
#
# models.json schema:
# {
#   "default": "kimi-k2.7-code:cloud",
#   "anthropic_mapping": {
#     "opus": "kimi-k2.7-code:cloud",
#     "sonnet": "kimi-k2.5:cloud",
#     "haiku": "devstral-small-2:24b-cloud"
#   }
# }
#
# Resolution rules:
# - If the requested model is not a claude-* ID, it is forwarded unchanged.
# - The tier (opus/sonnet/haiku) is looked up in anthropic_mapping.
# - If the mapped model is missing or does not exist in Ollama, the default
#   model is used; if the default is also invalid, the first available Ollama
#   model is used. A warning is logged whenever a fallback happens.

_models_file = CONFIG["models_file"]


class _ModelMapper:
    """Hot-reloadable Anthropic -> Ollama model mapping."""

    def __init__(self):
        self.default: str = ""
        self.mapping: dict[str, str] = {}
        self._mtime: float = 0.0
        self._last_check: float = 0.0
        self._load()

    def _load(self):
        try:
            with open(_models_file) as f:
                data = json.load(f)
            self.default = data.get("default", "")
            self.mapping = data.get("anthropic_mapping", {})
            self._mtime = os.path.getmtime(_models_file)
            log.info(f"Loaded model mapping from {_models_file}: default={self.default!r}, tiers={list(self.mapping)}")
        except FileNotFoundError:
            log.warning(f"Model mapping file {_models_file} not found; model translation disabled")
            self.default = ""
            self.mapping = {}
            self._mtime = 0.0
        except Exception as e:
            log.error(f"Failed to load model mapping from {_models_file}: {e}; keeping previous mapping")

    def reload_if_changed(self):
        now = time.time()
        if now - self._last_check < KEY_RELOAD_INTERVAL:
            return
        self._last_check = now
        try:
            current_mtime = os.path.getmtime(_models_file)
        except FileNotFoundError:
            if self._mtime != 0.0:
                log.warning(f"Model mapping file {_models_file} disappeared; keeping cached mapping")
            return
        except Exception as e:
            log.error(f"Cannot stat model mapping file: {e}")
            return

        if current_mtime != self._mtime:
            log.info(f"Model mapping file changed (mtime {self._mtime} -> {current_mtime}), reloading")
            self._load()

    def resolve(self, requested: str, available: set[str]) -> tuple[str, str | None]:
        """Return (final_model, original_model).

        original_model is non-None only when a translation actually happened.
        """
        requested_lower = requested.lower()
        if not requested_lower.startswith("claude-"):
            return requested, None

        tier = None
        if "opus" in requested_lower:
            tier = "opus"
        elif "sonnet" in requested_lower:
            tier = "sonnet"
        elif "haiku" in requested_lower:
            tier = "haiku"

        candidates = []
        if tier:
            mapped = self.mapping.get(tier, "")
            if mapped:
                candidates.append((mapped, f"tier '{tier}' mapping"))
        if self.default:
            candidates.append((self.default, "default model"))
        if available:
            for preferred in ("kimi-k2.7-code:cloud", "kimi-k2.5:cloud", "deepseek-v4-pro:cloud"):
                if preferred in available:
                    candidates.append((preferred, "preferred available model"))
                    break
            candidates.append((next(iter(available)), "first available model"))

        for model, source in candidates:
            if model and model in available:
                if model != requested:
                    log.info(f"Mapped Anthropic model {requested} -> {model} (using {source})")
                return model, requested

        # Nothing valid found, return original and let upstream fail cleanly.
        log.warning(f"Could not resolve Anthropic model {requested}; no valid Ollama model available")
        return requested, None


# --- Configuration ---
OLLAMA_BASE = CONFIG["ollama_base_url"]
PORT = int(CONFIG["port"])

# Max request body size in bytes. Requests larger than this are rejected before
# the body is read, protecting the proxy from memory pressure and oversized
# audit log writes. Default: 50 MiB.
MAX_BODY_SIZE = int(CONFIG["max_body_size"])

# --- Key store ---
# Supports two formats in keys.json:
#   Legacy (plaintext):  {"sk-xxx": "user1", "sk-yyy": "user2"}
#   Hashed:             {"_salt": "hex...", "users": {"sha256$hex...": "user1", ...}}
# Hashed format stores SHA-256(salt + token) so plaintext tokens never touch disk.
# The validate_key function tries both formats for backward compatibility.

_keys_file = CONFIG["keys_file"]
SALT_LEN = 32  # bytes


def _hash_token(salt: str, token: str) -> str:
    """Compute SHA-256(salt_hex + token)."""
    return hashlib.sha256((salt + token).encode()).hexdigest()


def _load_keys(keys_path: str) -> tuple[dict, dict, str | None]:
    """Load key file. Returns (plain_keys, hashed_users, salt).

    plain_keys:  {token: username} for legacy format
    hashed_users: {hash_hex: username} for hashed format
    salt: hex string for hashed format (None for legacy-only files)
    """
    if not os.path.exists(keys_path):
        return {}, {}, None
    with open(keys_path) as f:
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

    def __init__(self, keys_path: str):
        self.keys_path = keys_path
        self.api_keys: dict[str, str] = {}
        self.hashed_users: dict[str, str] = {}
        self.salt: str | None = None
        self._mtime: float = 0.0
        self._last_check: float = 0.0
        self._load()

    def _load(self):
        try:
            self.api_keys, self.hashed_users, self.salt = _load_keys(self.keys_path)
            try:
                self._mtime = os.path.getmtime(self.keys_path)
            except FileNotFoundError:
                self._mtime = 0.0
            except Exception as e:
                log.error(f"Cannot stat key file {self.keys_path}: {e}")
            count = len(self.api_keys) + len(self.hashed_users)
            log.info(f"Loaded {count} key(s) from {self.keys_path}")
        except Exception as e:
            log.error(f"Failed to load keys from {self.keys_path}: {e}; keeping previous keys")

    def reload_if_changed(self):
        now = time.time()
        if now - self._last_check < KEY_RELOAD_INTERVAL:
            return
        self._last_check = now
        try:
            current_mtime = os.path.getmtime(self.keys_path)
        except FileNotFoundError:
            if self._mtime != 0.0:
                log.warning(f"Key file {self.keys_path} disappeared; keeping cached keys")
            return
        except Exception as e:
            log.error(f"Cannot stat key file: {e}")
            return

        if current_mtime != self._mtime:
            log.info(f"Key file changed (mtime {self._mtime} -> {current_mtime}), reloading")
            self._load()


# Minimum seconds between mtime checks for keys.json.
KEY_RELOAD_INTERVAL = float(CONFIG["key_reload_interval"])


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
AUDIT_LOG = CONFIG["audit_log"]
AUDIT_ENABLED = bool(AUDIT_LOG)

# Initialise the key store and model mapper.
_key_store = _KeyStore(_keys_file)
_model_mapper = _ModelMapper()

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


# Global populated during lifespan: set of model names available in Ollama.
AVAILABLE_OLLAMA_MODELS: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage httpx client lifecycle across startup/shutdown."""
    global client, AVAILABLE_OLLAMA_MODELS
    client = httpx.AsyncClient(
        base_url=OLLAMA_BASE,
        timeout=httpx.Timeout(300.0),
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=100),
    )
    log.info(f"httpx client ready for Ollama at {OLLAMA_BASE}")
    try:
        try:
            r = await client.get("/api/tags", timeout=2.0)
            if r.status_code == 200:
                AVAILABLE_OLLAMA_MODELS = {m.get("name", "") for m in r.json().get("models", [])}
                log.info(f"Discovered {len(AVAILABLE_OLLAMA_MODELS)} Ollama models")
            else:
                log.warning(f"Could not fetch Ollama model list: {r.status_code}")
        except Exception as e:
            log.warning(f"Could not fetch Ollama model list: {e}")
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


def make_ollama_error(status_code: int, message: str) -> JSONResponse:
    """Return an Ollama-style JSON error response."""
    return JSONResponse(status_code=status_code, content={"error": message})


def native_ollama_auth_error_message(api_key: str | None) -> str:
    """Return a native Ollama client friendly authentication error."""
    if api_key:
        return (
            "Invalid aproxy token for native Ollama API. "
            "Check the API key configured in your Ollama client or the token embedded in the base URL."
        )
    return (
        "Authentication required for native Ollama API. "
        "Configure your Ollama client to send an aproxy token via Authorization: Bearer <token>, "
        "x-api-key, or HTTP Basic credentials in the base URL."
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


def _merge_usage(target: dict, usage: dict | None):
    """Merge token usage fields from an upstream event into the final usage map."""
    if isinstance(usage, dict):
        target.update(usage)


def _merge_stream_usage_from_line(line: str, total_tokens: dict):
    """Extract usage from one SSE data line, preserving the latest cumulative values."""
    if not line.startswith("data:"):
        return
    try:
        event = json.loads(line[5:].strip())
    except json.JSONDecodeError:
        return

    if isinstance(event.get("message"), dict):
        _merge_usage(total_tokens, event["message"].get("usage"))
    _merge_usage(total_tokens, event.get("usage"))


def _json_or_empty(body: bytes) -> dict:
    """Parse a JSON request/response body for analytics without changing forwarding."""
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ollama_native_tokens(data: dict) -> dict | None:
    """Convert native Ollama usage fields into aproxy token counters."""
    tokens = {}
    if isinstance(data.get("prompt_eval_count"), int):
        tokens["input_tokens"] = data["prompt_eval_count"]
    if isinstance(data.get("eval_count"), int):
        tokens["output_tokens"] = data["eval_count"]
    return tokens or None


def _merge_ollama_native_usage_from_line(line: str, total_tokens: dict):
    """Extract native Ollama usage fields from one NDJSON line."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    tokens = _ollama_native_tokens(data)
    if tokens:
        total_tokens.update(tokens)


_OLLAMA_MODEL_ROUTES = {
    ("POST", "/api/generate"),
    ("POST", "/api/chat"),
    ("POST", "/api/embed"),
}
_OLLAMA_METADATA_ROUTES = {
    ("GET", "/api/tags"),
    ("GET", "/api/ps"),
    ("GET", "/api/version"),
    ("POST", "/api/show"),
}
_OLLAMA_ADMIN_ROUTES = {
    ("POST", "/api/create"),
    ("POST", "/api/copy"),
    ("POST", "/api/pull"),
    ("POST", "/api/push"),
    ("DELETE", "/api/delete"),
}
_OLLAMA_ADMIN_PATHS = {path for _, path in _OLLAMA_ADMIN_ROUTES}
_OLLAMA_STREAMING_MODEL_PATHS = {"/api/generate", "/api/chat"}


def _classify_ollama_route(method: str, path: str) -> str:
    """Classify native Ollama routes before any upstream forwarding."""
    key = (method.upper(), path)
    if key in _OLLAMA_MODEL_ROUTES:
        return "model_egress"
    if key in _OLLAMA_METADATA_ROUTES:
        return "metadata"
    if key in _OLLAMA_ADMIN_ROUTES:
        return "admin_blocked"
    return "unsupported"


def _ollama_should_stream(path: str, body_json: dict) -> bool:
    """Native Ollama generate/chat stream by default unless stream is false."""
    return path in _OLLAMA_STREAMING_MODEL_PATHS and body_json.get("stream", True) is not False


def _upstream_target(request: Request, path: str) -> str:
    query = request.url.query
    return f"{path}?{query}" if query else path


def _ollama_headers(request: Request) -> dict:
    headers = {"Authorization": "Bearer ollama"}
    for h in ("accept", "content-type"):
        v = request.headers.get(h)
        if v:
            headers[h] = v
    return headers


def _response_from_upstream(resp: httpx.Response) -> Response:
    headers = {}
    content_type = resp.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


def audit(user_key: str, method: str, path: str, model: str | None = None,
          status: int | None = None, tokens: dict | None = None, error: str | None = None,
          api_family: str | None = None, route_class: str | None = None):
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
    if api_family:
        record["api_family"] = api_family
    if route_class:
        record["route_class"] = route_class
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


def _extract_basic_auth_token(value: str) -> str | None:
    """Extract an aproxy token from HTTP Basic credentials.

    Some native Ollama clients do not expose custom headers, but they do accept
    credentials embedded into the base URL, for example:
      http://sk-token@host:4001
      http://user:sk-token@host:4001

    HTTP clients send those as Authorization: Basic base64("user:password").
    Treat password as the token when present, otherwise use username.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value.strip() or None

    if ":" not in decoded:
        return decoded.strip() or None

    username, password = decoded.split(":", 1)
    if password.strip():
        return password.strip()
    return username.strip() or None


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
            return _extract_basic_auth_token(authorization[6:].strip())
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

    # Detect streaming responses by content-type header. Starlette's internal
    # _StreamingResponse may have media_type=None; the real type is only in
    # response headers.
    ct = response.headers.get("content-type", "")
    is_streaming = (
        getattr(request.state, "streaming_response", False)
        or ct.startswith("text/event-stream")
        or ct.startswith("application/x-ndjson")
    )

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
    "/api/generate", "/api/chat", "/api/embed", "/api/tags",
    "/api/ps", "/api/version", "/api/show",
}

def _normalize_path(path: str) -> str:
    """Normalize URL path to a fixed set of buckets for metrics labels."""
    if path in _KNOWN_PATHS:
        return path
    if path in _OLLAMA_ADMIN_PATHS:
        return "/api/admin"
    if path.startswith("/api/"):
        return "/api/other"
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

    requested_model = body_json.get("model", "unknown")
    stream = body_json.get("stream", False)

    # Translate Anthropic model IDs to Ollama model names using the hot-reloadable
    # models.json mapping. Native Ollama model names are left untouched.
    _model_mapper.reload_if_changed()
    resolved_model, original_model = _model_mapper.resolve(requested_model, AVAILABLE_OLLAMA_MODELS)
    if resolved_model != requested_model:
        body_json["model"] = resolved_model
        body = json.dumps(body_json).encode("utf-8")
        if original_model:
            log.info(f"[{user}] mapped Anthropic model {original_model} -> {resolved_model}")

    model = resolved_model

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

    log.info(f"[{user}] POST /v1/messages model={model} (requested={requested_model}) stream={stream}")

    try:
        if stream:
            return await _stream_response(request, client, user, model, body, headers)
        else:
            r = await client.post("/v1/messages", content=body, headers=headers)

            tokens = None
            error_text = None

            if r.status_code == 200:
                resp_body = r.json()
                if "usage" in resp_body:
                    tokens = resp_body["usage"]
            else:
                log.warning(f"[{user}] Ollama error {r.status_code}: {r.text[:500]}")
                error_text = r.text[:200]

            audit(user, "POST", "/v1/messages", model=model, status=r.status_code, tokens=tokens, error=error_text)

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
        stream_cm = http_client.stream("POST", "/v1/messages", content=body, headers=headers)
        resp = await stream_cm.__aenter__()
        request.state.stream_status = resp.status_code

        if resp.status_code != 200:
            try:
                error_body = await resp.aread()
            finally:
                await stream_cm.__aexit__(None, None, None)
            error_text = error_body.decode("utf-8", errors="replace")
            audit(user, "POST", "/v1/messages", model=model, status=resp.status_code, error=error_text[:200])
            return JSONResponse(
                status_code=resp.status_code,
                content=error_body_to_content(error_body, resp.headers.get("content-type", "")),
            )

        total_tokens = {}

        async def generate():
            stream_broken = False
            client_disconnected = False
            yielded_any = False
            try:
                async for line in resp.aiter_lines():
                    yielded_any = True
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
                    _merge_stream_usage_from_line(line, total_tokens)
            except httpx.StreamClosed:
                if yielded_any:
                    client_disconnected = True
                    log.info(f"[{user}] client disconnected during stream")
                else:
                    stream_broken = True
                    log.error(f"[{user}] Ollama closed the stream unexpectedly before any data")
            except Exception as e:
                if yielded_any:
                    client_disconnected = True
                    log.warning(f"[{user}] stream error after yielding data: {e}")
                else:
                    stream_broken = True
                    log.error(f"[{user}] Error while streaming from Ollama: {e}")
            finally:
                status = 200 if not stream_broken else 500
                request.state.stream_status = status
                error_msg = None
                if client_disconnected:
                    error_msg = "client disconnected"
                elif stream_broken:
                    error_msg = "stream closed by Ollama"
                audit(user, "POST", "/v1/messages", model=model, status=status, tokens=total_tokens if total_tokens else None, error=error_msg)
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception as e:
                    log.warning(f"[{user}] error closing Ollama stream: {e}")
                if stream_broken:
                    # Emit a final SSE error event so the client sees a clean failure
                    error_event = {
                        "type": "error",
                        "error": {
                            "type": "internal_error",
                            "message": "Ollama closed the stream unexpectedly. Check that the requested model is loaded and available.",
                        },
                    }
                    yield f"event: error\ndata: {json.dumps(error_event)}\n\n"

        request.state.streaming_response = True
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


async def _ollama_native_response(
    request: Request,
    http_client,
    user: str,
    path: str,
    route_class: str,
    model: str | None,
    body: bytes,
    headers: dict,
):
    """Proxy a non-streaming native Ollama request and record audit usage."""
    target = _upstream_target(request, path)
    resp = await http_client.request(request.method, target, content=body, headers=headers)

    tokens = None
    error_text = None
    if route_class == "model_egress" and resp.status_code == 200:
        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                tokens = _ollama_native_tokens(resp.json())
            except (json.JSONDecodeError, ValueError):
                tokens = None
    elif resp.status_code >= 400:
        error_text = resp.text[:200]

    audit(
        user,
        request.method,
        path,
        model=model,
        status=resp.status_code,
        tokens=tokens,
        error=error_text,
        api_family="ollama",
        route_class=route_class,
    )
    return _response_from_upstream(resp)


async def _ollama_native_stream_response(
    request: Request,
    http_client,
    user: str,
    path: str,
    route_class: str,
    model: str | None,
    body: bytes,
    headers: dict,
):
    """Stream native Ollama NDJSON while extracting final usage fields."""
    request.state.stream_status = 500
    target = _upstream_target(request, path)

    try:
        stream_cm = http_client.stream(request.method, target, content=body, headers=headers)
        resp = await stream_cm.__aenter__()
        request.state.stream_status = resp.status_code

        if resp.status_code != 200:
            try:
                error_body = await resp.aread()
            finally:
                await stream_cm.__aexit__(None, None, None)
            error_text = error_body.decode("utf-8", errors="replace")
            audit(
                user,
                request.method,
                path,
                model=model,
                status=resp.status_code,
                error=error_text[:200],
                api_family="ollama",
                route_class=route_class,
            )
            content_type = resp.headers.get("content-type", "application/json")
            return Response(
                content=error_body,
                status_code=resp.status_code,
                headers={"content-type": content_type},
            )

        total_tokens = {}

        async def generate():
            stream_broken = False
            yielded_any = False
            try:
                async for line in resp.aiter_lines():
                    yielded_any = True
                    yield line + "\n"
                    _merge_ollama_native_usage_from_line(line, total_tokens)
            except httpx.StreamClosed:
                stream_broken = True
                if yielded_any:
                    log.warning(f"[{user}] Ollama closed native stream unexpectedly after partial data")
                else:
                    log.error(f"[{user}] Ollama closed native stream unexpectedly before any data")
            except Exception as e:
                stream_broken = True
                if yielded_any:
                    log.warning(f"[{user}] native Ollama stream error after partial data: {e}")
                else:
                    log.error(f"[{user}] Error while streaming from native Ollama route {path}: {e}")
            finally:
                status = 200 if not stream_broken else 500
                request.state.stream_status = status
                error_msg = None
                if stream_broken:
                    error_msg = "stream closed by Ollama"
                audit(
                    user,
                    request.method,
                    path,
                    model=model,
                    status=status,
                    tokens=total_tokens if total_tokens else None,
                    error=error_msg,
                    api_family="ollama",
                    route_class=route_class,
                )
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception as e:
                    log.warning(f"[{user}] error closing native Ollama stream: {e}")
                if stream_broken:
                    yield json.dumps({"error": "Ollama closed the stream unexpectedly."}) + "\n"

        request.state.streaming_response = True
        content_type = resp.headers.get("content-type", "application/x-ndjson")
        return StreamingResponse(generate(), media_type=content_type)

    except httpx.ReadTimeout:
        log.error(f"[{user}] Native Ollama stream timeout for {path}")
        request.state.stream_status = 504
        audit(
            user,
            request.method,
            path,
            model=model,
            status=504,
            error="stream timeout",
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(504, "Ollama stream timed out")
    except Exception as e:
        log.error(f"[{user}] Native Ollama stream error for {path}: {e}", exc_info=True)
        request.state.stream_status = 500
        audit(
            user,
            request.method,
            path,
            model=model,
            status=500,
            error=str(e),
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(500, f"Ollama proxy stream error: {e}")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def ollama_api(request: Request, path: str, x_api_key: str | None = Header(None),
                     authorization: str | None = Header(None)):
    """Allowlisted native Ollama API proxy with aproxy authentication and audit."""
    api_path = f"/api/{path}"
    api_key = extract_api_key(x_api_key, authorization)
    try:
        user = await validate_key_async(api_key, request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        if exc.status_code == 401:
            return make_ollama_error(exc.status_code, native_ollama_auth_error_message(api_key))
        return make_ollama_error(exc.status_code, detail.get("message", "Authentication failed"))

    route_class = _classify_ollama_route(request.method, api_path)
    if route_class == "admin_blocked":
        audit(
            user,
            request.method,
            api_path,
            status=403,
            error="native Ollama admin route is disabled",
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(403, "This Ollama API route is disabled by aproxy policy")
    if route_class == "unsupported":
        audit(
            user,
            request.method,
            api_path,
            status=404,
            error="unsupported native Ollama API route",
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(404, "Unsupported Ollama API route")

    body = await request.body() if request.method in {"POST", "PUT", "PATCH"} else b""
    body_json = _json_or_empty(body)
    model = body_json.get("model") if isinstance(body_json.get("model"), str) else None
    headers = _ollama_headers(request)

    log.info(f"[{user}] {request.method} {api_path} route_class={route_class} model={model or '-'}")

    try:
        if route_class == "model_egress" and _ollama_should_stream(api_path, body_json):
            return await _ollama_native_stream_response(
                request, http_client=client, user=user, path=api_path,
                route_class=route_class, model=model, body=body, headers=headers,
            )
        return await _ollama_native_response(
            request, http_client=client, user=user, path=api_path,
            route_class=route_class, model=model, body=body, headers=headers,
        )
    except httpx.ReadTimeout:
        log.error(f"[{user}] Native Ollama request timeout for {api_path}")
        audit(
            user,
            request.method,
            api_path,
            model=model,
            status=504,
            error="timeout",
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(504, "Ollama request timed out")
    except Exception as e:
        log.error(f"[{user}] Native Ollama proxy error for {api_path}: {e}", exc_info=True)
        audit(
            user,
            request.method,
            api_path,
            model=model,
            status=500,
            error=str(e),
            api_family="ollama",
            route_class=route_class,
        )
        return make_ollama_error(500, f"Ollama proxy error: {e}")


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


# --- Catch-all: reject unrecognised paths after auth ---


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str, x_api_key: str | None = Header(None),
                     authorization: str | None = Header(None)):
    """Reject unmatched paths so model-capable traffic cannot bypass allowlists."""
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key_async(api_key, request)
    audit(user, request.method, f"/{path}", status=404, error="unsupported route")
    return make_error(404, "not_found", "Unsupported API route")


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
