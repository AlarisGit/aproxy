#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APROXY_INTEGRATION_ENV_FILE:-$ROOT_DIR/.env}"
LOG_LINES="${APROXY_INTEGRATION_LOG_LINES:-80}"
TIMEOUT_SECONDS="${APROXY_INTEGRATION_TIMEOUT:-180}"
RUN_WEB=0
RUN_LONG=0
RUN_AGENT=0
REQUIRE_ALL_TIERS=0
SHOW_LOGS=1
SUITE_TMPDIR=""
SUITE_STARTED_EPOCH="$(date -u +%s)"
PERMISSION_MODE="${APROXY_INTEGRATION_PERMISSION_MODE:-bypassPermissions}"
AUDIT_TAIL_FILE=""
PROXY_TAIL_FILE=""

usage() {
  cat <<'USAGE'
Usage: scripts/integration_claude_code_suite.sh [options]

Runs a realistic Claude Code integration suite through aproxy/Ollama. Unlike the
quick smoke test, this suite intentionally does NOT pass --model. Claude Code
must choose models from ANTHROPIC_DEFAULT_OPUS_MODEL,
ANTHROPIC_DEFAULT_SONNET_MODEL, and ANTHROPIC_DEFAULT_HAIKU_MODEL.

Default checks:
  - aproxy health/auth/models/metrics preflight;
  - simple headless prompt;
  - filesystem edit workflow in an isolated temp directory;
  - shell workflow in an isolated temp directory;
  - SSE regression check: Claude output must not contain JSON parse warnings;
  - audit/log/metrics summary;
  - model-tier coverage report from fresh audit records.

Options:
  --env FILE            Load environment from FILE instead of .env.
  --web                 Enable WebFetch/WebSearch checks.
  --long                Enable long-running shell/output checks.
  --agent               Enable agent/background capability probes.
  --full                Enable --web --long --agent.
  --require-all-tiers   Fail unless fresh audit records include opus, sonnet,
                        and haiku model ids from the loaded .env.
  --log-lines N         Number of remote/local log lines to inspect. Default: 80.
  --no-logs             Skip final log tail.
  --help                Show this help.

Environment overrides:
  APROXY_LOG_HOST              SSH host for logs. Defaults to host from
                               ANTHROPIC_BASE_URL.
  APROXY_AUDIT_LOG             Audit log path. Default:
                               /var/log/aproxy/audit.jsonl.
  APROXY_PROXY_LOG             Proxy log path. Default:
                               /var/log/aproxy/proxy.log.
  APROXY_INTEGRATION_TIMEOUT   Per-Claude-command timeout seconds. Default: 180.
  APROXY_INTEGRATION_PERMISSION_MODE
                               Claude Code permission mode for test cases.
                               Default: bypassPermissions. Test file edits run
                               only inside mktemp directories.

Notes:
  This suite is designed to be safe to run from the repository root. File edits
  happen only under a temporary directory created by mktemp.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV_FILE="$2"
      shift 2
      ;;
    --web)
      RUN_WEB=1
      shift
      ;;
    --long)
      RUN_LONG=1
      shift
      ;;
    --agent)
      RUN_AGENT=1
      shift
      ;;
    --full)
      RUN_WEB=1
      RUN_LONG=1
      RUN_AGENT=1
      shift
      ;;
    --require-all-tiers)
      REQUIRE_ALL_TIERS=1
      shift
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

