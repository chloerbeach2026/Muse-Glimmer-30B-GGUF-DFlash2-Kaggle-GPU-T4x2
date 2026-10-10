#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/scripts/common.sh"

MODEL_PATH="$MODELS_DIR/$MODEL_FILE"
CONFIG_EXPECTED_SIZE="$(model_expected_size)"
CONFIG_EXPECTED_SHA="$(model_expected_sha256)"
MODEL_STATE="$RUNTIME_STATE_DIR/model.env"
mkdir -p "$MODELS_DIR" "$RUNTIME_STATE_DIR"

verify_file() {
  local path="$1" expected_size="${2:-}" expected_sha="${3:-}" size actual
  [[ -s "$path" ]] || return 1
  size="$(stat -Lc '%s' "$path" 2>/dev/null || stat -Lf '%z' "$path")"
  if [[ -n "$expected_size" && "$size" != "$expected_size" ]]; then
    echo "VERIFY: size mismatch for $path: got=$size expected=$expected_size" >&2
    return 1
  fi
  if [[ "${VERIFY_MODEL_SHA256:-1}" == 1 && -n "$expected_sha" ]]; then
    actual="$(sha256sum "$path" | awk '{print $1}')"
    if [[ "$actual" != "$expected_sha" ]]; then
      echo "VERIFY: sha256 mismatch for $path: got=$actual expected=$expected_sha" >&2
      return 1
    fi
  fi
  return 0
}

write_model_state() {
  local path="$1" source="$2" revision="$3" verification_seconds="$4" digest size
  size="$(stat -Lc '%s' "$path")"
  digest="$(sha256sum "$path" | awk '{print $1}')"
  {
    printf 'MODEL_RESOLVED_PATH=%q\n' "$path"
    printf 'MODEL_RESOLVED_FILE=%q\n' "$(basename "$path")"
    printf 'MODEL_RUNTIME_SOURCE=%q\n' "$source"
    printf 'MODEL_RUNTIME_DATASET_ROOT=%q\n' ""
    printf 'MODEL_RESOLVED_SIZE_BYTES=%q\n' "$size"
    printf 'MODEL_RESOLVED_SHA256=%q\n' "$digest"
    printf 'MODEL_METADATA_SOURCE=%q\n' "huggingface_hub"
    printf 'MODEL_SELECTION=%q\n' exact
    printf 'MODEL_HF_REPO=%q\n' "$MODEL_REPO"
    printf 'MODEL_HF_REVISION=%q\n' "$revision"
    printf 'MODEL_VERIFICATION_SECONDS=%q\n' "$verification_seconds"
  } > "$MODEL_STATE"
  printf '%s  %s\n' "$digest" "$path" > "$RUNTIME_STATE_DIR/model.sha256"
}

case "${MODEL_SOURCE_MODE:-hf}" in hf) ;; *) echo "ERROR: model source is HF-only; MODEL_SOURCE_MODE must be hf (got '${MODEL_SOURCE_MODE:-unset}')." >&2; exit 2;; esac
[[ -n "${MODEL_REPO:-}" && -n "${MODEL_FILE:-}" ]] || { echo 'ERROR: MODEL_REPO and MODEL_FILE are required.' >&2; exit 2; }
[[ -n "${MODEL_HF_REVISION:-}" ]] || { echo 'ERROR: MODEL_HF_REVISION must be a branch, tag, or commit.' >&2; exit 2; }

if [[ "${FORCE_MODEL_DOWNLOAD:-0}" == 1 ]]; then rm -f "$MODEL_PATH" "$MODELS_DIR/${MODEL_FILE}.sha256"; fi
if [[ -f "$MODEL_PATH" ]]; then
  echo "Existing HF model cache: $MODEL_PATH"
  verify_started="$(monotonic_now)"
  if verify_file "$MODEL_PATH" "$CONFIG_EXPECTED_SIZE" "$CONFIG_EXPECTED_SHA"; then
    verification_seconds="$(elapsed_since "$verify_started")"
    write_model_state "$MODEL_PATH" hf_verified_cache "$MODEL_HF_REVISION" "$verification_seconds"
    ls -lh "$MODEL_PATH"
    exit 0
  fi
  echo 'Existing model cache failed configured identity verification; removing invalid file.' >&2
  rm -f "$MODEL_PATH"
fi

download_started="$(monotonic_now)"
hf_download_asset "$MODEL_REPO" "$MODEL_FILE" "$MODEL_HF_REVISION" "$MODELS_DIR"
verify_started="$(monotonic_now)"
verify_file "$MODEL_PATH" "$CONFIG_EXPECTED_SIZE" "$CONFIG_EXPECTED_SHA" || {
  echo 'ERROR: final Hugging Face target verification failed; refusing to start server.' >&2
  exit 4
}
verification_seconds="$(elapsed_since "$verify_started")"
write_model_state "$MODEL_PATH" huggingface_download "$MODEL_HF_REVISION" "$verification_seconds"
sha256sum "$MODEL_PATH" | tee "$MODELS_DIR/${MODEL_FILE}.sha256"
echo "MODEL_DOWNLOAD_SECONDS=$(elapsed_since "$download_started")"
ls -lh "$MODEL_PATH"
