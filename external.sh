#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVE_SH="$ROOT/serve.sh"
EXPOSE_SH="$ROOT/expose.sh"
READINESS_SH="$ROOT/readiness.sh"
VERSION_FILE="$ROOT/VERSION"
TELEMETRY_FILE="${MUSE_DEMO_TELEMETRY_FILE:-$ROOT/artifacts/runtime-state/demo-observability.env}"
LOG_FILE="${EXTERNAL_LOG_FILE:-$ROOT/artifacts/runtime-state/external.log}"
BACKEND_READY_TIMEOUT="${EXTERNAL_BACKEND_READY_TIMEOUT:-900}"
BACKEND_READY_INTERVAL="${EXTERNAL_BACKEND_READY_INTERVAL:-1}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$ROOT/vendor/bin/cloudflared}"
export CLOUDFLARED_BIN

TARGET_SHA256='0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384'
DRAFT_SHA256='b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef'
RUNTIME_COMMIT='64f765f5adefa4620dddda436ce56f1430435536'
GATEWAY_SCHEMA_VERSION=1
MODEL_NAME='Muse-Glimmer-30B-Q4_K_M'

usage() {
  cat <<'EOT'
Usage: ./external.sh {preflight|start|status|endpoint|local-endpoint|stop|stop-all} [--format text|env|json]

  preflight Validate local external configuration without starting processes.
  start     Ensure canonical backend READY, then start/reuse authenticated exposure.
  status    Print combined backend/exposure lifecycle status.
  --format  For start/status/stop: text (default), env, or json.
  endpoint  Print only the proven HTTPS public endpoint.
  local-endpoint Print only the canonical local auth-proxy endpoint.
  stop      Stop external exposure only; leave backend running.
  stop-all  Stop exposure first, then stop the owned canonical backend.
EOT
}

now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

log_event() {
  local msg="$1"
  mkdir -p "$(dirname "$LOG_FILE")"
  : >> "$LOG_FILE"
  chmod 600 "$LOG_FILE" 2>/dev/null || true
  printf '%s %s\n' "$(now)" "$msg" >> "$LOG_FILE"
}

require_entrypoints() {
  [[ -x "$SERVE_SH" ]] || { echo "ERROR: backend lifecycle entrypoint is missing/not executable: $SERVE_SH" >&2; return 20; }
  [[ -x "$EXPOSE_SH" ]] || { echo "ERROR: exposure lifecycle entrypoint is missing/not executable: $EXPOSE_SH" >&2; return 20; }
  [[ -x "$READINESS_SH" ]] || { echo "ERROR: readiness entrypoint is missing/not executable: $READINESS_SH" >&2; return 20; }
}

env_value() {
  local key="$1" text="$2" line prefix="${1}="
  while IFS= read -r line; do
    if [[ "$line" == "$prefix"* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done <<< "$text"
  return 0
}

read_version() {
  if [[ -f "$VERSION_FILE" ]]; then
    tr -d '\r\n' < "$VERSION_FILE"
  else
    printf 'unknown\n'
  fi
}

parse_format_args() {
  local fmt='text'
  while (( $# )); do
    case "$1" in
      --format)
        shift
        [[ $# -gt 0 ]] || { echo 'ERROR: --format requires text, env, or json' >&2; return 64; }
        fmt="$1"
        ;;
      --format=*) fmt="${1#*=}" ;;
      *) echo "ERROR: unexpected argument: $1" >&2; return 64 ;;
    esac
    shift
  done
  case "$fmt" in text|env|json) ;; *) echo 'ERROR: --format must be text, env, or json' >&2; return 64 ;; esac
  printf '%s\n' "$fmt"
}

best_effort_gpu_count() {
  local helper="$ROOT/scripts/nvidia_runtime.sh"
  if [[ ! -f "$helper" ]]; then printf 'UNKNOWN\n'; return 0; fi
  (
    # shellcheck disable=SC1090
    source "$helper"
    resolve_nvidia_runtime >/dev/null 2>&1 || { printf 'UNKNOWN\n'; exit 0; }
    local count
    count="$($NVIDIA_SMI_PATH --query-gpu=index --format=csv,noheader,nounits 2>/dev/null | awk 'NF{n++} END{print n+0}')"
    [[ "$count" =~ ^[0-9]+$ ]] || count='UNKNOWN'
    printf '%s\n' "$count"
  )
}

telemetry_value() {
  local key="$1" alt="${2:-}" line=''
  [[ -f "$TELEMETRY_FILE" ]] || { printf 'UNKNOWN\n'; return 0; }
  line="$(awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$TELEMETRY_FILE" 2>/dev/null || true)"
  if [[ -z "$line" && -n "$alt" ]]; then
    line="$(awk -F= -v k="$alt" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$TELEMETRY_FILE" 2>/dev/null || true)"
  fi
  [[ -n "$line" ]] && printf '%s\n' "$line" || printf 'UNKNOWN\n'
}

