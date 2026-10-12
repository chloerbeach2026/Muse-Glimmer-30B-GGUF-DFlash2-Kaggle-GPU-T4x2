#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/scripts/common.sh"
source "$PROJECT_ROOT/scripts/cuda_toolkit.sh"

STATE="$RUNTIME_STATE_DIR/llama.env"
mkdir -p "$RUNTIME_STATE_DIR"
case "${KAGGLE_RUNTIME_COPY_MODE:-auto}" in auto|never|always) ;; *) echo 'ERROR: KAGGLE_RUNTIME_COPY_MODE must be auto|never|always' >&2; exit 2;; esac
case "${KAGGLE_RUNTIME_REQUIRE_MANIFEST:-0}" in 0|1) ;; *) echo 'ERROR: KAGGLE_RUNTIME_REQUIRE_MANIFEST must be 0|1' >&2; exit 2;; esac

json="$(python3 "$PROJECT_ROOT/scripts/kaggle_runtime.py" \
  --input-root "$KAGGLE_INPUT_ROOT" \
  --dataset-slug "$KAGGLE_RUNTIME_DATASET" \
  --model-file "$MODEL_FILE" \
  --model-aliases "${KAGGLE_MODEL_ALIASES:-}")"

mapfile -t discovered < <(python3 -c '
import json,sys
x=json.load(sys.stdin)
for k in ("dataset_root","llama_bin_dir","llama_source_dir","llama_source_snapshot"):
    print(x.get(k) or "")
' <<< "$json")
DATASET_ROOT="${discovered[0]:-}"
BIN_DIR="${discovered[1]:-}"
SOURCE_DIR="${discovered[2]:-}"
SOURCE_SNAPSHOT="${discovered[3]:-}"

validate_runtime() {
  local bin_dir="$1" ld_path="$2" version_out devices_out help_out rc_version rc_devices rc_help
  for bin in llama-server llama-cli llama-bench; do [[ -x "$bin_dir/$bin" ]] || return 31; done
  set +e
  version_out="$(env LD_LIBRARY_PATH="$ld_path" CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" "$bin_dir/llama-cli" --version 2>&1)"; rc_version=$?
  devices_out="$(env LD_LIBRARY_PATH="$ld_path" CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" "$bin_dir/llama-cli" --list-devices 2>&1)"; rc_devices=$?
  help_out="$(env LD_LIBRARY_PATH="$ld_path" "$bin_dir/llama-server" --help 2>&1)"; rc_help=$?
  set -e
  (( rc_version == 0 && rc_devices == 0 && rc_help == 0 )) || return 32
  grep -qi 'CUDA' <<< "$devices_out" || return 33
  grep -q -- '--reasoning-budget' <<< "$help_out" || return 34
  grep -q -- '--jinja' <<< "$help_out" || return 35
  LLAMA_VALIDATED_VERSION="$version_out"
  LLAMA_VALIDATED_DEVICES="$devices_out"
  return 0
}

binary_hash() { sha256sum "$1" | awk '{print $1}'; }

LLAMA_RUNTIME_MANIFEST_VERIFIED_SELECTED=0
LLAMA_RUNTIME_MANIFEST_PATH_SELECTED=""
LLAMA_RUNTIME_SOURCE_COMMIT_SELECTED=""

verify_prebuilt_manifest() {
  local bin_dir="$1" manifest="${DATASET_ROOT}/${KAGGLE_RUNTIME_MANIFEST_FILE:-runtime-manifest.json}" out
  if [[ "${KAGGLE_RUNTIME_REQUIRE_MANIFEST:-0}" != 1 ]]; then
    LLAMA_RUNTIME_MANIFEST_VERIFIED_SELECTED=0
    LLAMA_RUNTIME_MANIFEST_PATH_SELECTED=""
    LLAMA_RUNTIME_SOURCE_COMMIT_SELECTED=""
    return 0
  fi
  [[ -n "${LLAMA_CPP_EXPECTED_COMMIT:-}" ]] || { echo 'ERROR: manifest-required prebuilt runtime needs LLAMA_CPP_EXPECTED_COMMIT.' >&2; return 40; }
  set +e
  out="$(python3 "$PROJECT_ROOT/scripts/runtime_manifest.py" verify \
    --manifest "$manifest" --bin-dir "$bin_dir" \
    --expected-repo "$LLAMA_CPP_REPO" --expected-commit "$LLAMA_CPP_EXPECTED_COMMIT" \
    --required-cuda-arch "$CUDA_ARCHITECTURES" 2>&1)"
  local rc=$?
  set -e
  if (( rc != 0 )); then
    echo "$out" >&2
    return 41
  fi
  LLAMA_RUNTIME_MANIFEST_VERIFIED_SELECTED=1
  LLAMA_RUNTIME_MANIFEST_PATH_SELECTED="$manifest"
  LLAMA_RUNTIME_SOURCE_COMMIT_SELECTED="$LLAMA_CPP_EXPECTED_COMMIT"
  return 0
}

