#!/usr/bin/env bash
set -Eeuo pipefail
# Secrets are environment-only; force xtrace off before reading or launching with them.
set +x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/common.sh"
export_effective_config
STATE_DIR="${RUNTIME_STATE_DIR:-$ROOT/artifacts/runtime-state}"
TUNNEL_TOKEN_FILE="$STATE_DIR/named-tunnel-token"
STATE_FILE="$STATE_DIR/exposure.json"
PROXY_LOG="$STATE_DIR/exposure-proxy.log"
TUNNEL_LOG="$STATE_DIR/exposure-tunnel.log"
PROXY_HOST="127.0.0.1"
PROXY_PORT="${EXPOSURE_PROXY_PORT:-8090}"
MODE="${EXPOSURE_MODE:-quick}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$ROOT/vendor/bin/cloudflared}"
READY_TIMEOUT="${EXPOSURE_READY_TIMEOUT:-45}"
BACKEND_READY_TIMEOUT="${EXPOSURE_BACKEND_READY_TIMEOUT:-900}"
BACKEND_READY_INTERVAL="${EXPOSURE_BACKEND_READY_INTERVAL:-1}"
PUBLIC_READY_TIMEOUT="${EXPOSURE_PUBLIC_READY_TIMEOUT:-}"
PUBLIC_PROBE_INTERVAL="${EXPOSURE_PUBLIC_PROBE_INTERVAL:-1}"
STOP_TIMEOUT="${EXPOSURE_STOP_TIMEOUT:-5}"
PUBLIC_PROBE_BIN="${EXPOSURE_PUBLIC_PROBE_BIN:-}"
LOCAL_PROBE_BIN="${EXPOSURE_LOCAL_PROBE_BIN:-}"

STARTING=0
PROXY_PID=""
TUNNEL_PID=""

state_status(){ python3 "$ROOT/scripts/exposure_state.py" status --state "$STATE_FILE" --format env; }

state_value(){
  local key="$1" text="$2"
  awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/,""); print; exit}' <<<"$text"
}

require_token(){
  python3 - <<'PY'
import os,sys
s=os.environ.get('MUSE_API_TOKEN','')
ok=len(s)>=32 and all(ord(c)>=32 and ord(c)!=127 for c in s)
if not ok:
    print('ERROR: MUSE_API_TOKEN is required and must be at least 32 characters with no control characters', file=sys.stderr)
    raise SystemExit(20)
PY
}

probe_url(){
  local mode="$1" url="$2"
  if [[ -n "$LOCAL_PROBE_BIN" ]]; then
    "$LOCAL_PROBE_BIN" "$mode" "$url"
    return
  fi
  python3 - "$mode" "$url" <<'PY'
import os,sys,urllib.error,urllib.parse,urllib.request
mode,url=sys.argv[1:3]
u=urllib.parse.urlsplit(url)
if u.scheme == 'http' and u.hostname not in {'127.0.0.1','localhost','::1'}:
    print('000'); raise SystemExit(0)
if u.scheme not in {'http','https'}:
    print('000'); raise SystemExit(0)
headers={}
if mode == 'auth':
    token=os.environ.get('MUSE_API_TOKEN','')
    if len(token)<32:
        print('000'); raise SystemExit(0)
    headers['Authorization']='Bearer '+token
elif mode != 'unauth':
    print('000'); raise SystemExit(0)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None
opener=urllib.request.build_opener(NoRedirect)
try:
    with opener.open(urllib.request.Request(url, headers=headers), timeout=4) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except (urllib.error.URLError, TimeoutError, OSError, ValueError):
    print('000')
PY
}

public_probe(){
  local mode="$1" url="$2"
  if [[ -n "$PUBLIC_PROBE_BIN" ]]; then
    "$PUBLIC_PROBE_BIN" "$mode" "$url"
  else
    probe_url "$mode" "$url"
  fi
}

backend_gate(){
  local serve_status guard starting_guard deadline now
  deadline="$(python3 - "$BACKEND_READY_TIMEOUT" <<'PY'
import sys,time; print(time.monotonic()+float(sys.argv[1]))
PY
)"
  while :; do
    if ! serve_status="$($ROOT/serve.sh status 2>&1)"; then
      printf '%s\n' "$serve_status" >&2
      echo 'ERROR: persistent backend status command failed; refusing external exposure' >&2
      return 30
    fi
    if guard="$(printf '%s\n' "$serve_status" | python3 "$ROOT/scripts/exposure_guard.py" validate-backend 2>&1)"; then
      printf '%s\n' "$guard"
      return 0
    fi
    if ! starting_guard="$(printf '%s\n' "$serve_status" | python3 "$ROOT/scripts/exposure_guard.py" validate-backend-starting 2>&1)"; then
      printf '%s\n' "$guard" >&2
      echo 'ERROR: persistent backend is not canonical READY/proven; refusing external exposure' >&2
      return 31
    fi
    now="$(python3 - <<'PY'