json_from_env() {
  python3 -B -c 'import json,sys; out=dict(raw.rstrip("\n").split("=",1) for raw in sys.stdin if raw.strip() and "=" in raw); print(json.dumps(out,sort_keys=True,separators=(",",":")))'
}

render_env_blob() {
  local fmt="$1" blob="$2"
  case "$fmt" in
    json) printf '%s\n' "$blob" | json_from_env ;;
    text|env) printf '%s\n' "$blob" ;;
    *) echo "ERROR: invalid renderer format: $fmt" >&2; return 64 ;;
  esac
}

READINESS_OUTPUT=''
READINESS_RC=0
run_readiness_gate() {
  local rc=0
  READINESS_OUTPUT="$(EXTERNAL_MODE="$EXPOSURE_MODE" EXPOSURE_MODE="$EXPOSURE_MODE" "$READINESS_SH" --mode "$EXPOSURE_MODE" --format env)"
  rc=$?
  READINESS_RC=$rc
  (( rc == 0 )) || return "$rc"
  [[ "$(env_value START_READINESS "$READINESS_OUTPUT")" == PASS ]] || return 2
  return 0
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

serve_cmd() {
  (
    unset MUSE_API_TOKEN TUNNEL_TOKEN
    "$SERVE_SH" "$@"
  )
}

backend_status() { serve_cmd status 2>&1 || true; }
exposure_status() { "$EXPOSE_SH" status 2>&1 || true; }

backend_identity_ok() {
  local s="$1" profile host port owned prov target draft runtime
  profile="$(env_value PROFILE "$s")"
  host="$(env_value HOST "$s")"
  port="$(env_value PORT "$s")"
  owned="$(env_value OWNED "$s")"
  prov="$(env_value PROVENANCE "$s")"
  target="$(env_value TARGET_SHA256 "$s")"
  draft="$(env_value DRAFT_SHA256 "$s")"
  runtime="$(env_value RUNTIME_COMMIT "$s")"

  [[ "$profile" == dflash2 ]] || return 1
  [[ "$host" == 127.0.0.1 ]] || return 1
  is_uint "$port" && (( port >= 1 && port <= 65535 )) || return 1
  [[ "$owned" == 1 && "$prov" == PASS ]] || return 1
  [[ "$target" == "$TARGET_SHA256" ]] || return 1
  [[ "$draft" == "$DRAFT_SHA256" ]] || return 1
  [[ "$runtime" == "$RUNTIME_COMMIT" ]] || return 1
}

backend_ready_ok() {
  local s="$1"
  [[ "$(env_value STATUS "$s")" == ready ]] || return 1
  [[ "$(env_value HEALTHY "$s")" == 1 ]] || return 1
  backend_identity_ok "$s"
}

backend_starting_ok() {
  local s="$1" healthy
  [[ "$(env_value STATUS "$s")" == starting ]] || return 1
  healthy="$(env_value HEALTHY "$s")"
  [[ "$healthy" == 0 || "$healthy" == 1 ]] || return 1
  backend_identity_ok "$s"
}

exposure_ready_ok() {
  local s="$1" mode status proxy_owned tunnel_owned auth_enabled
  status="$(env_value STATUS "$s")"
  proxy_owned="$(env_value PROXY_OWNED "$s")"
  tunnel_owned="$(env_value TUNNEL_OWNED "$s")"
  auth_enabled="$(env_value AUTH_ENABLED "$s")"
  mode="$(env_value MODE "$s")"

  [[ "$proxy_owned" == 1 && "$auth_enabled" == 1 ]] || return 1
  case "$mode" in
    proxy)
      [[ "$status" == proxy-ready && "$tunnel_owned" == 0 ]] || return 1
      ;;
    quick|named)
      [[ "$status" == ready && "$tunnel_owned" == 1 ]] || return 1
      ;;
    *) return 1 ;;
  esac
}
exposure_stopped_ok() {
  local s="$1"
  [[ "$(env_value STATUS "$s")" == stopped ]] || return 1
  [[ "$(env_value PROXY_OWNED "$s")" == 0 ]] || return 1
  [[ "$(env_value TUNNEL_OWNED "$s")" == 0 ]] || return 1
}