write_runtime_state() {
  local selected_bin="$1" selected_ld="$2" selected_source="$3" source_path="${4:-}" snapshot="${5:-}" source_hash="${6:-}" cmake_args="${7:-}" compiler="${8:-}" cuda_version="${9:-}" build_dir="${10:-}" source_build_seconds="${11:-0}" cache_hit="${12:-0}"
  {
    printf 'LLAMA_RESOLVED_BIN_DIR=%q\n' "$selected_bin"
    printf 'LLAMA_RUNTIME_SOURCE=%q\n' "$selected_source"
    printf 'LLAMA_RUNTIME_LD_LIBRARY_PATH=%q\n' "$selected_ld"
    printf 'LLAMA_RUNTIME_DATASET_ROOT=%q\n' "$DATASET_ROOT"
    printf 'LLAMA_RUNTIME_VERSION_TEXT=%q\n' "$LLAMA_VALIDATED_VERSION"
    printf 'LLAMA_SOURCE_SNAPSHOT=%q\n' "$snapshot"
    printf 'LLAMA_SOURCE_PATH=%q\n' "$source_path"
    printf 'LLAMA_SOURCE_TREE_SHA256=%q\n' "$source_hash"
    printf 'LLAMA_BUILD_CMAKE_ARGS=%q\n' "$cmake_args"
    printf 'LLAMA_BUILD_COMPILER=%q\n' "$compiler"
    printf 'LLAMA_BUILD_CUDA_VERSION=%q\n' "$cuda_version"
    printf 'LLAMA_BUILD_DIR=%q\n' "$build_dir"
    printf 'LLAMA_SOURCE_BUILD_SECONDS=%q\n' "$source_build_seconds"
    printf 'SOURCE_BUILD_CACHE_HIT=%q\n' "$cache_hit"
    printf 'CUDAToolkit_ROOT=%q\n' "${CUDAToolkit_ROOT:-}"
    printf 'CUDACXX=%q\n' "${CUDACXX:-}"
    printf 'CMAKE_CUDA_COMPILER=%q\n' "${CMAKE_CUDA_COMPILER:-}"
    printf 'CUDA_TOOLKIT_ROOT_RESOLVED=%q\n' "${CUDA_TOOLKIT_ROOT_RESOLVED:-}"
    printf 'CUDA_NVCC_PATH=%q\n' "${CUDA_NVCC_PATH:-}"
    printf 'CUDA_NVCC_REALPATH=%q\n' "${CUDA_NVCC_REALPATH:-}"
    printf 'CUDA_RUNTIME_HEADER=%q\n' "${CUDA_RUNTIME_HEADER:-}"
    printf 'CUDA_CUDART_PATH=%q\n' "${CUDA_CUDART_PATH:-}"
    printf 'CUDA_TOOLKIT_VERSION=%q\n' "${CUDA_TOOLKIT_VERSION:-}"
    printf 'CUDA_DRIVER_LIBRARY=%q\n' "${CUDA_DRIVER_LIBRARY:-}"
    printf 'CUDA_PATH=%q\n' "${CUDA_PATH:-}"
    printf 'LLAMA_BINARY_SHA256_SERVER=%q\n' "$(binary_hash "$selected_bin/llama-server")"
    printf 'LLAMA_BINARY_SHA256_CLI=%q\n' "$(binary_hash "$selected_bin/llama-cli")"
    printf 'LLAMA_BINARY_SHA256_BENCH=%q\n' "$(binary_hash "$selected_bin/llama-bench")"
    printf 'LLAMA_RUNTIME_MANIFEST_VERIFIED=%q\n' "${LLAMA_RUNTIME_MANIFEST_VERIFIED_SELECTED:-0}"
    printf 'LLAMA_RUNTIME_MANIFEST_PATH=%q\n' "${LLAMA_RUNTIME_MANIFEST_PATH_SELECTED:-}"
    printf 'LLAMA_RUNTIME_SOURCE_COMMIT=%q\n' "${LLAMA_RUNTIME_SOURCE_COMMIT_SELECTED:-}"
  } > "$STATE"
}