import time; print(time.monotonic())
PY
)"
    if ! python3 - "$now" "$deadline" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)
PY
    then
      echo 'ERROR: timed out waiting for canonical persistent backend readiness' >&2
      printf 'BACKEND_READY_TIMEOUT=%s\n' "$BACKEND_READY_TIMEOUT" >&2
      printf 'LAST_BACKEND_STATUS=%s\n' "$(state_value STATUS "$serve_status")" >&2
      printf 'LAST_BACKEND_HEALTHY=%s\n' "$(state_value HEALTHY "$serve_status")" >&2
      return 36
    fi
    sleep "$BACKEND_READY_INTERVAL"
  done
}

port_in_use(){
  python3 - "$PROXY_HOST" "$PROXY_PORT" <<'PY'
import socket,sys
host=sys.argv[1]; port=int(sys.argv[2])
s=socket.socket(); s.settimeout(.15)
try:
    rc=s.connect_ex((host,port))
finally:
    s.close()
raise SystemExit(0 if rc == 0 else 1)
PY
}

wait_local_proxy(){
  local deadline now unauth auth
  deadline="$(python3 - "$READY_TIMEOUT" <<'PY'
import sys,time
print(time.monotonic()+float(sys.argv[1]))
PY
)"
  while :; do
    if [[ -n "$PROXY_PID" ]] && ! kill -0 "$PROXY_PID" 2>/dev/null; then
      echo 'ERROR: auth proxy exited before readiness' >&2
      tail -n 40 "$PROXY_LOG" >&2 2>/dev/null || true
      return 40
    fi
    unauth="$(probe_url unauth "http://$PROXY_HOST:$PROXY_PORT/health")"
    auth="$(probe_url auth "http://$PROXY_HOST:$PROXY_PORT/health")"
    if [[ "$unauth" == 401 && "$auth" =~ ^2[0-9][0-9]$ ]]; then return 0; fi
    now="$(python3 - <<'PY'
import time; print(time.monotonic())
PY
)"
    python3 - "$now" "$deadline" <<'PY' || return 41
import sys
raise SystemExit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)
PY
    sleep .05
  done
}

wait_quick_url(){
  local deadline now url
  deadline="$(python3 - "$READY_TIMEOUT" <<'PY'
import sys,time; print(time.monotonic()+float(sys.argv[1]))
PY
)"
  while :; do
    [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null || {
      echo 'ERROR: cloudflared exited before publishing a Quick Tunnel URL' >&2
      tail -n 60 "$TUNNEL_LOG" >&2 2>/dev/null || true
      return 50
    }
    url="$(grep -Eo 'https://[A-Za-z0-9.-]+\.trycloudflare\.(com|app)' "$TUNNEL_LOG" 2>/dev/null | tail -n1 || true)"
    if [[ -n "$url" ]]; then printf '%s\n' "$url"; return 0; fi
    now="$(python3 - <<'PY'
import time; print(time.monotonic())
PY
)"
    python3 - "$now" "$deadline" <<'PY' || {
import sys
raise SystemExit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)
PY
      echo 'ERROR: timed out waiting for Quick Tunnel HTTPS URL' >&2; tail -n 60 "$TUNNEL_LOG" >&2 2>/dev/null || true; return 51;
    }
    sleep .05
  done
}

