#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLL_SECONDS="${AGENT_SUPERVISOR_POLL_SECONDS:-15}"
MAX_START_BACKOFF="${AGENT_SUPERVISOR_MAX_BACKOFF_SECONDS:-60}"
STOP_REQUESTED=0

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
request_stop() { STOP_REQUESTED=1; log "SUPERVISOR_STOP_REQUESTED signal=$1"; }
trap 'request_stop TERM' TERM
trap 'request_stop INT' INT

status_value() {
  local key="$1" data="$2"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' <<<"$data"
}

stack_is_healthy() {
  local data="$1" external backend healthy owned provenance auth
  external="$(status_value STATUS "$data")"
  backend="$(status_value BACKEND_STATUS "$data")"
  healthy="$(status_value BACKEND_HEALTHY "$data")"
  owned="$(status_value BACKEND_OWNED "$data")"
  provenance="$(status_value BACKEND_PROVENANCE "$data")"
  auth="$(status_value AUTH_ENABLED "$data")"
  [[ "$external" == ready || "$external" == proxy-ready ]] &&
    [[ "$backend" == ready ]] && [[ "$healthy" == 1 ]] &&
    [[ "$owned" == 1 ]] && [[ "$provenance" == PASS ]] &&
    [[ "$auth" == 1 ]]
}

stop_stack() {
  log "SUPERVISOR_STOPPING_STACK"
  "$ROOT/external.sh" stop-all --format text || true
}

wait_or_stop() {
  local seconds="$1" left="$1"
  while (( left > 0 && STOP_REQUESTED == 0 )); do
    sleep 1 & wait $! || true
    left=$((left - 1))
  done
}

backoff=5
while (( STOP_REQUESTED == 0 )); do
  log "SUPERVISOR_START_ATTEMPT backoff_seconds=$backoff"
  if "$ROOT/external.sh" start --format text; then
    log "SUPERVISOR_START_COMMAND=PASS"
    failures=0
    while (( STOP_REQUESTED == 0 )); do
      wait_or_stop "$POLL_SECONDS"
      (( STOP_REQUESTED == 1 )) && break
      status_data="$("$ROOT/external.sh" status --format env 2>/dev/null)" || status_data=""
      if stack_is_healthy "$status_data"; then
        if (( failures > 0 )); then log "SUPERVISOR_STACK_RECOVERED"; fi
        failures=0
        backoff=5
      else
        failures=$((failures + 1))
        log "SUPERVISOR_HEALTH_CHECK_FAILED failures=$failures"
        if (( failures >= 2 )); then
          break
        fi
      fi
    done
  else
    log "SUPERVISOR_START_COMMAND=FAILED"
  fi

  (( STOP_REQUESTED == 1 )) && break
  stop_stack
  wait_or_stop "$backoff"
  backoff=$((backoff * 2))
  (( backoff > MAX_START_BACKOFF )) && backoff="$MAX_START_BACKOFF"
done

stop_stack
log "SUPERVISOR_EXIT=PASS"
