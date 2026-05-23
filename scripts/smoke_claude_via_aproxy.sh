#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APROXY_SMOKE_ENV_FILE:-$ROOT_DIR/.env}"
LOG_LINES="${APROXY_SMOKE_LOG_LINES:-30}"
PROMPT="${APROXY_SMOKE_PROMPT:-Ответь ровно строкой: APROXY_SMOKE_OK}"
EXPECTED="${APROXY_SMOKE_EXPECTED:-APROXY_SMOKE_OK}"
MODEL="${APROXY_SMOKE_MODEL:-}"
SMOKE_TMPDIR=""

usage() {
  cat <<'USAGE'
Usage: scripts/smoke_claude_via_aproxy.sh [options]

Runs an end-to-end smoke test for Claude Code -> aproxy -> Ollama:
  1. loads .env without printing secrets;
  2. checks /health;
  3. checks unauthenticated /v1/models is rejected;
  4. checks authenticated /v1/models and /metrics;
  5. runs claude -p through ANTHROPIC_BASE_URL;
  6. prints recent aproxy audit/proxy logs when available.

Options:
  --env FILE          Load environment from FILE instead of .env.
  --model MODEL      Claude model argument. Defaults to env model or sonnet.
  --prompt TEXT      Prompt for claude -p.
  --expected TEXT    Expected substring in claude output.
  --log-lines N      Number of log lines to show. Default: 30.
  --no-logs          Skip log tail.
  --help             Show this help.

Environment overrides:
  APROXY_LOG_HOST        SSH host for logs. Defaults to host from ANTHROPIC_BASE_URL.
  APROXY_AUDIT_LOG       Audit log path. Default: /var/log/aproxy/audit.jsonl.
  APROXY_PROXY_LOG       Proxy log path. Default: /var/log/aproxy/proxy.log.
  APROXY_SMOKE_TIMEOUT   Claude timeout seconds. Default: 120.
USAGE
}

SHOW_LOGS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV_FILE="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --prompt)
      PROMPT="$2"
      shift 2
      ;;
    --expected)
      EXPECTED="$2"
      shift 2
      ;;
    --log-lines)
      LOG_LINES="$2"
      shift 2
      ;;
    --no-logs)
      SHOW_LOGS=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

step() {
  printf '\n==> %s\n' "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

redact() {
  local token="${ANTHROPIC_AUTH_TOKEN:-}"
  if [[ -n "$token" ]]; then
    sed -E \
      -e "s#${token//\//\\/}#[REDACTED_ANTHROPIC_AUTH_TOKEN]#g" \
      -e 's/sk-[A-Za-z0-9_-]{12,}/sk-[REDACTED]/g'
  else
    sed -E 's/sk-[A-Za-z0-9_-]{12,}/sk-[REDACTED]/g'
  fi
}

load_env() {
  [[ -f "$ENV_FILE" ]] || fail "env file not found: $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
  [[ -n "${ANTHROPIC_BASE_URL:-}" ]] || fail "ANTHROPIC_BASE_URL is not set by $ENV_FILE"
  [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]] || fail "ANTHROPIC_AUTH_TOKEN is not set by $ENV_FILE"
  MODEL="${MODEL:-${ANTHROPIC_DEFAULT_SONNET_MODEL:-sonnet}}"
}

base_url() {
  printf '%s' "${ANTHROPIC_BASE_URL%/}"
}

base_host() {
  python3 - "$ANTHROPIC_BASE_URL" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).hostname or "")
PY
}

curl_body_status() {
  local output_file="$1"
  shift
  curl --silent --show-error --location --max-time 20 \
    --output "$output_file" \
    --write-out '%{http_code}' \
    "$@"
}

show_json_summary() {
  local file="$1"
  python3 - "$file" <<'PY' 2>/dev/null || sed -n '1,20p' "$file"
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
if isinstance(data, dict):
    keys = list(data.keys())[:12]
    print("json keys:", ", ".join(keys))
    if "data" in data and isinstance(data["data"], list):
        print("data items:", len(data["data"]))
        for item in data["data"][:5]:
            if isinstance(item, dict):
                print(" -", item.get("id") or item.get("model") or item)
else:
    print(type(data).__name__)
PY
}