wait_public_auth(){
  local public_url="$1" timeout deadline now unauth auth
  local seen_unauth_401=0 seen_auth_2xx=0
  local last_unauth=000 last_auth=000
  [[ "$public_url" == https://* ]] || { echo 'ERROR: public URL must use HTTPS' >&2; return 60; }

  timeout="$PUBLIC_READY_TIMEOUT"
  if [[ -z "$timeout" ]]; then
    if [[ "$MODE" == quick ]]; then timeout=90; else timeout="$READY_TIMEOUT"; fi
  fi
  deadline="$(python3 - "$timeout" <<'PY'
import sys,time; print(time.monotonic()+float(sys.argv[1]))
PY
)"

  while :; do
    [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null || { echo 'ERROR: tunnel process exited during public readiness' >&2; return 61; }

    if ! unauth="$(public_probe unauth "${public_url%/}/health")"; then unauth=000; fi
    if ! auth="$(public_probe auth "${public_url%/}/health")"; then auth=000; fi
    [[ "$unauth" =~ ^[0-9][0-9][0-9]$ ]] || unauth=000
    [[ "$auth" =~ ^[0-9][0-9][0-9]$ ]] || auth=000
    last_unauth="$unauth"
    last_auth="$auth"

    [[ "$unauth" == 401 ]] && seen_unauth_401=1
    [[ "$auth" =~ ^2[0-9][0-9]$ ]] && seen_auth_2xx=1
    if [[ "$seen_unauth_401" == 1 && "$seen_auth_2xx" == 1 ]]; then return 0; fi

    now="$(python3 - <<'PY'
import time; print(time.monotonic())
PY
)"
    if ! python3 - "$now" "$deadline" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)
PY
    then
      echo 'ERROR: timed out waiting for authenticated public readiness' >&2
      printf 'LAST_UNAUTH_CODE=%s\n' "$last_unauth" >&2
      printf 'LAST_AUTH_CODE=%s\n' "$last_auth" >&2
      printf 'SEEN_UNAUTH_401=%s\n' "$seen_unauth_401" >&2
      printf 'SEEN_AUTH_2XX=%s\n' "$seen_auth_2xx" >&2
      printf 'PUBLIC_READY_TIMEOUT=%s\n' "$timeout" >&2
      return 62
    fi
    sleep "$PUBLIC_PROBE_INTERVAL"
  done
}

terminate_started_pid_bounded(){
  local pid="${1:-}" deadline now state
  [[ -n "$pid" ]] || return 0
  kill "$pid" 2>/dev/null || return 0
  deadline="$(python3 - "$STOP_TIMEOUT" <<'PY'
import sys,time
print(time.monotonic()+max(0.0,float(sys.argv[1])))
PY
)"
  while kill -0 "$pid" 2>/dev/null; do
    state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}' || true)"
    [[ -z "$state" || "$state" == Z* ]] && return 0
    now="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
    if ! python3 - "$now" "$deadline" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)
PY
    then
      kill -KILL "$pid" 2>/dev/null || true
      return 0
    fi
    sleep .05
  done
}

cleanup_partial(){
  set +e
  # exposure_state stop-owned is already bounded. Never follow it with an
  # unbounded shell `wait`: a stubborn/slow child must not wedge start failure.
  if [[ -f "$STATE_FILE" ]]; then
    python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT" >/dev/null 2>&1 || true
  fi
  # STARTING=1 only covers processes launched by this invocation, so these PIDs
  # are safe to terminate directly. Reaping is intentionally left to shell exit.
  terminate_started_pid_bounded "$TUNNEL_PID"
  terminate_started_pid_bounded "$PROXY_PID"
  rm -f "$TUNNEL_TOKEN_FILE"
  set -e
}

on_exit(){
  local rc=$?
  if [[ "$STARTING" == 1 && "$rc" != 0 ]]; then cleanup_partial; fi
  exit "$rc"
}
trap on_exit EXIT