case_step() {
  printf '\n-- %s\n' "$*"
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
  [[ -n "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}" ]] || fail "ANTHROPIC_DEFAULT_OPUS_MODEL is not set by $ENV_FILE"
  [[ -n "${ANTHROPIC_DEFAULT_SONNET_MODEL:-}" ]] || fail "ANTHROPIC_DEFAULT_SONNET_MODEL is not set by $ENV_FILE"
  [[ -n "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}" ]] || fail "ANTHROPIC_DEFAULT_HAIKU_MODEL is not set by $ENV_FILE"
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

run_preflight() {
  local tmpdir="$1"
  local base
  base="$(base_url)"

  step "Environment"
  echo "env file: $ENV_FILE"
  echo "base url: $base"
  echo "default opus: $ANTHROPIC_DEFAULT_OPUS_MODEL"
  echo "default sonnet: $ANTHROPIC_DEFAULT_SONNET_MODEL"
  echo "default haiku: $ANTHROPIC_DEFAULT_HAIKU_MODEL"
  echo "permission mode: $PERMISSION_MODE"
  echo "token: [set, redacted]"

  step "aproxy preflight"
  local health_body="$tmpdir/health.json"
  local health_status
  health_status="$(curl_body_status "$health_body" "$base/health")"
  echo "health status: $health_status"
  cat "$health_body" | redact
  [[ "$health_status" == "200" ]] || fail "/health returned $health_status"

  local unauth_body="$tmpdir/models_unauth.json"
  local unauth_status
  unauth_status="$(curl_body_status "$unauth_body" "$base/v1/models")"
  echo "unauthenticated /v1/models status: $unauth_status"
  [[ "$unauth_status" == "401" ]] || fail "unauthenticated /v1/models returned $unauth_status, expected 401"

  local models_body="$tmpdir/models_auth.json"
  local models_status
  models_status="$(curl_body_status "$models_body" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" "$base/v1/models")"
  echo "authenticated /v1/models status: $models_status"
  [[ "$models_status" == "200" ]] || fail "authenticated /v1/models returned $models_status"

  python3 - "$models_body" "$ANTHROPIC_DEFAULT_OPUS_MODEL" "$ANTHROPIC_DEFAULT_SONNET_MODEL" "$ANTHROPIC_DEFAULT_HAIKU_MODEL" <<'PY'
import json, sys
path, *models = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
ids = {item.get("id") for item in data.get("data", []) if isinstance(item, dict)}
missing = [m for m in models if m not in ids]
print("model count:", len(ids))
for model in models:
    print(("available: " if model in ids else "missing: ") + model)
if missing:
    raise SystemExit("configured model(s) missing from /v1/models: " + ", ".join(missing))
PY

  local metrics_body="$tmpdir/metrics.txt"
  local metrics_status
  metrics_status="$(curl_body_status "$metrics_body" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" "$base/metrics")"
  echo "authenticated /metrics status: $metrics_status"
  [[ "$metrics_status" == "200" ]] || fail "authenticated /metrics returned $metrics_status"
}

run_claude_case() {
  local case_id="$1"
  local prompt="$2"
  local expected="$3"
  local workdir="${4:-$ROOT_DIR}"
  local output="$SUITE_TMPDIR/${case_id}.out"
  local rc=0

  case_step "$case_id"
  echo "workdir: $workdir"
  echo "expected: $expected"
  set +e
  if command -v timeout >/dev/null 2>&1; then
    (cd "$workdir" && printf '%s\n' "$prompt" | timeout "$TIMEOUT_SECONDS" claude -p --no-session-persistence --permission-mode "$PERMISSION_MODE" >"$output" 2>&1)
  elif command -v gtimeout >/dev/null 2>&1; then
    (cd "$workdir" && printf '%s\n' "$prompt" | gtimeout "$TIMEOUT_SECONDS" claude -p --no-session-persistence --permission-mode "$PERMISSION_MODE" >"$output" 2>&1)
  else
    (cd "$workdir" && printf '%s\n' "$prompt" | claude -p --no-session-persistence --permission-mode "$PERMISSION_MODE" >"$output" 2>&1)
  fi
  rc=$?
  set -e

  cat "$output" | redact
  [[ "$rc" == "0" ]] || fail "$case_id: claude exited with status $rc"
  grep -F "$expected" "$output" >/dev/null || fail "$case_id: expected text not found"
  if grep -F "Could not parse message into JSON" "$output" >/dev/null; then
    fail "$case_id: SSE framing regression detected in Claude output"
  fi
}