# 1) Prefer a complete prebuilt runtime when the dataset actually contains one.
if [[ -n "$BIN_DIR" ]]; then
  SOURCE_BUILD_ROOT="$(cd "$BIN_DIR/.." && pwd)"
  DIRECT_LD="$BIN_DIR:$SOURCE_BUILD_ROOT:$SOURCE_BUILD_ROOT/lib"
  [[ -n "${LD_LIBRARY_PATH:-}" ]] && DIRECT_LD="$DIRECT_LD:$LD_LIBRARY_PATH"

  selected_bin=""; selected_ld=""; selected_source=""; direct_rc=0; manifest_rc=0
  set +e; verify_prebuilt_manifest "$BIN_DIR"; manifest_rc=$?; set -e
  if (( manifest_rc != 0 )); then
    echo "Kaggle prebuilt runtime manifest rejected (exit=$manifest_rc); prebuilt path will not be used." >&2
  elif [[ "$KAGGLE_RUNTIME_COPY_MODE" != always ]]; then
    set +e; validate_runtime "$BIN_DIR" "$DIRECT_LD"; direct_rc=$?; set -e
    if (( direct_rc == 0 )); then
      selected_bin="$BIN_DIR"; selected_ld="$DIRECT_LD"; selected_source=kaggle_prebuilt
    fi
  fi

  if [[ -z "$selected_bin" && "$KAGGLE_RUNTIME_COPY_MODE" != never && "$manifest_rc" -eq 0 ]]; then
    cache="${KAGGLE_RUNTIME_CACHE_DIR:-/opt/muse-llama-runtime}"
    tmp="${cache}.tmp.$$"
    rm -rf "$tmp"
    mkdir -p "$tmp/bin"
    cp -a "$BIN_DIR/." "$tmp/bin/"
    if [[ -d "$SOURCE_BUILD_ROOT/lib" ]]; then mkdir -p "$tmp/lib"; cp -a "$SOURCE_BUILD_ROOT/lib/." "$tmp/lib/"; fi
    chmod u+x "$tmp/bin/llama-server" "$tmp/bin/llama-cli" "$tmp/bin/llama-bench"
    rm -rf "$cache"
    mkdir -p "$(dirname "$cache")"
    mv "$tmp" "$cache"
    copied_bin="$cache/bin"
    copied_ld="$copied_bin:$cache:$cache/lib"
    [[ -n "${LD_LIBRARY_PATH:-}" ]] && copied_ld="$copied_ld:$LD_LIBRARY_PATH"
    set +e; validate_runtime "$copied_bin" "$copied_ld"; copy_rc=$?; set -e
    if (( copy_rc == 0 )); then
      selected_bin="$copied_bin"; selected_ld="$copied_ld"; selected_source=kaggle_prebuilt_copy
    else
      echo "Copied Kaggle runtime failed validation (exit=$copy_rc)." >&2
    fi
  fi

  if [[ -n "$selected_bin" ]]; then
    write_runtime_state "$selected_bin" "$selected_ld" "$selected_source"
    echo "Using Kaggle llama.cpp prebuilt runtime: $selected_bin (source=$selected_source)"
    echo "$LLAMA_VALIDATED_VERSION"
    echo "$LLAMA_VALIDATED_DEVICES"
    exit 0
  fi
  echo "Kaggle prebuilt runtime was discovered but failed validation (direct=${direct_rc:-n/a}); checking for dataset source snapshot." >&2
fi

# 2) Source builds are available only in auto/build modes. The canonical Kaggle
# mode is intentionally prebuilt-only: if Add Input is configured incorrectly or
# the runtime is incompatible, fail loudly instead of spending time compiling.
if [[ "${LLAMA_SOURCE_MODE:-auto}" == "kaggle" ]]; then
  echo "ERROR: LLAMA_SOURCE_MODE=kaggle requires a validated prebuilt runtime from the attached dataset; source builds are disabled." >&2
  exit 20
fi

