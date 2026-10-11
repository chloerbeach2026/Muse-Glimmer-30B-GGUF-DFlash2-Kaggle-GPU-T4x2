#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/scripts/common.sh"

explicit="${DFLASH_MODEL_PATH:-}"
filename="${DFLASH_MODEL_FILE:-Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf}"
repo_id="${DFLASH_HF_MODEL_REPO:-meta-models/Muse-Glimmer-30B-GGUF}"
revision="${DFLASH_HF_REVISION:-43c7eadd41352a299ea8e0a36b3157978dd63596}"
hf_filename="${DFLASH_HF_FILE:-dflash-Muse-Glimmer-30B-Q4_K_M.gguf}"
expected_size="${DFLASH_MODEL_SIZE_BYTES:-}"
expected_sha="${DFLASH_MODEL_SHA256:-}"
verify_sha="${VERIFY_DFLASH_SHA256:-1}"
[[ "$verify_sha" == 0 || "$verify_sha" == 1 ]] || { echo 'ERROR: VERIFY_DFLASH_SHA256 must be 0 or 1' >&2; exit 2; }

validate() {
  local path="$1" actual_size actual_sha
  [[ -s "$path" ]] || { echo "ERROR: DFlash2 model does not exist or is empty: $path" >&2; return 2; }
  actual_size="$(stat -Lc '%s' "$path")"
  if [[ -n "$expected_size" && "$actual_size" != "$expected_size" ]]; then
    echo "ERROR: DFlash2 size mismatch for $path: expected=$expected_size actual=$actual_size" >&2
    return 3
  fi
  actual_sha="$(sha256sum "$path" | awk '{print $1}')"
  if [[ "$verify_sha" == 1 && -n "$expected_sha" && "$actual_sha" != "$expected_sha" ]]; then
    echo "ERROR: DFlash2 SHA256 mismatch for $path: expected=$expected_sha actual=$actual_sha" >&2
    return 4
  fi
  {
    printf 'DFLASH_RESOLVED_PATH=%q\n' "$path"
    printf 'DFLASH_RESOLVED_FILE=%q\n' "$(basename "$path")"
    printf 'DFLASH_RESOLVED_SHA256=%q\n' "$actual_sha"
    printf 'DFLASH_RESOLVED_SIZE_BYTES=%q\n' "$actual_size"
    printf 'DFLASH_HF_REPO=%q\n' "$repo_id"
    printf 'DFLASH_HF_REVISION=%q\n' "$revision"
  } > "$RUNTIME_STATE_DIR/dflash.env"
  printf '%s  %s\n' "$actual_sha" "$path" > "$RUNTIME_STATE_DIR/dflash.sha256"
  python3 - "$path" <<'PY'
import os,sys
print(os.path.abspath(sys.argv[1]))
PY
}

if [[ -n "$explicit" ]]; then
  echo 'ERROR: DFLASH_MODEL_PATH overrides are disabled; use the configured Hugging Face repo/file.' >&2
  exit 2
fi

local_path="$MODELS_DIR/$filename"
if [[ "${FORCE_MODEL_DOWNLOAD:-0}" != 1 && -s "$local_path" ]]; then
  validate "$local_path"
  exit $?
fi
mkdir -p "$MODELS_DIR" "$RUNTIME_STATE_DIR"
hf_download_asset "$repo_id" "$hf_filename" "$revision" "$MODELS_DIR"
downloaded="$MODELS_DIR/$hf_filename"
[[ "$downloaded" == "$local_path" ]] || mv -f "$downloaded" "$local_path"
validate "$local_path"