clean_https_origin() {
  local u="$1"
  [[ "$u" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]
}

safe_endpoint() {
  local url
  url="$($EXPOSE_SH endpoint)" || return $?
  clean_https_origin "$url" || { echo 'ERROR: exposure endpoint is not a clean HTTPS origin' >&2; return 54; }
  printf '%s\n' "$url"
}


safe_local_endpoint() {
  local e mode status url
  e="$(exposure_status)"
  mode="$(env_value MODE "$e")"
  status="$(env_value STATUS "$e")"
  [[ "$mode" == proxy && "$status" == proxy-ready ]] || {
    echo 'ERROR: local endpoint is available only for a canonical proxy-ready exposure' >&2
    return 56
  }
  exposure_ready_ok "$e" || {
    echo 'ERROR: proxy exposure is not canonical owned/authenticated' >&2
    return 56
  }
  url="$(env_value PROXY_URL "$e")"
  [[ "$url" =~ ^http://127\.0\.0\.1:[0-9]+$ ]] || {
    echo 'ERROR: proxy local endpoint is not a loopback HTTP origin' >&2
    return 56
  }
  printf '%s\n' "$url"
}
mode_preflight() {
  local mode="${EXTERNAL_MODE:-${EXPOSURE_MODE:-quick}}"
  [[ "$mode" == quick || "$mode" == named || "$mode" == proxy ]] || {
    echo 'ERROR: EXTERNAL_MODE/EXPOSURE_MODE must be quick, named, or proxy' >&2
    return 35
  }
  export EXPOSURE_MODE="$mode"
  return 0
}
credential_preflight() {
  local token="${MUSE_API_TOKEN:-}"
  if (( ${#token} < 32 )) || printf '%s' "$token" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    echo 'ERROR: MUSE_API_TOKEN is required and must be at least 32 printable characters' >&2
    return 20
  fi
  if [[ "$EXPOSURE_MODE" == named ]]; then
    [[ -n "${TUNNEL_TOKEN:-}" ]] || {
      echo 'ERROR: TUNNEL_TOKEN is required for EXTERNAL_MODE=named' >&2
      return 52
    }
    clean_https_origin "${EXPOSURE_PUBLIC_URL:-}" || {
      echo 'ERROR: EXPOSURE_PUBLIC_URL must be a clean HTTPS origin for EXTERNAL_MODE=named' >&2
      return 53
    }
  fi
  return 0
}


cloudflared_preflight() {
  # Quick and Named modes use the repo's pinned, verified binary by default.
  [[ "$EXPOSURE_MODE" == proxy ]] && return 0
  if [[ "$CLOUDFLARED_BIN" == "$ROOT/vendor/bin/cloudflared" ]]; then
    bash "$ROOT/scripts/install_cloudflared.sh" "$CLOUDFLARED_BIN" || return 34
  elif [[ "$CLOUDFLARED_BIN" == */* ]]; then
    [[ -x "$CLOUDFLARED_BIN" ]] || {
      echo "ERROR: cloudflared binary not found/executable: $CLOUDFLARED_BIN" >&2
      return 34
    }
  else
    command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1 || {
      echo "ERROR: cloudflared binary not found: $CLOUDFLARED_BIN" >&2
      return 34
    }
  fi
  return 0
}

validate_preflight() {
  require_entrypoints || return $?
  mode_preflight || return $?
  credential_preflight || return $?
  cloudflared_preflight || return $?
  return 0
}

print_preflight() {
  validate_preflight || return $?
  if [[ "$EXPOSURE_MODE" == proxy ]]; then
    printf 'PREFLIGHT=PASS\n'
    printf 'MODE=proxy\n'
    printf 'API_TOKEN_PRESENT=1\n'
    printf 'TUNNEL_TOKEN_PRESENT=0\n'
    printf 'PUBLIC_URL=\n'
    printf 'CLOUDFLARED_PRESENT=0\n'
    return 0
  fi
  printf 'PREFLIGHT=PASS\n'
  printf 'MODE=%s\n' "$EXPOSURE_MODE"
  printf 'API_TOKEN_PRESENT=1\n'
  if [[ "$EXPOSURE_MODE" == named ]]; then
    printf 'TUNNEL_TOKEN_PRESENT=1\n'
    printf 'PUBLIC_URL=%s\n' "$EXPOSURE_PUBLIC_URL"
  else
    printf 'TUNNEL_TOKEN_PRESENT=0\n'
    printf 'PUBLIC_URL=\n'
  fi
  printf 'CLOUDFLARED_PRESENT=1\n'
}
wait_backend_ready() {
  local s attempts i
  [[ "$BACKEND_READY_TIMEOUT" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || {
    echo 'ERROR: EXTERNAL_BACKEND_READY_TIMEOUT must be a non-negative number' >&2
    return 35
  }
  [[ "$BACKEND_READY_INTERVAL" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || {
    echo 'ERROR: EXTERNAL_BACKEND_READY_INTERVAL must be a positive number' >&2
    return 35
  }
  attempts="$(awk -v t="$BACKEND_READY_TIMEOUT" -v i="$BACKEND_READY_INTERVAL" 'BEGIN { if (i <= 0 || t < 0) exit 2; n=int(t/i); if (n*i < t) n++; if (n < 1) n=1; print n }')" || {
    echo 'ERROR: invalid backend readiness timeout/interval' >&2
    return 35
  }
  for ((i=0; i<=attempts; i++)); do
    s="$(backend_status)"
    if backend_ready_ok "$s"; then
      return 0
    fi
    if ! backend_starting_ok "$s"; then
      echo 'ERROR: backend stopped being canonical owned/proven while waiting for READY' >&2
      return 31
    fi
    if (( i == attempts )); then
      echo "ERROR: canonical backend did not become READY within ${BACKEND_READY_TIMEOUT}s" >&2
      echo "BACKEND_READY_TIMEOUT=$BACKEND_READY_TIMEOUT" >&2
      echo "LAST_BACKEND_STATUS=$(env_value STATUS "$s")" >&2
      echo "LAST_BACKEND_HEALTHY=$(env_value HEALTHY "$s")" >&2
      return 36
    fi
    sleep "$BACKEND_READY_INTERVAL"
  done
}

normalized_status_env() {
  local status_override="${1:-}" next_action="${2:-}" start_result="${3:-}"
  local b e b_status e_status external public='' local_proxy='' mode
  local proxy_owned tunnel_owned auth_enabled proxy_status tunnel_status model gpu_count active busy
  b="$(backend_status)"; e="$(exposure_status)"
  b_status="$(env_value STATUS "$b")"; e_status="$(env_value STATUS "$e")"
  mode="$(env_value MODE "$e")"
  [[ -n "$mode" ]] || mode="${EXPOSURE_MODE:-${EXTERNAL_MODE:-quick}}"
  proxy_owned="$(env_value PROXY_OWNED "$e")"; tunnel_owned="$(env_value TUNNEL_OWNED "$e")"
  auth_enabled="$(env_value AUTH_ENABLED "$e")"

  if backend_ready_ok "$b" && exposure_ready_ok "$e"; then
    if [[ "$mode" == proxy ]]; then
      local_proxy="$(safe_local_endpoint 2>/dev/null || true)"
      [[ -n "$local_proxy" ]] && external=proxy-ready || external=degraded
    else
      public="$(safe_endpoint 2>/dev/null || true)"
      [[ -n "$public" ]] && external=ready || external=degraded
    fi
  elif backend_ready_ok "$b" && [[ "$e_status" == stopped ]]; then
    external=backend-only
  elif [[ "$b_status" == starting || "$e_status" == starting ]]; then
    external=starting
  elif [[ "$b_status" == stopped && "$e_status" == stopped ]]; then
    external=stopped
  else
    external=degraded
  fi

  [[ -n "$status_override" ]] && external="$status_override"
  case "$proxy_owned" in 1) proxy_status=ready ;; 0|'') proxy_status=stopped ;; *) proxy_status=unknown ;; esac
  case "$tunnel_owned" in 1) tunnel_status=ready ;; 0|'') tunnel_status=stopped ;; *) tunnel_status=unknown ;; esac
  model="$MODEL_NAME"
  gpu_count="$(best_effort_gpu_count)"
  active="$(telemetry_value DEMO_ACTIVE_INFERENCE active_inference)"
  busy="$(telemetry_value DEMO_BUSY busy)"
  [[ "$active" != UNKNOWN ]] || active=0
  [[ "$busy" != UNKNOWN ]] || busy=0
  [[ -n "$next_action" ]] || {
    case "$external" in
      ready|proxy-ready) next_action=NONE ;;
      backend-only) next_action=START_EXPOSURE ;;
      stopped) next_action=START ;;
      blocked) next_action=FIX_READINESS_BLOCKERS ;;
      *) next_action=INSPECT_STATUS ;;
    esac
  }

  cat <<EOF
SCHEMA_VERSION=$GATEWAY_SCHEMA_VERSION
VERSION=$(read_version)
STATUS=$external
EXTERNAL_STATUS=$external
MODE=$mode
PUBLIC_URL=$public
AUTH_ENABLED=${auth_enabled:-0}
MODEL=$model
GPU_COUNT=$gpu_count
BACKEND_STATUS=$b_status
BACKEND_HEALTHY=$(env_value HEALTHY "$b")
BACKEND_OWNED=$(env_value OWNED "$b")
BACKEND_PROVENANCE=$(env_value PROVENANCE "$b")
PROXY_STATUS=$proxy_status
TUNNEL_STATUS=$tunnel_status
DEMO_ACTIVE_INFERENCE=$active
DEMO_BUSY=$busy
NEXT_ACTION_CODE=$next_action
EXPOSURE_STATUS=$e_status
PROXY_OWNED=${proxy_owned:-0}
TUNNEL_OWNED=${tunnel_owned:-0}
LOCAL_PROXY_URL=$local_proxy
START_RESULT=$start_result
EOF
}

print_combined_status() {
  local fmt="${1:-text}" status_override="${2:-}" next_action="${3:-}" start_result="${4:-}"
  local blob
  blob="$(normalized_status_env "$status_override" "$next_action" "$start_result")"
  render_env_blob "$fmt" "$blob"
}

render_readiness_blocked() {
  local fmt="$1" rc="$2" next='FIX_READINESS_BLOCKERS'
  local readiness_next
  readiness_next="$(env_value NEXT_ACTION_CODE "$READINESS_OUTPUT")"
  [[ "$readiness_next" == FIX_API_TOKEN ]] && next='SET_API_TOKEN'
  print_combined_status "$fmt" blocked "$next" "READINESS_RC_$rc"
}

start_external() {
  local fmt="${1:-text}"
  validate_preflight || return $?
  log_event 'ACTION=start PHASE=readiness-gate'

  local rrc=0
  set +e
  run_readiness_gate
  rrc=$?
  set -e
  if (( rrc != 0 )); then
    log_event "ACTION=start FINAL_RC=$rrc REASON=readiness-gate"
    render_readiness_blocked "$fmt" "$rrc"
    return "$rrc"
  fi

  log_event 'ACTION=start PHASE=backend-preflight'
  local b e status rc backend_started_here=0 exposure_started_here=0 exposure_was_stopped=0
  b="$(backend_status)"; status="$(env_value STATUS "$b")"
  case "$status" in
    ready)
      backend_ready_ok "$b" || { echo 'ERROR: READY backend is not canonical owned/healthy/proven; refusing orchestration' >&2; log_event 'ACTION=start FINAL_RC=31 REASON=backend-gate'; return 31; }
      log_event 'ACTION=start PHASE=backend-ready BACKEND_ACTION=reuse'
      ;;
    starting)
      backend_starting_ok "$b" || { echo 'ERROR: STARTING backend is not canonical owned/proven; refusing orchestration' >&2; log_event 'ACTION=start FINAL_RC=31 REASON=backend-gate'; return 31; }
      log_event 'ACTION=start PHASE=backend-start BACKEND_ACTION=wait-existing'
      wait_backend_ready || { rc=$?; log_event "ACTION=start FINAL_RC=$rc REASON=backend-wait"; return "$rc"; }
      log_event 'ACTION=start PHASE=backend-ready BACKEND_ACTION=wait-existing'
      ;;
    stopped)
      log_event 'ACTION=start PHASE=backend-start BACKEND_ACTION=start'
      if serve_cmd start >/dev/null; then backend_started_here=1; else rc=$?; log_event "ACTION=start FINAL_RC=$rc REASON=serve-start"; return "$rc"; fi
      b="$(backend_status)"
      if ! backend_ready_ok "$b"; then
        echo 'ERROR: serve.sh start returned success without canonical READY backend' >&2
        if (( backend_started_here )); then serve_cmd stop >/dev/null 2>&1 || true; fi
        log_event 'ACTION=start FINAL_RC=42 REASON=post-serve-gate'
        return 42
      fi
      log_event 'ACTION=start PHASE=backend-ready BACKEND_ACTION=start'
      ;;
    *)
      echo "ERROR: backend state is not safely orchestratable: STATUS=${status:-unknown}" >&2
      log_event 'ACTION=start FINAL_RC=31 REASON=backend-state'
      return 31
      ;;
  esac

  e="$(exposure_status)"
  if exposure_ready_ok "$e"; then
    if [[ "$(env_value MODE "$e")" != "$EXPOSURE_MODE" ]]; then
      echo 'ERROR: existing READY exposure mode does not match requested mode' >&2
      if (( backend_started_here )); then serve_cmd stop >/dev/null 2>&1 || true; fi
      log_event 'ACTION=start FINAL_RC=55 REASON=exposure-mode-mismatch'
      return 55
    fi
    if [[ "$EXPOSURE_MODE" != proxy ]]; then
      set +e; safe_endpoint >/dev/null; rc=$?; set -e
      if (( rc != 0 )); then
        echo 'ERROR: existing READY exposure has no valid clean HTTPS endpoint' >&2
        if (( backend_started_here )); then serve_cmd stop >/dev/null 2>&1 || true; fi
        log_event "ACTION=start FINAL_RC=$rc REASON=endpoint-gate"
        return "$rc"
      fi
    fi
    log_event 'ACTION=start PHASE=exposure-ready EXPOSURE_ACTION=reuse'
    print_combined_status "$fmt" '' '' ALREADY_READY
    log_event 'ACTION=start FINAL_RC=0'
    return 0
  fi
  exposure_stopped_ok "$e" && exposure_was_stopped=1 || true

  log_event 'ACTION=start PHASE=exposure-start EXPOSURE_ACTION=start'
  if "$EXPOSE_SH" start >/dev/null; then
    exposure_started_here=1
  else
    rc=$?
    if (( exposure_was_stopped )); then "$EXPOSE_SH" stop >/dev/null 2>&1 || true; fi
    if (( backend_started_here )); then serve_cmd stop >/dev/null 2>&1 || true; fi
    log_event "ACTION=start FINAL_RC=$rc REASON=expose-start"
    return "$rc"
  fi
  e="$(exposure_status)"
  if ! exposure_ready_ok "$e"; then
    echo 'ERROR: expose.sh start returned success without canonical READY exposure' >&2
    (( exposure_started_here )) && "$EXPOSE_SH" stop >/dev/null 2>&1 || true
    (( backend_started_here )) && serve_cmd stop >/dev/null 2>&1 || true
    log_event 'ACTION=start FINAL_RC=52 REASON=post-expose-gate'
    return 52
  fi
  if [[ "$EXPOSURE_MODE" != proxy ]]; then
    set +e; safe_endpoint >/dev/null; rc=$?; set -e
    if (( rc != 0 )); then
      (( exposure_started_here )) && "$EXPOSE_SH" stop >/dev/null 2>&1 || true
      (( backend_started_here )) && serve_cmd stop >/dev/null 2>&1 || true
      log_event "ACTION=start FINAL_RC=$rc REASON=endpoint-gate"
      return "$rc"
    fi
  fi
  log_event 'ACTION=start PHASE=exposure-ready EXPOSURE_ACTION=start'
  print_combined_status "$fmt" '' '' STARTED
  log_event 'ACTION=start FINAL_RC=0'
}

stop_external() {
  local fmt="${1:-text}"
  require_entrypoints || return $?
  log_event 'ACTION=stop PHASE=exposure-stop'
  local rc e
  if "$EXPOSE_SH" stop >/dev/null; then :; else rc=$?; log_event "ACTION=stop FINAL_RC=$rc REASON=expose-stop"; return "$rc"; fi
  e="$(exposure_status)"
  exposure_stopped_ok "$e" || { echo 'ERROR: exposure did not reach canonical stopped state' >&2; log_event 'ACTION=stop FINAL_RC=62 REASON=exposure-not-stopped'; return 62; }
  print_combined_status "$fmt"
  log_event 'ACTION=stop FINAL_RC=0'
}

stop_all() {
  local fmt="${1:-text}"
  require_entrypoints || return $?
  log_event 'ACTION=stop-all PHASE=exposure-stop'
  local rc e b status
  if "$EXPOSE_SH" stop >/dev/null; then :; else rc=$?; log_event "ACTION=stop-all FINAL_RC=$rc REASON=expose-stop"; return "$rc"; fi
  e="$(exposure_status)"
  exposure_stopped_ok "$e" || { echo 'ERROR: exposure did not reach canonical stopped state' >&2; log_event 'ACTION=stop-all FINAL_RC=62 REASON=exposure-not-stopped'; return 62; }

  b="$(backend_status)"; status="$(env_value STATUS "$b")"
  case "$status" in
    stopped)
      ;;
    ready)
      backend_ready_ok "$b" || { echo 'ERROR: refusing to stop non-canonical/foreign READY backend' >&2; log_event 'ACTION=stop-all FINAL_RC=31 REASON=backend-gate'; return 31; }
      log_event 'ACTION=stop-all PHASE=backend-stop BACKEND_ACTION=stop'
      if serve_cmd stop >/dev/null; then :; else rc=$?; log_event "ACTION=stop-all FINAL_RC=$rc REASON=serve-stop"; return "$rc"; fi
      ;;
    starting)
      backend_starting_ok "$b" || { echo 'ERROR: refusing to stop non-canonical/foreign STARTING backend' >&2; log_event 'ACTION=stop-all FINAL_RC=31 REASON=backend-gate'; return 31; }
      log_event 'ACTION=stop-all PHASE=backend-stop BACKEND_ACTION=stop'
      if serve_cmd stop >/dev/null; then :; else rc=$?; log_event "ACTION=stop-all FINAL_RC=$rc REASON=serve-stop"; return "$rc"; fi
      ;;
    *)
      echo "ERROR: refusing to stop backend with unsafe state: STATUS=${status:-unknown}" >&2
      log_event 'ACTION=stop-all FINAL_RC=31 REASON=backend-state'
      return 31
      ;;
  esac
  b="$(backend_status)"
  [[ "$(env_value STATUS "$b")" == stopped && "$(env_value OWNED "$b")" == 0 ]] || {
    echo 'ERROR: backend did not reach stopped/unowned state' >&2
    log_event 'ACTION=stop-all FINAL_RC=63 REASON=backend-not-stopped'
    return 63
  }
  print_combined_status "$fmt"
  log_event 'ACTION=stop-all FINAL_RC=0'
}

main() {
  local cmd="${1:-}" fmt='text'
  [[ $# -gt 0 ]] && shift || true
  case "$cmd" in
    preflight)
      [[ $# -eq 0 ]] || { usage >&2; return 64; }
      print_preflight
      ;;
    start|status|stop)
      fmt="$(parse_format_args "$@")" || return $?
      case "$cmd" in
        start) start_external "$fmt" ;;
        status) require_entrypoints && print_combined_status "$fmt" ;;
        stop) stop_external "$fmt" ;;
      esac
      ;;
    endpoint)
      [[ $# -eq 0 ]] || { usage >&2; return 64; }
      require_entrypoints && safe_endpoint
      ;;
    local-endpoint)
      [[ $# -eq 0 ]] || { usage >&2; return 64; }
      require_entrypoints && safe_local_endpoint
      ;;
    stop-all)
      fmt="$(parse_format_args "$@")" || return $?
      stop_all "$fmt"
      ;;
    -h|--help|help) usage ;;
    *) usage >&2; return 64 ;;
  esac
}

main "$@"

# proxy/BYO mode