# 3) Optional compatibility path for explicit auto/build modes: build a source snapshot.
if [[ -n "$SOURCE_DIR" ]]; then
  missing=()
  for c in cmake g++ make; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  if (( ${#missing[@]} )) && command -v apt-get >/dev/null 2>&1 && [[ "$(id -u)" -eq 0 ]]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential cmake
  fi
  for c in cmake g++ make; do command -v "$c" >/dev/null 2>&1 || { echo "ERROR: dataset-source CUDA build requires '$c'." >&2; exit 22; }; done
  grep -q 'LLM_ARCH_MUSE_GLIMMER' "$SOURCE_DIR/src/llama-arch.cpp" || { echo 'ERROR: attached llama.cpp source lacks Muse Glimmer support.' >&2; exit 23; }
  if ! resolve_cuda_toolkit; then
    echo 'ERROR: attached llama.cpp source is usable, but CUDA toolkit preflight failed; refusing an ambiguous source fallback.' >&2
    exit 26
  fi

  source_hash="$(python3 "$PROJECT_ROOT/scripts/source_tree_fingerprint.py" --root "$SOURCE_DIR")"
  key="${SOURCE_SNAPSHOT:-${source_hash:0:12}}"
  cache_root="${KAGGLE_SOURCE_BUILD_CACHE_DIR:-/opt/muse-llama-source-build}"
  build="$cache_root/$key/build"
  args=(
    -S "$SOURCE_DIR" -B "$build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
    -DGGML_CUDA=ON -DGGML_NATIVE=ON -DGGML_OPENMP=ON -DGGML_BLAS=OFF -DLLAMA_CURL=OFF
    "-DCMAKE_CUDA_ARCHITECTURES=$CUDA_ARCHITECTURES"
    "-DCUDAToolkit_ROOT=$CUDA_TOOLKIT_ROOT_RESOLVED"
    "-DCMAKE_CUDA_COMPILER=$CMAKE_CUDA_COMPILER"
  )
  printf -v cmake_args_text '%q ' "${args[@]}"
  cmake_args_text="${cmake_args_text% }"
  built_bin="$build/bin"
  source_build_cache_hit=0
  if [[ -x "$built_bin/llama-server" && -x "$built_bin/llama-cli" && -x "$built_bin/llama-bench" ]]; then
    source_build_cache_hit=1
  fi
  source_build_started="$(monotonic_now)"
  echo "Building attached llama.cpp source snapshot=${SOURCE_SNAPSHOT:-unknown} -> $build (cache_present=$source_build_cache_hit)"
  cmake "${args[@]}"
  jobs="$(nproc)"; (( jobs > 8 )) && jobs=8
  cmake --build "$build" --config Release -j"$jobs" --target llama-server llama-cli llama-bench
  source_build_seconds="$(elapsed_since "$source_build_started")"
  for bin in llama-server llama-cli llama-bench; do [[ -x "$built_bin/$bin" ]] || { echo "ERROR missing $built_bin/$bin" >&2; exit 24; }; done
  built_ld="$built_bin:$build:$build/lib"
  [[ -n "${LD_LIBRARY_PATH:-}" ]] && built_ld="$built_ld:$LD_LIBRARY_PATH"
  validate_runtime "$built_bin" "$built_ld" || { rc=$?; echo "ERROR: dataset-source built runtime failed validation (exit=$rc)." >&2; exit 25; }
  compiler="$(g++ --version 2>&1 | head -n1 || true)"
  cuda_version="$("$CUDA_NVCC_REALPATH" --version 2>&1 | tail -n1 || true)"
  LLAMA_RUNTIME_MANIFEST_VERIFIED_SELECTED=0; LLAMA_RUNTIME_MANIFEST_PATH_SELECTED=""; LLAMA_RUNTIME_SOURCE_COMMIT_SELECTED="${SOURCE_SNAPSHOT:-}"
  write_runtime_state "$built_bin" "$built_ld" kaggle_source_build "$SOURCE_DIR" "$SOURCE_SNAPSHOT" "$source_hash" "$cmake_args_text" "$compiler" "$cuda_version" "$build" "$source_build_seconds" "$source_build_cache_hit"
  echo "Using llama.cpp built from attached dataset source: $built_bin (source=kaggle_source_build snapshot=${SOURCE_SNAPSHOT:-unknown})"
  echo "$LLAMA_VALIDATED_VERSION"
  echo "$LLAMA_VALIDATED_DEVICES"
  exit 0
fi

if [[ -z "$DATASET_ROOT" ]]; then
  echo "Kaggle dataset '$KAGGLE_RUNTIME_DATASET' not found below $KAGGLE_INPUT_ROOT" >&2
else
  echo "Dataset found at $DATASET_ROOT but contains neither a usable prebuilt runtime nor a Muse-capable llama.cpp source snapshot." >&2
fi
exit 20