run_default_suite() {
  step "Claude Code integration cases without --model"

  run_claude_case \
    "A01-basic-prompt" \
    "Ответь ровно строкой: APROXY_BASIC_OK" \
    "APROXY_BASIC_OK"

  local fsdir="$SUITE_TMPDIR/fs-work"
  mkdir -p "$fsdir"
  cat > "$fsdir/bug.py" <<'PY'
def divide(a, b):
    return a / b
PY
  run_claude_case \
    "B01-filesystem-edit" \
    "В файле bug.py исправь функцию divide: при b == 0 она должна возвращать None вместо исключения. Внеси правку в файл и в финальном ответе напиши APROXY_FILE_OK." \
    "APROXY_FILE_OK" \
    "$fsdir"
  grep -E "b[[:space:]]*==[[:space:]]*0|if not b" "$fsdir/bug.py" >/dev/null || fail "B01-filesystem-edit: bug.py was not updated with a zero-division guard"

  local shelldir="$SUITE_TMPDIR/shell-work"
  mkdir -p "$shelldir"
  printf 'alpha\nbeta\ngamma\n' > "$shelldir/items.txt"
  run_claude_case \
    "D01-shell-workflow" \
    "Используя shell-команду, посчитай количество строк в items.txt. В финальном ответе напиши APROXY_SHELL_OK и число строк." \
    "APROXY_SHELL_OK" \
    "$shelldir"

  if [[ "$RUN_WEB" == "1" ]]; then
    run_claude_case \
      "C01-webfetch" \
      "Получить https://example.com через доступный web-инструмент и вывести заголовок страницы. В финальном ответе напиши APROXY_WEBFETCH_OK." \
      "APROXY_WEBFETCH_OK"

    run_claude_case \
      "C02-websearch" \
      "Найди в интернете текущую стабильную версию Go и выведи одну строку с ней. В финальном ответе также напиши APROXY_WEBSEARCH_OK." \
      "APROXY_WEBSEARCH_OK"
  else
    echo "Skipping web checks. Use --web or --full to enable."
  fi

  if [[ "$RUN_LONG" == "1" ]]; then
    local longdir="$SUITE_TMPDIR/long-work"
    mkdir -p "$longdir"
    run_claude_case \
      "G01-long-shell" \
      "Выполни shell-команду, которая создаст файл numbers.txt со строками от 1 до 300, затем выведи последние 5 строк. В финальном ответе напиши APROXY_LONG_OK." \
      "APROXY_LONG_OK" \
      "$longdir"
  else
    echo "Skipping long-running checks. Use --long or --full to enable."
  fi

  if [[ "$RUN_AGENT" == "1" ]]; then
    local agentdir="$SUITE_TMPDIR/agent-work"
    mkdir -p "$agentdir"
    printf 'one\n' > "$agentdir/a.txt"
    printf 'two\n' > "$agentdir/b.txt"
    run_claude_case \
      "E01-agent-probe" \
      "Если headless Claude Code поддерживает subagents/background agents, используй их для независимого анализа a.txt и b.txt. Если не поддерживает, выполни анализ сам. В финальном ответе напиши APROXY_AGENT_OK." \
      "APROXY_AGENT_OK" \
      "$agentdir"
  else
    echo "Skipping agent/background probes. Use --agent or --full to enable."
  fi
}

fetch_log_window() {
  local host="${APROXY_LOG_HOST:-$(base_host)}"
  local audit_log="${APROXY_AUDIT_LOG:-/var/log/aproxy/audit.jsonl}"
  local proxy_log="${APROXY_PROXY_LOG:-/var/log/aproxy/proxy.log}"
  AUDIT_TAIL_FILE="$SUITE_TMPDIR/audit_tail.jsonl"
  PROXY_TAIL_FILE="$SUITE_TMPDIR/proxy_tail.log"

  if [[ -z "$host" || "$host" == "127.0.0.1" || "$host" == "localhost" ]]; then
    if [[ -r "$audit_log" ]]; then
      tail -n "$LOG_LINES" "$audit_log" > "$AUDIT_TAIL_FILE"
    else
      : > "$AUDIT_TAIL_FILE"
    fi
    if [[ -r "$proxy_log" ]]; then
      tail -n "$LOG_LINES" "$proxy_log" > "$PROXY_TAIL_FILE"
    else
      : > "$PROXY_TAIL_FILE"
    fi
  else
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
      "tail -n '$LOG_LINES' '$audit_log' 2>/dev/null || true" > "$AUDIT_TAIL_FILE" || true
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
      "tail -n '$LOG_LINES' '$proxy_log' 2>/dev/null || true" > "$PROXY_TAIL_FILE" || true
  fi
}

