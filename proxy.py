#!/opt/litellm-venv/bin/python3
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

import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

# --- Configuration ---
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
PORT = int(os.environ.get("ANTHROPIC_PROXY_PORT", "4001"))

# Static key file: maps token VALUES to user names
# Supports both x-api-key and Authorization: Bearer tokens
# Format: {"sk-xxx": "sergey", "sk-yyy": "hermes", ...}
API_KEYS = {}
_keys_file = os.environ.get("API_KEYS_FILE", "/home/sergey/Projects/aproxy/keys.json")
if os.path.exists(_keys_file):
    with open(_keys_file) as f:
        API_KEYS = json.load(f)

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

# --- App ---
app = FastAPI(title="aproxy")
client = httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=httpx.Timeout(300.0))


def make_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Return Anthropic-compatible error response."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def audit(user_key: str, method: str, path: str, model: str | None = None,
          status: int | None = None, tokens: dict | None = None, error: str | None = None):
    """Write audit record."""
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


async def validate_key(api_key: str | None) -> str:
    """Validate authentication token. Returns user identifier. Raises HTTPException with Anthropic format."""
    if not api_key:
        raise HTTPException(status_code=401, detail={"type": "authentication_error", "message": "Authentication required. Set ANTHROPIC_AUTH_TOKEN or provide a valid key."})

    # Check if token maps to a user in keys.json
    if api_key in API_KEYS:
        return API_KEYS[api_key]

    raise HTTPException(status_code=401, detail={"type": "authentication_error", "message": "Invalid authentication token. Check your ANTHROPIC_AUTH_TOKEN or key value."})


@app.middleware("http")
async def add_cors_and_timing(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    response.headers["X-Response-Time"] = f"{elapsed:.3f}s"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


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

@app.get("/health")
async def health():
    try:
        r = await client.get("/api/version")
        ollama_version = r.json().get("version", "unknown") if r.status_code == 200 else "unreachable"
    except Exception:
        ollama_version = "unreachable"
    return {"status": "ok", "ollama": {"version": ollama_version}, "proxy": "aproxy/1.3"}


@app.get("/v1/models")
async def list_models(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key(api_key)
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
    user = await validate_key(api_key)

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
            return await _stream_response(client, user, model, body, headers)
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


async def _stream_response(http_client, user: str, model: str, body: bytes, headers: dict):
    """Stream response from Ollama back to client."""
    async def generate():
        total_tokens = {}
        try:
            async with http_client.stream("POST", "/v1/messages", content=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    audit(user, "POST", "/v1/messages", model=model, status=resp.status_code, error=error_body.decode()[:200])
                    yield error_body
                    return

                async for line in resp.aiter_lines():
                    yield line + "\n\n"
                    # Try to extract token counts from stream events
                    if '"type":"message_delta"' in line or '"type": "message_delta"' in line:
                        try:
                            event = json.loads(line.removeprefix("data: ").strip())
                            if "usage" in event:
                                total_tokens.update(event["usage"])
                        except (json.JSONDecodeError, KeyError):
                            pass

            audit(user, "POST", "/v1/messages", model=model, status=200, tokens=total_tokens if total_tokens else None)
        except Exception as e:
            log.error(f"[{user}] Stream error: {e}")
            audit(user, "POST", "/v1/messages", model=model, status=500, error=str(e))

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/v1/messages/batches")
async def messages_batches(request: Request, x_api_key: str | None = Header(None),
                            authorization: str | None = Header(None)):
    """Message batches - return not supported."""
    api_key = extract_api_key(x_api_key, authorization)
    await validate_key(api_key)
    return make_error(404, "not_found", "Message batches are not supported by this proxy")


# --- Organization/User info endpoints that Claude Code expects ---

@app.api_route("/v1/organizations", methods=["GET"])
async def organizations(request: Request, x_api_key: str | None = Header(None),
                         authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key(api_key)
    audit(user, "GET", "/v1/organizations")
    return {"data": [], "has_more": False}


@app.api_route("/v1/organizations/{org_id}/users", methods=["GET"])
async def org_users(org_id: str, x_api_key: str | None = Header(None),
                    authorization: str | None = Header(None)):
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key(api_key)
    audit(user, "GET", f"/v1/organizations/{org_id}/users")
    return {"data": [], "has_more": False}


# --- Catch-all: proxy unrecognised paths to Ollama with auth ---


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str, x_api_key: str | None = Header(None),
                     authorization: str | None = Header(None)):
    """Proxy any unmatched paths to Ollama with authentication."""
    api_key = extract_api_key(x_api_key, authorization)
    user = await validate_key(api_key)
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
    log.info(f"Starting aproxy on :{PORT} -> {OLLAMA_BASE}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)