start_exposure(){
  local gate backend_url existing status public_url proxy_url tunnel_token
  require_token
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR" 2>/dev/null || true

  if gate="$(backend_gate)"; then
    :
  else
    local gate_rc=$?
    # If an old owned exposure exists while backend provenance is bad, close ingress fail-closed.
    [[ -f "$STATE_FILE" ]] && python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT" >/dev/null 2>&1 || true
    return "$gate_rc"
  fi
  backend_url="$(state_value BACKEND_URL "$gate")"
  [[ -n "$backend_url" ]] || { echo 'ERROR: backend gate returned no BACKEND_URL' >&2; return 32; }
  proxy_url="http://$PROXY_HOST:$PROXY_PORT"

  existing="$(state_status)"
  status="$(state_value STATUS "$existing")"
  if [[ "$status" == ready || "$status" == proxy-ready ]]; then
    if [[ "$(state_value MODE "$existing")" != "$MODE" ]]; then
      echo 'ERROR: existing exposure mode does not match requested mode' >&2
      return 55
    fi
  fi
  if [[ "$status" == proxy-ready && "$(state_value PROXY_OWNED "$existing")" == 1 && "$(state_value TUNNEL_OWNED "$existing")" == 0 ]]; then
    PROXY_PID="$(state_value PROXY_PID "$existing")"
    TUNNEL_PID=""
    if wait_local_proxy; then
      echo 'Authenticated local ingress already PROXY-READY.'
      printf '%s\n' "$existing"
      return 0
    fi
    echo 'Owned proxy exposure exists but local readiness no longer passes; restarting safely.' >&2
    python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT" >/dev/null
    existing="$(state_status)"; status="$(state_value STATUS "$existing")"
  fi

  if [[ "$status" == ready && "$(state_value PROXY_OWNED "$existing")" == 1 && "$(state_value TUNNEL_OWNED "$existing")" == 1 ]]; then
    public_url="$(state_value PUBLIC_URL "$existing")"
    PROXY_PID="$(state_value PROXY_PID "$existing")"
    TUNNEL_PID="$(state_value TUNNEL_PID "$existing")"
    if [[ "$(probe_url unauth "$proxy_url/health")" == 401 ]] \
       && [[ "$(probe_url auth "$proxy_url/health")" =~ ^2[0-9][0-9]$ ]] \
       && wait_public_auth "$public_url"; then
      echo 'External exposure already READY.'
      printf '%s\n' "$existing"
      return 0
    fi
    echo 'Owned exposure exists but readiness no longer passes; restarting safely.' >&2
    python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT" >/dev/null
  elif [[ "$status" != stopped ]]; then
    python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT" >/dev/null || true
  fi

  if port_in_use; then
    echo "ERROR: proxy port $PROXY_HOST:$PROXY_PORT is already owned by another listener" >&2
    return 33
  fi
  [[ "$MODE" == quick || "$MODE" == named || "$MODE" == proxy ]] || { echo 'ERROR: EXPOSURE_MODE must be quick, named, or proxy' >&2; return 35; }
  if [[ "$MODE" != proxy ]]; then
    if [[ "$CLOUDFLARED_BIN" == "$ROOT/vendor/bin/cloudflared" ]]; then
      bash "$ROOT/scripts/install_cloudflared.sh" "$CLOUDFLARED_BIN" || return 34
    else
      command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1 || { echo "ERROR: cloudflared binary not found: $CLOUDFLARED_BIN" >&2; return 34; }
    fi
  fi

  # Use the actual context that llama-server successfully loaded (including an OOM fallback).
  local metadata_file="${RUNTIME_STATE_DIR:-$ROOT/artifacts/runtime-state}/server-metadata.json"
  local -a runtime_limits=()
  mapfile -t runtime_limits < <(python3 - "$metadata_file" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    context=int(d["context_size"])
    output=int(d.get("max_tokens",context))
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    print("ERROR: missing/invalid actual server context metadata", file=sys.stderr)
    raise SystemExit(2)
if context < 1 or output < 1:
    raise SystemExit("ERROR: invalid actual server context/output metadata")
print(context)
print(min(context, output, 131072))
PY
  )
  [[ "${#runtime_limits[@]}" -eq 2 ]] || { echo "ERROR: could not determine effective server Context/output budget." >&2; return 36; }
  local effective_context="${runtime_limits[0]}" effective_output="${runtime_limits[1]}"
  echo "GATEWAY_EFFECTIVE_CONTEXT_SIZE=$effective_context"
  echo "GATEWAY_EFFECTIVE_MAX_OUTPUT_TOKENS=$effective_output"

  : >"$PROXY_LOG"; chmod 600 "$PROXY_LOG" 2>/dev/null || true
  TUNNEL_PID=""
  if [[ "$MODE" != proxy ]]; then : >"$TUNNEL_LOG"; chmod 600 "$TUNNEL_LOG" 2>/dev/null || true; fi
  STARTING=1
  MUSE_API_TOKEN="$MUSE_API_TOKEN" nohup python3 "$ROOT/scripts/auth_proxy.py" \
    --listen-host "$PROXY_HOST" --listen-port "$PROXY_PORT" \
    --backend-host 127.0.0.1 --backend-port "${backend_url##*:}" \
    --effective-context-size "$effective_context" --max-output-tokens "$effective_output" \
    --max-request-bytes "${MAX_REQUEST_BYTES:-33554432}" \
    --queue-size "${ADMISSION_QUEUE_SIZE:-8}" \
    --queue-wait-seconds "${ADMISSION_QUEUE_WAIT_SECONDS:-600}" \
    --idle-timeout "${GATEWAY_IDLE_TIMEOUT:-180}" \
    --hard-deadline "${GATEWAY_HARD_DEADLINE:-3600}" \
    >>"$PROXY_LOG" 2>&1 &
  PROXY_PID=$!
  wait_local_proxy || return $?

  if [[ "$MODE" == proxy ]]; then
    python3 "$ROOT/scripts/exposure_state.py" begin --state "$STATE_FILE" --mode proxy \
      --backend-url "$backend_url" --proxy-url "$proxy_url" \
      --proxy-pid "$PROXY_PID" --proxy-command-token auth_proxy.py \
      --proxy-log "$PROXY_LOG"
    python3 "$ROOT/scripts/exposure_state.py" mark-proxy-ready --state "$STATE_FILE"
    STARTING=0
    echo 'Authenticated local ingress STARTED / PROXY-READY.'
    state_status
    return 0
  fi

  if [[ "$MODE" == quick ]]; then
    NO_AUTOUPDATE=true nohup "$CLOUDFLARED_BIN" tunnel --url "$proxy_url" >>"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    public_url="$(wait_quick_url)" || return $?
  else
    tunnel_token="${TUNNEL_TOKEN:-}"
    [[ -n "$tunnel_token" ]] || { echo 'ERROR: TUNNEL_TOKEN is required for EXPOSURE_MODE=named' >&2; return 52; }
    public_url="${EXPOSURE_PUBLIC_URL:-}"
    [[ -n "$public_url" ]] || { echo 'ERROR: EXPOSURE_PUBLIC_URL=https://... is required for named tunnel' >&2; return 53; }
    local public_gate
    if ! public_gate="$(printf '%s\n' "$public_url" | python3 "$ROOT/scripts/exposure_guard.py" validate-public-url 2>&1)"; then
      printf '%s\n' "$public_gate" >&2
      return 53
    fi
    public_url="$(state_value PUBLIC_URL "$public_gate")"
    [[ -n "$public_url" ]] || { echo 'ERROR: named public URL gate returned no canonical URL' >&2; return 53; }
    # Keep the named-tunnel credential out of argv and cloudflared's process environment.
    install -m 600 /dev/null "$TUNNEL_TOKEN_FILE"
    printf '%s' "$tunnel_token" > "$TUNNEL_TOKEN_FILE"
    chmod 600 "$TUNNEL_TOKEN_FILE"
    unset TUNNEL_TOKEN tunnel_token
    NO_AUTOUPDATE=true nohup "$CLOUDFLARED_BIN" tunnel run --token-file "$TUNNEL_TOKEN_FILE" >>"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
  fi

  python3 "$ROOT/scripts/exposure_state.py" begin --state "$STATE_FILE" --mode "$MODE" \
    --backend-url "$backend_url" --proxy-url "$proxy_url" \
    --proxy-pid "$PROXY_PID" --proxy-command-token auth_proxy.py \
    --tunnel-pid "$TUNNEL_PID" --tunnel-command-token "$(basename "$CLOUDFLARED_BIN")" \
    --proxy-log "$PROXY_LOG" --tunnel-log "$TUNNEL_LOG"

  wait_public_auth "$public_url" || return $?
  python3 "$ROOT/scripts/exposure_state.py" mark-ready --state "$STATE_FILE" --public-url "$public_url"
  STARTING=0
  echo 'Secure external exposure STARTED / READY.'
  state_status
}

