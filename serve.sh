#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT_ROOT/scripts/common.sh"
source "$PROJECT_ROOT/scripts/run_lock.sh"
source "$PROJECT_ROOT/scripts/process_utils.sh"

ACTION="${1:-status}"
SERVE_PROFILE="${SERVE_PROFILE:-dflash2}"
SERVER_STATE="$RUNTIME_STATE_DIR/server.json"
SERVER_LOG="$RUNTIME_STATE_DIR/server.log"
SERVER_METADATA="$RUNTIME_STATE_DIR/server-metadata.json"
CONTROL_LOCK="$RUNTIME_STATE_DIR/server-control.lock"
STATE_HELPER="$PROJECT_ROOT/scripts/server_state.py"
CONTROL_LOCK_ACQUIRED=0

CANONICAL_TARGET_FILE='Muse-Glimmer-30B-Q4_K_M.gguf'
CANONICAL_TARGET_SIZE='17306324000'
CANONICAL_TARGET_SHA='0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384'
CANONICAL_DRAFT_FILE='Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf'
CANONICAL_DRAFT_SIZE='1645657280'
CANONICAL_DRAFT_SHA='93dbfb6f88e4645dec1347cf93f9d6fc80b90d413038722385b2a8e53565c949'
CANONICAL_RUNTIME_COMMIT='64f765f5adefa4620dddda436ce56f1430435536'

usage() {
  cat <<'EOF'
Usage: ./serve.sh start|status|stop|restart

Environment:
  SERVE_PROFILE=dflash2   # default; use baseline explicitly to disable DFlash2
  SERVER_PORT=8088        # loopback only
EOF
}

json_field() {
  local key="$1"
  python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else str(v).lower() if isinstance(v,bool) else v)' "$key"
}

acquire_control_lock() {
  if acquire_run_lock "$CONTROL_LOCK"; then
    CONTROL_LOCK_ACQUIRED=1
  else
    exit 12
  fi
}

release_control_lock() {
  if (( CONTROL_LOCK_ACQUIRED == 1 )); then
    release_run_lock "$CONTROL_LOCK" || true
    CONTROL_LOCK_ACQUIRED=0
  fi
}
trap release_control_lock EXIT

state_json() {
  python3 "$STATE_HELPER" status --state "$SERVER_STATE" --format json
}

state_provenance() {
  local report="$1"
  python3 -c '
import json,sys
d=json.load(sys.stdin)
profile=d.get("profile")
if not profile:
    print("NA")
    raise SystemExit(0)
ok=(d.get("target_sha256")==sys.argv[1] and d.get("runtime_commit")==sys.argv[3])
if profile=="dflash2":
    ok=ok and d.get("draft_sha256")==sys.argv[2]
elif profile=="baseline":
    ok=ok and not d.get("draft_sha256")
else:
    ok=False
print("PASS" if ok else "FAIL")
' "$CANONICAL_TARGET_SHA" "$CANONICAL_DRAFT_SHA" "$CANONICAL_RUNTIME_COMMIT" <<<"$report"
}

print_status() {
  local report provenance
  report="$(state_json)"
  python3 "$STATE_HELPER" status --state "$SERVER_STATE" --format env
  provenance="$(state_provenance "$report")"
  echo "PROVENANCE=$provenance"
}

