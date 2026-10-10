#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT
source "$PROJECT_ROOT/scripts/nvidia_runtime.sh"

declare -Ag _USER_OVERRIDES=()
_CONFIG_KEYS=(
  MODEL_REPO PRIMARY_MODEL_FILE MODEL_FILE MODEL_SIZE_BYTES MODEL_SHA256 ALT_MODEL_FILE ALT_MODEL_SIZE_BYTES ALT_MODEL_SHA256 MODEL_HF_REVISION HF_DOWNLOAD_WORKERS HF_XET_FIXED_DOWNLOAD_CONCURRENCY HF_XET_HIGH_PERFORMANCE HF_ENDPOINT HF_DOWNLOAD_RETRIES HF_RESUME_DOWNLOAD
  DYNAMIC_BASELINE_FILE DYNAMIC_BASELINE_SIZE_BYTES DYNAMIC_BASELINE_SHA256
  MODEL_SELECTION_POLICY MODEL_LOAD_ONLY MODEL_EXPECTED_SIZE_BYTES MODEL_EXPECTED_SHA256
  FORCE_MODEL_DOWNLOAD VERIFY_MODEL_SHA256 RECOVER_HF_INCOMPLETE MM_PROJ_REPO MM_PROJ_REVISION MM_PROJ_FILE MM_PROJ_PATH MM_PROJ_REQUIRED
  SPECULATIVE_MODE DFLASH_KAGGLE_INPUT_ROOT DFLASH_KAGGLE_MODEL_DATASET DFLASH_HF_MODEL_REPO DFLASH_HF_REVISION DFLASH_HF_FILE DFLASH_MODEL_PATH DFLASH_MODEL_FILE DFLASH_MODEL_SIZE_BYTES DFLASH_MODEL_SHA256 VERIFY_DFLASH_SHA256
  DFLASH_DRAFT_THREADS DFLASH_DRAFT_BATCH_THREADS DFLASH_DRAFT_N_MAX DFLASH_DRAFT_N_MIN DFLASH_DRAFT_P_MIN DFLASH_DRAFT_GPU_LAYERS DFLASH_DRAFT_DEVICE
  REQUIRE_FULL_QUALITY_ACCEPTANCE
  KAGGLE_INPUT_ROOT KAGGLE_MODEL_DATASET KAGGLE_RUNTIME_DATASET KAGGLE_RUNTIME_COPY_MODE KAGGLE_RUNTIME_CACHE_DIR KAGGLE_SOURCE_BUILD_CACHE_DIR KAGGLE_RUNTIME_REQUIRE_MANIFEST KAGGLE_RUNTIME_MANIFEST_FILE KAGGLE_MODEL_ALIASES MODEL_SOURCE_MODE LLAMA_SOURCE_MODE
  LLAMA_CPP_REPO LLAMA_CPP_REF LLAMA_CPP_EXPECTED_COMMIT LLAMA_MIN_BUILD LLAMA_BUILD_VARIANT CUDA_ARCHITECTURES CUDAToolkit_ROOT CUDACXX CMAKE_CUDA_COMPILER
  NVIDIA_SMI NVIDIA_SMI_SEARCH_PATHS NVIDIA_DRIVER_LIB_SEARCH_DIRS
  CUDA_VISIBLE_DEVICES GPU_REQUIRED_COUNT GPU_MIN_VRAM_MIB GPU_LAYERS GPU_SPLIT_MODE GPU_TENSOR_SPLIT GPU_MAIN GPU_MONITOR_INTERVAL
  SERVER_HOST SERVER_ALLOW_NONLOOPBACK SERVER_PORT SERVER_START_TIMEOUT PARALLEL_SLOTS ADMISSION_QUEUE_SIZE ADMISSION_QUEUE_WAIT_SECONDS MAX_REQUEST_BYTES MAX_MESSAGES MAX_SINGLE_CONTENT MAX_AGGREGATE_CONTENT MAX_TOOL_CALLS
  THREADS BATCH_THREADS BATCH_SIZE UBATCH_SIZE CONTEXT_SIZE
  TEMPERATURE TOP_P TOP_K SEED MAX_TOKENS REASONING_STRENGTH REASONING_BUDGET MIN_ANSWER_TOKENS REASONING_PRESERVE PROMPT_LIMIT REQUEST_TIMEOUT
  MONITOR_INTERVAL ENABLE_LOAD_BENCH LOAD_BENCH_REPS ENABLE_LLAMA_BENCH LLAMA_BENCH_PROMPT LLAMA_BENCH_GEN LLAMA_BENCH_REPS
  SOURCE_INTEGRITY_MODE RUN_PROFILE RUN_LABEL
)
for _k in "${_CONFIG_KEYS[@]}"; do
  if [[ -n "${!_k+x}" ]]; then _USER_OVERRIDES["$_k"]=1; fi
done

DEFAULT_ENV="$PROJECT_ROOT/config/default.env"
if [[ -f "$DEFAULT_ENV" ]]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    if [[ -z "${!key+x}" ]]; then printf -v "$key" '%s' "$value"; fi
  done < "$DEFAULT_ENV"