analyze_audit_tiers() {
  local audit_file="$1"

  step "Fresh audit model-tier coverage"
  python3 - "$audit_file" "$SUITE_STARTED_EPOCH" \
    "$ANTHROPIC_DEFAULT_OPUS_MODEL" \
    "$ANTHROPIC_DEFAULT_SONNET_MODEL" \
    "$ANTHROPIC_DEFAULT_HAIKU_MODEL" <<'PY'
import json
import sys
from collections import Counter
from datetime import datetime

audit_file, started_s, opus, sonnet, haiku = sys.argv[1:]
started = int(started_s)
models = {"opus": opus, "sonnet": sonnet, "haiku": haiku}
counts = Counter()
statuses = Counter()
fresh = []

with open(audit_file, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            ts = item.get("ts")
            if ts:
                event_epoch = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                if event_epoch < started - 2:
                    continue
            fresh.append(item)
            model = item.get("model")
            if model:
                for tier, expected_model in models.items():
                    if model == expected_model:
                        counts[tier] += 1
                        break
                else:
                    counts["other:" + model] += 1
            if "status" in item:
                statuses[str(item["status"])] += 1
        except Exception:
            continue

print("fresh audit records:", len(fresh))
print("statuses:", dict(statuses))
for tier in ("opus", "sonnet", "haiku"):
    print(f"{tier}: {counts[tier]} ({models[tier]})")
others = {k: v for k, v in counts.items() if k.startswith("other:")}
if others:
    print("other models:", others)

missing = [tier for tier in ("opus", "sonnet", "haiku") if counts[tier] == 0]
if missing:
    print("missing tiers:", ", ".join(missing))
    raise SystemExit(42)
PY
}

show_log_summary() {
  [[ "$SHOW_LOGS" == "1" ]] || return 0
  local audit_file="$1"
  local proxy_file="$2"

  step "Recent aproxy audit/proxy logs"
  echo "--- audit"
  tail -n 20 "$audit_file" | redact || true
  echo "--- proxy"
  tail -n 20 "$proxy_file" | redact || true
}

run_metrics_summary() {
  local tmpdir="$1"
  local base
  base="$(base_url)"
  local metrics_body="$tmpdir/final_metrics.txt"
  local metrics_status
  metrics_status="$(curl_body_status "$metrics_body" -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" "$base/metrics")"
  [[ "$metrics_status" == "200" ]] || fail "final /metrics returned $metrics_status"

  step "Final metrics summary"
  grep -E '^(aproxy_requests_total|aproxy_tokens_(input|output)_total|aproxy_active_connections)' "$metrics_body" | tail -n 40 | redact || true
  if ! grep -E '^aproxy_active_connections[[:space:]]+0(\.0)?$' "$metrics_body" >/dev/null; then
    fail "aproxy_active_connections did not return to 0"
  fi
}

main() {
  load_env
  SUITE_TMPDIR="$(mktemp -d)"
  trap 'rm -rf "$SUITE_TMPDIR"' EXIT

  run_preflight "$SUITE_TMPDIR"
  run_default_suite
  run_metrics_summary "$SUITE_TMPDIR"

  fetch_log_window

  local tier_rc=0
  analyze_audit_tiers "$AUDIT_TAIL_FILE" || tier_rc=$?
  show_log_summary "$AUDIT_TAIL_FILE" "$PROXY_TAIL_FILE"

  if [[ "$tier_rc" != "0" ]]; then
    if [[ "$REQUIRE_ALL_TIERS" == "1" ]]; then
      fail "not all default model tiers appeared in fresh audit records"
    fi
    echo "WARNING: not all default model tiers appeared. This may be normal if Claude Code did not choose every tier for this scenario."
  fi

  step "Integration suite passed"
}

main "$@"