assert_persistent_contract() {
  [[ "$SERVER_HOST" == '127.0.0.1' ]] || {
    echo "ERROR: 1.0.0 persistent server is loopback-only; SERVER_HOST must be 127.0.0.1 (got $SERVER_HOST)." >&2
    exit 4
  }
  [[ "$SERVER_ALLOW_NONLOOPBACK" == '0' ]] || {
    echo 'ERROR: 1.0.0 persistent server requires SERVER_ALLOW_NONLOOPBACK=0; external access is enabled via ./expose.sh.' >&2
    exit 4
  }
  [[ "$GPU_SPLIT_MODE" == 'layer' && "$GPU_TENSOR_SPLIT" == '1,1' && "$GPU_LAYERS" == '999' ]] || {
    echo "ERROR: 1.0.0 inference freeze requires GPU split layer/1,1 and GPU_LAYERS=999." >&2
    exit 4
  }
  [[ "$PARALLEL_SLOTS" == '1' ]] || { echo 'ERROR: 1.0.0 inference freeze requires PARALLEL_SLOTS=1.' >&2; exit 4; }
  [[ "$DFLASH_DRAFT_N_MAX" == '15' ]] || { echo 'ERROR: 1.0.0 inference freeze requires DFLASH_DRAFT_N_MAX=15.' >&2; exit 4; }
  [[ "${REASONING_PRESERVE:-0}" == '0' ]] || { echo 'ERROR: reasoning-preserve remains frozen off.' >&2; exit 4; }
  [[ "$LLAMA_CPP_EXPECTED_COMMIT" == "$CANONICAL_RUNTIME_COMMIT" ]] || {
    echo "ERROR: exact z-lab runtime commit is frozen to $CANONICAL_RUNTIME_COMMIT." >&2
    exit 4
  }
  [[ "$MODEL_FILE" == "$CANONICAL_TARGET_FILE" ]] || {
    echo "ERROR: persistent server target is frozen to $CANONICAL_TARGET_FILE." >&2
    exit 4
  }
  [[ "$MODEL_SHA256" == "$CANONICAL_TARGET_SHA" && "$MODEL_SIZE_BYTES" == "$CANONICAL_TARGET_SIZE" ]] || {
    echo 'ERROR: configured target identity differs from the canonical target identity.' >&2
    exit 4
  }
  if [[ "$SERVE_PROFILE" == dflash2 ]]; then
    [[ "$DFLASH_MODEL_FILE" == "$CANONICAL_DRAFT_FILE" && "$DFLASH_MODEL_SHA256" == "$CANONICAL_DRAFT_SHA" && "$DFLASH_MODEL_SIZE_BYTES" == "$CANONICAL_DRAFT_SIZE" ]] || {
      echo 'ERROR: configured DFlash2 identity differs from the canonical draft identity.' >&2
      exit 4
    }
  fi
}