run_preflight() {
  local tmpdir="$1"
  local base
  base="$(base_url)"

  step "Environment"
  echo "env file: $ENV_FILE"
  echo "base url: $base"
  echo "model: $MODEL"
  echo "token: [set, redacted]"

  step "GET /health"
  local health_body="$tmpdir/health.json"
  local health_status
  health_status="$(curl_body_status "$health_body" "$base/health")"
  echo "status: $health_status"
  cat "$health_body" | redact
  [[ "$health_status" == "200" ]] || fail "/health returned $health_status"

  step "GET /v1/models without auth must fail"
  local unauth_body="$tmpdir/models_unauth.json"
  local unauth_status
  unauth_status="$(curl_body_status "$unauth_body" "$base/v1/models")"
  echo "status: $unauth_status"
  cat "$unauth_body" | redact
  [[ "$unauth_status" == "401" ]] || fail "unauthenticated /v1/models returned $unauth_status, expected 401"

  step "GET /v1/models with auth"
  local models_body="$tmpdir/models_auth.json"
  local models_status
  models_status="$(curl_body_status "$models_body" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" "$base/v1/models")"
  echo "status: $models_status"
  show_json_summary "$models_body" | redact
  [[ "$models_status" == "200" ]] || fail "authenticated /v1/models returned $models_status"

  step "GET /metrics with auth"
  local metrics_body="$tmpdir/metrics.txt"
  local metrics_status
  metrics_status="$(curl_body_status "$metrics_body" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" "$base/metrics")"
  echo "status: $metrics_status"
  grep -E '^(aproxy_requests_total|aproxy_tokens_(input|output)_total|aproxy_active_connections)' "$metrics_body" | tail -n 20 | redact || true
  [[ "$metrics_status" == "200" ]] || fail "authenticated /metrics returned $metrics_status"
}

run_claude() {
  local tmpdir="$1"
  local output="$tmpdir/claude_output.txt"
  local timeout_s="${APROXY_SMOKE_TIMEOUT:-120}"

  step "claude -p through aproxy"
  command -v claude >/dev/null 2>&1 || fail "claude command not found"
  echo "prompt: $PROMPT"
  echo "expected substring: $EXPECTED"

  local rc=0
  set +e
  if command -v timeout >/dev/null 2>&1; then
    printf '%s\n' "$PROMPT" | timeout "$timeout_s" claude -p \
      --model "$MODEL" \
      --no-session-persistence >"$output" 2>&1
  elif command -v gtimeout >/dev/null 2>&1; then
    printf '%s\n' "$PROMPT" | gtimeout "$timeout_s" claude -p \
      --model "$MODEL" \
      --no-session-persistence >"$output" 2>&1
  else
    printf '%s\n' "$PROMPT" | claude -p \
      --model "$MODEL" \
      --no-session-persistence >"$output" 2>&1
  fi
  rc=$?
  set -e

  cat "$output" | redact
  [[ "$rc" == "0" ]] || fail "claude exited with status $rc"
  grep -F "$EXPECTED" "$output" >/dev/null || fail "claude output did not contain expected text"
}

show_logs() {
  [[ "$SHOW_LOGS" == "1" ]] || return 0

  local host="${APROXY_LOG_HOST:-$(base_host)}"
  local audit_log="${APROXY_AUDIT_LOG:-/var/log/aproxy/audit.jsonl}"
  local proxy_log="${APROXY_PROXY_LOG:-/var/log/aproxy/proxy.log}"

  step "Recent aproxy logs"
  echo "log host: ${host:-local}"
  echo "audit log: $audit_log"
  echo "proxy log: $proxy_log"

  if [[ -z "$host" || "$host" == "127.0.0.1" || "$host" == "localhost" ]]; then
    for f in "$audit_log" "$proxy_log"; do
      if [[ -r "$f" ]]; then
        echo "--- $f"
        tail -n "$LOG_LINES" "$f" | redact
      else
        echo "--- $f is not readable locally"
      fi
    done
    return 0
  fi

  if ! command -v ssh >/dev/null 2>&1; then
    echo "ssh not found; skipping remote log tail"
    return 0
  fi

  ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
    "for f in '$audit_log' '$proxy_log'; do echo --- \$f; if test -r \$f; then tail -n '$LOG_LINES' \$f; else echo not-readable-or-missing; fi; done" \
    | redact || echo "remote log tail failed; set APROXY_LOG_HOST or run with --no-logs"
}

main() {
  load_env
  SMOKE_TMPDIR="$(mktemp -d)"
  trap 'rm -rf "$SMOKE_TMPDIR"' EXIT

  run_preflight "$SMOKE_TMPDIR"
  run_claude "$SMOKE_TMPDIR"
  show_logs

  step "Smoke test passed"
}

main "$@"