fi
RUN_PROFILE="${RUN_PROFILE:-smoke}"

if [[ -z "${VENV_DIR+x}" ]]; then
  if [[ -n "${KAGGLE_KERNEL_RUN_TYPE:-}" || -n "${KAGGLE_URL_BASE:-}" || -d /kaggle/working ]]; then
    VENV_DIR=/opt/muse-venv
  else
    VENV_DIR="$PROJECT_ROOT/.venv"
  fi
fi
LLAMA_DIR="${LLAMA_DIR:-$PROJECT_ROOT/vendor/llama.cpp}"
MODELS_DIR="${MODELS_DIR:-$PROJECT_ROOT/models}"
RUNS_DIR="${RUNS_DIR:-$PROJECT_ROOT/runs}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$PROJECT_ROOT/artifacts}"
COMPARISONS_DIR="${COMPARISONS_DIR:-$PROJECT_ROOT/comparisons}"
RUNTIME_STATE_DIR="${RUNTIME_STATE_DIR:-$ARTIFACTS_DIR/runtime-state}"
mkdir -p "$MODELS_DIR" "$RUNS_DIR" "$ARTIFACTS_DIR" "$COMPARISONS_DIR" "$RUNTIME_STATE_DIR" "$PROJECT_ROOT/vendor"
export VENV_DIR LLAMA_DIR MODELS_DIR RUNS_DIR ARTIFACTS_DIR COMPARISONS_DIR RUNTIME_STATE_DIR

python_bin() { if [[ -x "$VENV_DIR/bin/python" ]]; then echo "$VENV_DIR/bin/python"; else command -v python3; fi; }
hf_bin() { if [[ -x "$VENV_DIR/bin/hf" ]]; then echo "$VENV_DIR/bin/hf"; else command -v hf || true; fi; }
monotonic_now() { python3 -c 'import time; print(time.monotonic())'; }
elapsed_since() { python3 - "$1" <<'PYTIME'
import sys,time
print(f"{time.monotonic()-float(sys.argv[1]):.6f}")
PYTIME
}
is_user_override() { [[ -n "${_USER_OVERRIDES[$1]:-}" ]]; }

export_effective_config() {
  local key
  for key in "${_CONFIG_KEYS[@]}"; do [[ -n "${!key+x}" ]] && export "$key"; done
  return 0
}
user_override_keys_csv() {
  local out=() key
  for key in "${_CONFIG_KEYS[@]}"; do [[ -n "${_USER_OVERRIDES[$key]:-}" ]] && out+=("$key"); done
  local IFS=,; echo "${out[*]:-}"
}
resolve_threads() {
  local requested="${1:-auto}"
  if [[ "$requested" == auto || "$requested" == 0 || -z "$requested" ]]; then
    "$(python_bin)" - <<'PY'
import os
try: n=len(os.sched_getaffinity(0))
except Exception: n=os.cpu_count() or 1
print(max(1,n))
PY
  else echo "$requested"; fi
}
load_profile_exports() {
  local py profile_file assignments
  py="$(python_bin)"; profile_file="$PROJECT_ROOT/config/profiles.json"
  assignments="$("$py" - "$profile_file" "$RUN_PROFILE" <<'PY'
import json,shlex,sys
p=json.load(open(sys.argv[1],encoding='utf-8')); name=sys.argv[2]; seen=set()
if name not in p: raise SystemExit(f'Unknown RUN_PROFILE={name!r}; choose one of {sorted(p)}')
x=p[name]
while 'alias_of' in x:
    if name in seen: raise SystemExit('profile alias cycle')
    seen.add(name); name=x['alias_of']; x=p[name]
mapping={
 'CONTEXT_SIZE':x['context_size'],'MAX_TOKENS':x['max_tokens'],'REASONING_STRENGTH':x['reasoning_strength'],
 'REASONING_BUDGET':x['reasoning_budget'],'MIN_ANSWER_TOKENS':x['min_answer_tokens'],'PROMPT_LIMIT':x['prompt_limit'],
 'ENABLE_LOAD_BENCH':x.get('enable_load_bench',0),'LLAMA_BENCH_PROMPT':x['llama_bench_prompt'],
 'LLAMA_BENCH_GEN':x['llama_bench_gen'],'LLAMA_BENCH_REPS':x['llama_bench_reps']}
for k,v in mapping.items(): print(k+'='+shlex.quote(str(v)))
PY
)"
  while IFS='=' read -r key value; do
    [[ -z "$key" ]] && continue
    if ! is_user_override "$key"; then eval "printf -v '$key' '%s' $value"; fi
  done <<< "$assignments"
}