start_server() {
  acquire_control_lock
  case "$SERVE_PROFILE" in
    dflash2|baseline) ;;
    *) echo "ERROR: SERVE_PROFILE must be dflash2 or baseline; got '$SERVE_PROFILE'." >&2; exit 2 ;;
  esac

  local current status profile host port owned provenance
  current="$(state_json)"
  status="$(json_field status <<<"$current")"
  profile="$(json_field profile <<<"$current")"
  host="$(json_field host <<<"$current")"
  port="$(json_field port <<<"$current")"
  owned="$(json_field owned <<<"$current")"
  provenance="$(state_provenance "$current")"
  if [[ "$status" == ready ]]; then
    if [[ "$profile" == "$SERVE_PROFILE" && "$host" == "$SERVER_HOST" && "$port" == "$SERVER_PORT" ]]; then
      if [[ "$provenance" != PASS ]]; then
        echo "ERROR: owned READY state fails canonical provenance (PROVENANCE=$provenance); refusing idempotent acceptance." >&2
        exit 12
      fi
      echo "Muse-Glimmer persistent server is already READY (pid=$(json_field pid <<<"$current"), profile=$profile)."
      print_status
      return 0
    fi
    echo "ERROR: an owned READY server already exists with profile=$profile host=$host port=$port; stop it before changing serving configuration." >&2
    exit 12
  fi
  if [[ "$owned" == true && "$status" =~ ^(starting|degraded|stopping)$ ]]; then
    echo "ERROR: owned server is $status; use './serve.sh status' or './serve.sh restart' instead of spawning a duplicate." >&2
    exit 12
  fi
  if [[ "$status" == stale ]]; then
    python3 "$STATE_HELPER" clear-stale --state "$SERVER_STATE"
  fi

  # Persistent serving uses the full-context agent profile.
  RUN_PROFILE=agent
  load_profile_exports
  export_effective_config
  case "$SERVE_PROFILE" in
    dflash2) SPECULATIVE_MODE=dflash ;;
    baseline) SPECULATIVE_MODE=none ;;
  esac
  export RUN_PROFILE SPECULATIVE_MODE

  assert_persistent_contract
  python3 "$PROJECT_ROOT/scripts/server_exposure_guard.py" check-bind \
    --host "$SERVER_HOST" --allow-nonloopback "$SERVER_ALLOW_NONLOOPBACK"

  echo 'Verifying canonical source before persistent server preparation...'
  bash "$PROJECT_ROOT/scripts/verify_source.sh"

  echo 'Preparing/reusing venv and exact llama.cpp runtime...'
  bash "$PROJECT_ROOT/scripts/setup.sh"

  # Serving is intentionally exact: no verified-fallback model substitution.
  MODEL_SOURCE_MODE=hf
  MODEL_SELECTION_POLICY=exact
  MODEL_FILE="$CANONICAL_TARGET_FILE"
  MODEL_EXPECTED_SIZE_BYTES="$CANONICAL_TARGET_SIZE"
  MODEL_EXPECTED_SHA256="$CANONICAL_TARGET_SHA"
  export MODEL_SOURCE_MODE MODEL_SELECTION_POLICY MODEL_FILE MODEL_EXPECTED_SIZE_BYTES MODEL_EXPECTED_SHA256
  bash "$PROJECT_ROOT/scripts/download_model.sh"

  # Required multimodal projector comes only from the pinned Hugging Face repository.
  MM_PROJ_PATH="${MM_PROJ_PATH:-$MODELS_DIR/${MM_PROJ_FILE:-mmproj-Muse-Glimmer-30B-Q4_K_M.gguf}}"
  if [[ "${MM_PROJ_REQUIRED:-1}" == 1 ]]; then
    if [[ ! -s "$MM_PROJ_PATH" ]]; then
      hf_download_asset "${MM_PROJ_REPO:-meta-models/Muse-Glimmer-30B-GGUF}" \
        "${MM_PROJ_FILE:-mmproj-Muse-Glimmer-30B-Q4_K_M.gguf}" \
        "${MM_PROJ_REVISION:-43c7eadd41352a299ea8e0a36b3157978dd63596}" "$MODELS_DIR"
    fi
    [[ -s "$MM_PROJ_PATH" ]] || { echo "ERROR: required HF vision projector missing: $MM_PROJ_PATH" >&2; exit 4; }
    mmproj_size="$(stat -Lc '%s' "$MM_PROJ_PATH")"
    mmproj_sha="$(sha256sum "$MM_PROJ_PATH" | awk '{print $1}')"
    [[ "$mmproj_size" == "${MM_PROJ_SIZE_BYTES:-1400328928}" ]] || {
      echo "ERROR: HF vision projector size mismatch expected=${MM_PROJ_SIZE_BYTES:-1400328928} actual=$mmproj_size" >&2
      exit 4
    }
    [[ "$mmproj_sha" == "${MM_PROJ_SHA256:-f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6}" ]] || {
      echo "ERROR: HF vision projector SHA-256 mismatch actual=$mmproj_sha" >&2
      exit 4
    }
  else
    MM_PROJ_PATH=""
    mmproj_size=0
    mmproj_sha=""
  fi

  load_runtime_state
  apply_nvidia_runtime_env
  local py model_path bin_dir server_bin runtime_commit
  py="$(python_bin)"
  model_path="$(resolved_model_path)"
  bin_dir="$(llama_bin_dir)"
  server_bin="$bin_dir/llama-server"
  [[ -x "$server_bin" ]] || { echo "ERROR: llama-server missing at $server_bin" >&2; exit 3; }
  [[ -f "$model_path" ]] || { echo "ERROR: exact target missing at $model_path" >&2; exit 3; }
  [[ "${MODEL_SELECTION:-}" == exact && "${MODEL_RESOLVED_FILE:-}" == "$CANONICAL_TARGET_FILE" && "${MODEL_RESOLVED_SIZE_BYTES:-}" == "$CANONICAL_TARGET_SIZE" && "${MODEL_RESOLVED_SHA256:-}" == "$CANONICAL_TARGET_SHA" ]] || {
    echo 'ERROR: resolved target state does not match the exact canonical target identity.' >&2
    exit 4
  }

  runtime_commit="${LLAMA_RUNTIME_SOURCE_COMMIT:-${LLAMA_SOURCE_SNAPSHOT:-}}"
  [[ "$runtime_commit" == "$CANONICAL_RUNTIME_COMMIT" ]] || {
    echo "ERROR: resolved llama.cpp runtime commit '$runtime_commit' != canonical '$CANONICAL_RUNTIME_COMMIT'." >&2
    exit 4
  }
  case "${LLAMA_RUNTIME_SOURCE:-}" in
    kaggle_prebuilt*)
      [[ "${LLAMA_RUNTIME_MANIFEST_VERIFIED:-0}" == 1 ]] || { echo 'ERROR: prebuilt runtime manifest is not verified.' >&2; exit 4; }
      ;;
    local_build) ;;
    *) echo "ERROR: unsupported/unproven runtime source '${LLAMA_RUNTIME_SOURCE:-unknown}'." >&2; exit 4 ;;
  esac

  # setup.sh already runs this preflight; repeat immediately before the long-lived spawn.
  python3 "$PROJECT_ROOT/scripts/gpu_probe.py" --min-count "$GPU_REQUIRED_COUNT" --min-vram-mib "$GPU_MIN_VRAM_MIB"

  local draft_path='' draft_size='' draft_sha=''
  if [[ "$SPECULATIVE_MODE" == dflash ]]; then
    draft_path="$(resolve_dflash_model_path)"
    draft_size="$(stat -Lc '%s' "$draft_path")"
    draft_sha="$(sha256sum "$draft_path" | awk '{print $1}')"
    [[ "$draft_size" == "$CANONICAL_DRAFT_SIZE" && "$draft_sha" == "$CANONICAL_DRAFT_SHA" ]] || {
      echo 'ERROR: resolved DFlash2 draft identity does not match the canonical draft identity.' >&2
      exit 4
    }
    "$py" "$PROJECT_ROOT/scripts/speculative_utils.py" check-help --server "$server_bin"
  fi

  local threads batch_threads draft_threads draft_batch_threads context_per_slot
  threads="$(resolve_threads "$THREADS")"
  batch_threads="$(resolve_threads "$BATCH_THREADS")"
  draft_threads="$(resolve_pool_threads "$DFLASH_DRAFT_THREADS" "$threads")"
  draft_batch_threads="$(resolve_pool_threads "$DFLASH_DRAFT_BATCH_THREADS" "$batch_threads")"
  context_per_slot=$(( CONTEXT_SIZE / PARALLEL_SLOTS ))
  CONTEXT_PER_SLOT="$context_per_slot"
  (( CONTEXT_PER_SLOT >= 4096 )) || {
    echo "ERROR: context per slot ($context_per_slot) is below the safe minimum of 4096." >&2
    exit 4
  }
  (( MAX_TOKENS >= 1 && MAX_TOKENS <= CONTEXT_PER_SLOT )) || {
    echo "ERROR: MAX_TOKENS=$MAX_TOKENS must be between 1 and context per slot ($CONTEXT_PER_SLOT)." >&2
    exit 4
  }

  local runtime_ld="${LLAMA_RUNTIME_LD_LIBRARY_PATH:-}"
  if [[ -n "${NVIDIA_DRIVER_LIB_DIR:-}" ]]; then
    case ":$runtime_ld:" in *":$NVIDIA_DRIVER_LIB_DIR:"*) ;; *) runtime_ld="${runtime_ld:+$runtime_ld:}$NVIDIA_DRIVER_LIB_DIR" ;; esac
  fi
  [[ -z "$runtime_ld" ]] && runtime_ld="${LD_LIBRARY_PATH:-}"
  local -a runtime_env=(env "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES")
  [[ -n "$runtime_ld" ]] && runtime_env+=("LD_LIBRARY_PATH=$runtime_ld")

  local -a gpu_args=(-ngl "$GPU_LAYERS" -sm "$GPU_SPLIT_MODE")
  [[ -n "${GPU_TENSOR_SPLIT:-}" ]] && gpu_args+=(-ts "$GPU_TENSOR_SPLIT")
  server_help="$("$server_bin" --help 2>&1)" || { echo "ERROR: unable to inspect llama-server flags." >&2; exit 4; }
  for required_flag in --jinja --chat-template-kwargs --mmproj --cache-type-k --cache-type-v; do
    grep -q -- "$required_flag" <<<"$server_help" || {
      echo "ERROR: pinned llama-server does not support required agent flag $required_flag." >&2
      exit 4
    }
  done
  local -a server_cmd=(
    "$server_bin" -m "$model_path" -a muse-glimmer-30B -c "$CONTEXT_SIZE" -np "$PARALLEL_SLOTS"
    -t "$threads" -tb "$batch_threads" -b "$BATCH_SIZE" -ub "$UBATCH_SIZE"
    "${gpu_args[@]}"
    --host "$SERVER_HOST" --port "$SERVER_PORT" --jinja
    --temp "$TEMPERATURE" --top-p "$TOP_P" --top-k "$TOP_K"
    --chat-template-kwargs "{\"reasoning_strength\":\"$REASONING_STRENGTH\"}"
    --cache-type-k q8_0 --cache-type-v q8_0
  )
  if [[ -n "$MM_PROJ_PATH" ]]; then server_cmd+=(--mmproj "$MM_PROJ_PATH"); fi
  if [[ "${REASONING_BUDGET:-0}" =~ ^[1-9][0-9]*$ ]]; then server_cmd+=(--reasoning-budget "$REASONING_BUDGET"); fi
  if [[ "$SPECULATIVE_MODE" == dflash ]]; then
    local -a dflash_args=()
    mapfile -d '' -t dflash_args < <(
      "$py" "$PROJECT_ROOT/scripts/speculative_utils.py" emit-args \
        --model "$draft_path" \
        --draft-threads "$draft_threads" --draft-batch-threads "$draft_batch_threads" \
        --n-max "$DFLASH_DRAFT_N_MAX" --n-min "$DFLASH_DRAFT_N_MIN" --p-min "$DFLASH_DRAFT_P_MIN" \
        --draft-gpu-layers "$DFLASH_DRAFT_GPU_LAYERS" --draft-device "$DFLASH_DRAFT_DEVICE" --null
    )
    server_cmd+=("${dflash_args[@]}")
  fi

  python3 "$PROJECT_ROOT/scripts/server_guard.py" port-free --host "$SERVER_HOST" --port "$SERVER_PORT"

  local -a context_candidates=()
  local candidate next_context i server_pid ready_rc can_retry
  context_candidates+=("$CONTEXT_SIZE")
  if [[ "${AUTO_CONTEXT_FALLBACK:-1}" == 1 ]]; then
    for candidate in 65536 32768 16384 8192 4096; do
      if (( candidate < CONTEXT_SIZE )); then context_candidates+=("$candidate"); fi
    done
  fi

  : > "$SERVER_LOG"
  export SERVE_META_PROFILE="$SERVE_PROFILE" SERVE_META_TARGET_PATH="$model_path" SERVE_META_TARGET_SHA="$CANONICAL_TARGET_SHA"
  export SERVE_META_DRAFT_PATH="$draft_path" SERVE_META_DRAFT_SHA="$draft_sha" SERVE_META_RUNTIME_SOURCE="${LLAMA_RUNTIME_SOURCE:-}"
  export SERVE_META_RUNTIME_COMMIT="$runtime_commit" SERVE_META_CUDA="$CUDA_VISIBLE_DEVICES" SERVE_META_GPU_COUNT="$GPU_REQUIRED_COUNT"
  export SERVE_META_SERVER_BIN="$server_bin" SERVE_META_SERVER_BIN_SHA="${LLAMA_BINARY_SHA256_SERVER:-}"
  export SERVE_META_THREADS="$threads" SERVE_META_BATCH_THREADS="$batch_threads"
  for candidate in "${context_candidates[@]}"; do
    next_context=$(( candidate / PARALLEL_SLOTS ))
    (( next_context >= 4096 && MAX_TOKENS <= next_context )) || continue
    local -a attempt_cmd=("${server_cmd[@]}")
    for ((i=0; i<${#attempt_cmd[@]}; i++)); do
      if [[ "${attempt_cmd[$i]}" == "-c" ]] && (( i + 1 < ${#attempt_cmd[@]} )); then
        attempt_cmd[$((i+1))]="$candidate"
        break
      fi
    done
    export SERVE_META_CONTEXT="$candidate" SERVE_META_MAX_TOKENS="$MAX_TOKENS"
    export SERVE_META_REASONING="$REASONING_STRENGTH" SERVE_META_MM_PROJ_PATH="$MM_PROJ_PATH"
    export SERVE_META_MM_PROJ_SHA="$mmproj_sha" SERVE_META_MM_PROJ_SIZE="$mmproj_size"
    python3 - "$SERVER_METADATA" <<'PY'
import json,os,sys
m={
  'profile':os.environ['SERVE_META_PROFILE'],
  'target_path':os.environ['SERVE_META_TARGET_PATH'], 'target_sha256':os.environ['SERVE_META_TARGET_SHA'],
  'draft_path':os.environ['SERVE_META_DRAFT_PATH'] or None, 'draft_sha256':os.environ['SERVE_META_DRAFT_SHA'] or None,
  'runtime_source':os.environ['SERVE_META_RUNTIME_SOURCE'], 'runtime_commit':os.environ['SERVE_META_RUNTIME_COMMIT'],
  'cuda_visible_devices':os.environ['SERVE_META_CUDA'], 'gpu_count':int(os.environ['SERVE_META_GPU_COUNT']),
  'server_bin':os.environ['SERVE_META_SERVER_BIN'], 'server_bin_sha256':os.environ['SERVE_META_SERVER_BIN_SHA'] or None,
  'context_size':int(os.environ['SERVE_META_CONTEXT']), 'max_tokens':int(os.environ['SERVE_META_MAX_TOKENS']),
  'reasoning_strength':os.environ['SERVE_META_REASONING'],
  'mmproj_path':os.environ['SERVE_META_MM_PROJ_PATH'] or None, 'mmproj_sha256':os.environ['SERVE_META_MM_PROJ_SHA'] or None,
  'mmproj_size_bytes':int(os.environ['SERVE_META_MM_PROJ_SIZE']),
  'threads':int(os.environ['SERVE_META_THREADS']), 'batch_threads':int(os.environ['SERVE_META_BATCH_THREADS']),
}
with open(sys.argv[1],'w',encoding='utf-8') as f: json.dump(m,f,indent=2,sort_keys=True); f.write('\n')
PY
    {
      echo "# profile=$SERVE_PROFILE host=$SERVER_HOST port=$SERVER_PORT requested_context=$CONTEXT_SIZE actual_attempt_context=$candidate max_tokens=$MAX_TOKENS"
      echo "# target_sha256=$CANONICAL_TARGET_SHA draft_sha256=${draft_sha:-none}"
      echo "# runtime_source=${LLAMA_RUNTIME_SOURCE:-unknown} runtime_commit=$runtime_commit"
      echo "# mmproj_sha256=${mmproj_sha:-none} mmproj_size_bytes=${mmproj_size:-0}"
      printf '# command:'; printf ' %q' "${runtime_env[@]}" "${attempt_cmd[@]}"; echo
    } >> "$SERVER_LOG"

    echo "Starting persistent Muse-Glimmer server: profile=$SERVE_PROFILE host=$SERVER_HOST port=$SERVER_PORT context=$candidate output_cap=$MAX_TOKENS"
    nohup "${runtime_env[@]}" "${attempt_cmd[@]}" >> "$SERVER_LOG" 2>&1 < /dev/null &
    server_pid=$!
    if ! python3 "$STATE_HELPER" begin --state "$SERVER_STATE" --pid "$server_pid" \
        --profile "$SERVE_PROFILE" --host "$SERVER_HOST" --port "$SERVER_PORT" --log "$SERVER_LOG" --metadata-json "$SERVER_METADATA" \
        --command-token "$server_bin"; then
      terminate_process "$server_pid" 2 || true
      exit 10
    fi

    if python3 "$PROJECT_ROOT/scripts/server_guard.py" wait-ready --pid "$server_pid" \
        --url "http://$SERVER_HOST:$SERVER_PORT/health" --url "http://$SERVER_HOST:$SERVER_PORT/v1/models" \
        --timeout "$SERVER_START_TIMEOUT" --interval 1; then
      python3 "$STATE_HELPER" mark-ready --state "$SERVER_STATE"
      echo "SERVER_CONTEXT_REQUESTED=$CONTEXT_SIZE"
      echo "SERVER_CONTEXT_EFFECTIVE=$candidate"
      echo "SERVER_MAX_OUTPUT_TOKENS=$MAX_TOKENS"
      if (( candidate < CONTEXT_SIZE )); then
        echo "CONTEXT_AUTO_FALLBACK=USED requested=$CONTEXT_SIZE effective=$candidate"
      else
        echo "CONTEXT_AUTO_FALLBACK=NOT_NEEDED"
      fi
      echo 'Persistent server STARTED / READY.'
      print_status
      return 0
    else
      ready_rc=$?
      can_retry=0
      if ! kill -0 "$server_pid" 2>/dev/null || grep -Eiq 'out of memory|failed to allocate|allocation failed|CUDA error:.*(memory|alloc)|KV cache.*(fail|alloc|memory)|not enough (free )?memory' "$SERVER_LOG"; then
        can_retry=1
      fi
      tail -n 120 "$SERVER_LOG" >&2 || true
      if python3 "$STATE_HELPER" owned-pid --state "$SERVER_STATE" >/dev/null 2>&1; then
        terminate_process "$server_pid" 5 || true
      fi
      python3 "$STATE_HELPER" mark-stopped --state "$SERVER_STATE" --reason readiness-failed || true
      if (( can_retry == 1 )) && [[ "${AUTO_CONTEXT_FALLBACK:-1}" == 1 ]]; then
        echo "CONTEXT_ATTEMPT_FAILED=$candidate; evaluating next smaller context after resource/startup failure." >&2
        continue
      fi
      echo "ERROR: server did not become ready at context=$candidate; no safe automatic fallback condition detected." >&2
      exit "$ready_rc"
    fi
  done
  echo "ERROR: no context candidate fits MAX_TOKENS=$MAX_TOKENS (requested CONTEXT_SIZE=$CONTEXT_SIZE)." >&2
  exit 4
}

stop_server() {
  acquire_control_lock
  local report status pid
  report="$(state_json)"
  status="$(json_field status <<<"$report")"
  if [[ "$status" == stopped ]]; then
    echo 'Muse-Glimmer persistent server is already stopped.'
    print_status
    return 0
  fi
  if ! pid="$(python3 "$STATE_HELPER" owned-pid --state "$SERVER_STATE" 2>/dev/null)"; then
    if [[ "$status" == stale ]]; then
      echo 'Stale server state detected; clearing it without signaling any process.'
      python3 "$STATE_HELPER" clear-stale --state "$SERVER_STATE"
      print_status
      return 0
    fi
    echo "ERROR: state is '$status' but no exact owned PID can be proven; refusing to signal any process." >&2
    exit 12
  fi
  python3 "$STATE_HELPER" mark-stopping --state "$SERVER_STATE"
  echo "Stopping owned persistent server PID=$pid (TERM then bounded KILL fallback)..."
  terminate_process "$pid" 5
  python3 "$STATE_HELPER" mark-stopped --state "$SERVER_STATE" --reason requested
  echo 'Persistent server STOPPED.'
  print_status
}

case "$ACTION" in
  start) start_server ;;
  status) print_status ;;
  stop) stop_server ;;
  restart)
    "$0" stop
    exec "$0" start
    ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