stop_exposure(){
  mkdir -p "$STATE_DIR"
  if [[ ! -f "$STATE_FILE" ]]; then
    rm -f "$TUNNEL_TOKEN_FILE"
    echo 'External exposure already STOPPED.'
    state_status
    return 0
  fi
  python3 "$ROOT/scripts/exposure_state.py" stop-owned --state "$STATE_FILE" --timeout "$STOP_TIMEOUT"
  rm -f "$TUNNEL_TOKEN_FILE"
  echo 'Secure external exposure STOPPED.'
  state_status
}

status_exposure(){ state_status; }

endpoint_exposure(){
  local current status url public_gate
  current="$(state_status)"
  status="$(state_value STATUS "$current")"
  if [[ "$status" != ready || "$(state_value PROXY_OWNED "$current")" != 1 || "$(state_value TUNNEL_OWNED "$current")" != 1 || "$(state_value AUTH_ENABLED "$current")" != 1 ]]; then
    echo 'ERROR: secure external exposure is not READY/owned/authenticated' >&2
    return 70
  fi
  url="$(state_value PUBLIC_URL "$current")"
  if ! public_gate="$(printf '%s\n' "$url" | python3 "$ROOT/scripts/exposure_guard.py" validate-public-url 2>&1)"; then
    printf '%s\n' "$public_gate" >&2
    return 71
  fi
  state_value PUBLIC_URL "$public_gate"
}

case "${1:-}" in
  start) start_exposure ;;
  status) status_exposure ;;
  endpoint) endpoint_exposure ;;
  stop) stop_exposure ;;
  restart) stop_exposure >/dev/null; start_exposure ;;
  *) echo 'Usage: ./expose.sh {start|status|stop|restart|endpoint}' >&2; exit 2 ;;
esac

# proxy/BYO exposure