llama_build_dir() {
  local key="${LLAMA_CPP_EXPECTED_COMMIT:-${LLAMA_CPP_REF:-unknown}}"
  key="${key//[^A-Za-z0-9._-]/_}"
  echo "$LLAMA_DIR/build-gpu-v1-${key:0:16}-${LLAMA_BUILD_VARIANT:-cuda}"
}
load_runtime_state() {
  [[ -f "$RUNTIME_STATE_DIR/setup.env" ]] && source "$RUNTIME_STATE_DIR/setup.env"
  [[ -f "$RUNTIME_STATE_DIR/nvidia.env" ]] && source "$RUNTIME_STATE_DIR/nvidia.env"
  [[ -f "$RUNTIME_STATE_DIR/llama.env" ]] && source "$RUNTIME_STATE_DIR/llama.env"
  [[ -f "$RUNTIME_STATE_DIR/model.env" ]] && source "$RUNTIME_STATE_DIR/model.env"
  export LLAMA_RESOLVED_BIN_DIR LLAMA_RUNTIME_SOURCE LLAMA_RUNTIME_LD_LIBRARY_PATH LLAMA_RUNTIME_DATASET_ROOT LLAMA_RUNTIME_VERSION_TEXT \
    LLAMA_SOURCE_SNAPSHOT LLAMA_SOURCE_PATH LLAMA_SOURCE_TREE_SHA256 LLAMA_BUILD_CMAKE_ARGS LLAMA_BUILD_COMPILER LLAMA_BUILD_CUDA_VERSION LLAMA_BUILD_DIR \
    LLAMA_BINARY_SHA256_SERVER LLAMA_BINARY_SHA256_CLI LLAMA_BINARY_SHA256_BENCH LLAMA_RUNTIME_MANIFEST_VERIFIED LLAMA_RUNTIME_MANIFEST_PATH LLAMA_RUNTIME_SOURCE_COMMIT \
    MODEL_RESOLVED_PATH MODEL_RESOLVED_FILE MODEL_RUNTIME_SOURCE MODEL_RUNTIME_DATASET_ROOT MODEL_RESOLVED_SIZE_BYTES MODEL_RESOLVED_SHA256 MODEL_METADATA_SOURCE MODEL_SELECTION MODEL_VERIFICATION_SECONDS \
    SETUP_TOTAL_SECONDS VENV_BOOTSTRAP_SECONDS RUNTIME_DISCOVERY_SECONDS VENV_REUSED \
    LLAMA_SOURCE_BUILD_SECONDS SOURCE_BUILD_CACHE_HIT \
    CUDAToolkit_ROOT CUDACXX CMAKE_CUDA_COMPILER CUDA_TOOLKIT_ROOT_RESOLVED CUDA_NVCC_PATH CUDA_NVCC_REALPATH CUDA_RUNTIME_HEADER CUDA_CUDART_PATH CUDA_TOOLKIT_VERSION CUDA_DRIVER_LIBRARY CUDA_PATH \
    NVIDIA_SMI_PATH NVIDIA_SMI_REALPATH NVIDIA_SMI_BIN_DIR NVIDIA_ML_LIBRARY NVIDIA_DRIVER_LIB_DIR NVIDIA_RUNTIME_LD_LIBRARY_PATH NVIDIA_RUNTIME_PATH_INJECTED NVIDIA_RUNTIME_LD_LIBRARY_INJECTED 2>/dev/null || true
}
llama_bin_dir() {
  if [[ -n "${LLAMA_RESOLVED_BIN_DIR:-}" ]]; then echo "$LLAMA_RESOLVED_BIN_DIR"; else echo "$(llama_build_dir)/bin"; fi
}
resolved_model_path() {
  if [[ -n "${MODEL_RESOLVED_PATH:-}" ]]; then echo "$MODEL_RESOLVED_PATH"; else echo "$MODELS_DIR/$MODEL_FILE"; fi
}

model_expected_size() {
  if [[ -n "${MODEL_EXPECTED_SIZE_BYTES:-}" ]]; then echo "$MODEL_EXPECTED_SIZE_BYTES"
  elif [[ "$MODEL_FILE" == "${ALT_MODEL_FILE:-}" ]]; then echo "${ALT_MODEL_SIZE_BYTES:-}"
  elif [[ "$MODEL_FILE" == "${PRIMARY_MODEL_FILE:-}" ]]; then echo "${MODEL_SIZE_BYTES:-}"
  else echo ""; fi
}
model_expected_sha256() {
  if [[ -n "${MODEL_EXPECTED_SHA256:-}" ]]; then echo "$MODEL_EXPECTED_SHA256"
  elif [[ "$MODEL_FILE" == "${ALT_MODEL_FILE:-}" ]]; then echo "${ALT_MODEL_SHA256:-}"
  elif [[ "$MODEL_FILE" == "${PRIMARY_MODEL_FILE:-}" ]]; then echo "${MODEL_SHA256:-}"
  else echo ""; fi
}
resolve_pool_threads() {
  local mode="${1:-auto}" fallback="${2:?fallback required}"
  case "$mode" in
    auto|decode) echo "$fallback" ;;
    one|1) echo 1 ;;
    [0-9]*) echo "$mode" ;;
    *) echo "ERROR: invalid thread-pool mode: $mode" >&2; return 2 ;;
  esac
}

resolve_dflash_model_path() {
  bash "$PROJECT_ROOT/scripts/resolve_dflash_model.sh"
}
