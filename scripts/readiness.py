#!/usr/bin/env python3
"""Muse-Glimmer fresh-notebook readiness probe (strictly read-only).

Answers: "Can I safely attempt ./external.sh start? If not, what blocks me and
what should I fix first?".

Authority boundary: this is a diagnostic/read-only probe ONLY. It never starts,
stops, repairs, installs, copies, downloads, builds, exposes, benchmarks, or
mutates runtime/lifecycle/source state, and it never loads a GGUF or runs
inference. It never prints the API token.

Architecture (single canonical result object rendered as text/env/json):
    live probe layer  -> facts   (gathered read-only from config + attached inputs)
    assessment layer  -> result  (canonical statuses / blockers / warnings / action)
    renderer layer    -> text | env | json
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
RELEASE_FALLBACK = "unknown"

FROZEN_TARGET_FILE = "Muse-Glimmer-30B-Q4_K_M.gguf"
FROZEN_TARGET_SIZE = "17306324000"
FROZEN_TARGET_SHA = "0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384"
FROZEN_DRAFT_FILE = "Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf"
FROZEN_DRAFT_SIZE = "1631208128"
FROZEN_DRAFT_SHA = "b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef"
FROZEN_RUNTIME_COMMIT = "64f765f5adefa4620dddda436ce56f1430435536"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_default_env(root: Path) -> dict[str, str]:
    """Read config/default.env without sourcing common.sh (keeps the probe read-only)."""
    env: dict[str, str] = {}
    path = root / "config" / "default.env"
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw or raw.lstrip().startswith("#"):
            continue
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        if key and key.isupper() and key.replace("_", "").isalnum():
            env[key] = value.strip()
    return env


ENV_CONFIG_KEYS = (
    "KAGGLE_INPUT_ROOT",
    "KAGGLE_MODEL_DATASET",
    "DFLASH_KAGGLE_INPUT_ROOT",
    "DFLASH_KAGGLE_MODEL_DATASET",
    "KAGGLE_RUNTIME_DATASET",
    "CLOUDFLARED_BIN",
    "EXTERNAL_MODE",
    "EXPOSURE_MODE",
    "KAGGLE_RUNTIME_REQUIRE_MANIFEST",
    "MODEL_SOURCE_MODE", "MODEL_HF_REVISION", "MODEL_REPO", "MODELS_DIR",
)


def effective_config(root: Path, environ: dict | None = None) -> dict[str, str]:
    """Merge config/default.env with an explicit allowlist of env overrides."""
    cfg = load_default_env(root)
    cfg.setdefault("CLOUDFLARED_BIN", str(root / "vendor" / "bin" / "cloudflared"))
    env = os.environ if environ is None else environ
    for key in ENV_CONFIG_KEYS:
        if key in env and env[key] != "":
            cfg[key] = env[key]
    return cfg


def _target_input_root(cfg: dict) -> Path:
    return Path(cfg.get("KAGGLE_INPUT_ROOT", "/kaggle/input"))


def _dflash_input_root(cfg: dict) -> Path:
    return Path(
        cfg.get("DFLASH_KAGGLE_INPUT_ROOT")
        or cfg.get("KAGGLE_INPUT_ROOT", "/kaggle/input")
    )


# --------------------------------------------------------------------------- probe
def probe_manifest(root: Path) -> dict:
    checker = root / "scripts" / "source_manifest.py"
    manifest = root / "SOURCE_MANIFEST.sha256"
    if not checker.is_file() or not manifest.is_file():
        return {"status": "ERROR"}
    try:
        sm = _load_module("source_manifest", checker)
    except Exception:
        return {"status": "ERROR"}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ok = sm.check(root, manifest)
    except Exception:
        return {"status": "ERROR"}
    return {"status": "PASS" if ok else "FAIL"}


def _nvidia_device_nodes_present() -> bool:
    dev = Path("/dev")
    return any(p.name[6:].isdigit() for p in dev.glob("nvidia[0-9]*"))


def _resolve_nvidia_runtime_readonly(environ: dict | None = None) -> dict:
    """Reuse nvidia_runtime.sh in a child shell; never persist or mutate parent env."""
    env = dict(os.environ if environ is None else environ)
    helper = Path(__file__).resolve().parent / "nvidia_runtime.sh"
    if not helper.is_file():
        return {"status": "ERROR", "nvidia_smi": None, "env": env}
    bash = shutil.which("bash", path=env.get("PATH")) or "/bin/bash"
    script = r'''
source "$1"
if ! _nvidia_select_smi >/dev/null 2>&1; then
  printf 'ABSENT\0\0\0\0'
  exit 0
fi
if ! resolve_nvidia_runtime >/dev/null 2>&1; then
  printf 'ERROR\0\0\0\0'
  exit 0
fi
printf 'PASS\0%s\0%s\0%s\0' "$NVIDIA_SMI_PATH" "$PATH" "${LD_LIBRARY_PATH:-}"
'''
    try:
        proc = subprocess.run(
            [bash, "-c", script, "readiness-nvidia", str(helper)],
            check=True, capture_output=True, env=env,
        )
        fields = proc.stdout.decode("utf-8", errors="replace").split("\0")
    except (OSError, subprocess.SubprocessError):
        return {"status": "ERROR", "nvidia_smi": None, "env": env}
    if not fields:
        return {"status": "ERROR", "nvidia_smi": None, "env": env}
    status = fields[0]
    if status == "ABSENT":
        # A device node or an explicit NVIDIA_SMI means hardware/runtime was
        # expected; inability to resolve the management runtime is an error,
        # not proof that the host has zero GPUs.
        if env.get("NVIDIA_SMI") or _nvidia_device_nodes_present():
            status = "ERROR"
        return {"status": status, "nvidia_smi": None, "env": env}
    if status != "PASS" or len(fields) < 4:
        return {"status": "ERROR", "nvidia_smi": None, "env": env}
    child_env = dict(env)
    child_env["PATH"] = fields[2]
    child_env["LD_LIBRARY_PATH"] = fields[3]
    return {"status": "PASS", "nvidia_smi": fields[1], "env": child_env}

def probe_gpu() -> dict:
    runtime = _resolve_nvidia_runtime_readonly()
    if runtime["status"] == "ABSENT":
        # On a CPU notebook, no resolvable NVIDIA management runtime remains an
        # observed 0-GPU state. If nvidia-smi exists but cannot query the driver,
        # the resolver returns ERROR instead of misclassifying the hardware.
        return {"status": "PASS", "count": 0, "min_vram_observed_mib": None}
    if runtime["status"] != "PASS":
        return {"status": "ERROR", "count": None, "min_vram_observed_mib": None}
    try:
        gp = _load_module("gpu_probe", Path(__file__).resolve().parent / "gpu_probe.py")
        proc = subprocess.run(
            [
                runtime["nvidia_smi"],
                f"--query-gpu={gp.QUERY}",
                "--format=csv,noheader,nounits",
            ],
            check=True, text=True, capture_output=True, env=runtime["env"],
        )
        gpus = gp.parse_nvidia_smi_csv(proc.stdout)
    except Exception:
        return {"status": "ERROR", "count": None, "min_vram_observed_mib": None}
    min_vram = None
    if gpus:
        min_vram = min(g["memory_total_mib"] for g in gpus)
    return {"status": "PASS", "count": len(gpus), "min_vram_observed_mib": min_vram}


def probe_model(root: Path, kind: str, cfg: dict) -> dict:
    """Consult kaggle_runtime.discover_runtime for exact model presence/size/trusted-sha."""
    source_mode = (cfg.get("MODEL_SOURCE_MODE") or "auto").strip().lower()
    if source_mode == "hf":
        if kind == "target":
            repo_id = cfg.get("MODEL_REPO", "")
            model_file = cfg.get("MODEL_FILE", FROZEN_TARGET_FILE)
            revision = cfg.get("MODEL_HF_REVISION", "")
            expected_size = cfg.get("MODEL_SIZE_BYTES", FROZEN_TARGET_SIZE)
            expected_sha = cfg.get("MODEL_SHA256", FROZEN_TARGET_SHA)
        else:
            repo_id = cfg.get("DFLASH_HF_MODEL_REPO", "")
            model_file = cfg.get("DFLASH_MODEL_FILE", FROZEN_DRAFT_FILE)
            revision = cfg.get("DFLASH_HF_REVISION", "")
            expected_size = cfg.get("DFLASH_MODEL_SIZE_BYTES", FROZEN_DRAFT_SIZE)
            expected_sha = cfg.get("DFLASH_MODEL_SHA256", FROZEN_DRAFT_SHA)
        if not (repo_id and revision and model_file and expected_size and expected_sha):
            return {"kind": kind, "found": False, "selection": None, "size_match": "UNKNOWN", "trusted_sha_match": "UNKNOWN", "expected_sha": expected_sha, "expected_size": expected_size, "probe_error": True}
        model_root = Path(os.environ.get("MODELS_DIR", str(root / "models"))).expanduser()
        local_path = model_root / model_file
        size_bytes = local_path.stat().st_size if local_path.is_file() else None
        cache_ok = size_bytes == int(expected_size)
        return {"kind": kind, "found": True, "selection": "exact", "size_match": "YES", "trusted_sha_match": "UNKNOWN" if cache_ok else "YES", "sha_source": "startup_sha256_verification" if cache_ok else "pinned_hf_revision+configured_sha256", "expected_sha": expected_sha, "expected_size": expected_size, "size_bytes": size_bytes, "local_path": str(local_path), "source_mode": "hf", "hf_repo": repo_id, "hf_revision": revision, "download_required": not cache_ok, "probe_error": False}
    if kind == "target":
        dataset = cfg.get("KAGGLE_MODEL_DATASET", "")
        model_file = cfg.get("MODEL_FILE", FROZEN_TARGET_FILE)
        expected_size = cfg.get("MODEL_SIZE_BYTES", FROZEN_TARGET_SIZE)
        expected_sha = cfg.get("MODEL_SHA256", FROZEN_TARGET_SHA)
    else:
        dataset = cfg.get("DFLASH_KAGGLE_MODEL_DATASET", "")
        model_file = cfg.get("DFLASH_MODEL_FILE", FROZEN_DRAFT_FILE)
        expected_size = cfg.get("DFLASH_MODEL_SIZE_BYTES", FROZEN_DRAFT_SIZE)
        expected_sha = cfg.get("DFLASH_MODEL_SHA256", FROZEN_DRAFT_SHA)

    input_root = _target_input_root(cfg) if kind == "target" else _dflash_input_root(cfg)
    probe_error = False
    if not dataset:
        return {"kind": kind, "found": False, "selection": None, "size_match": "NO",
                "trusted_sha_match": "UNKNOWN", "sha_source": None, "expected_sha": expected_sha,
                "expected_size": expected_size, "probe_error": False}
    try:
        kr = _load_module("kaggle_runtime", Path(__file__).resolve().parent / "kaggle_runtime.py")
        info = kr.discover_runtime(input_root, dataset, model_file, [])
    except Exception:
        return {"kind": kind, "found": False, "selection": None, "size_match": "UNKNOWN",
                "trusted_sha_match": "UNKNOWN", "sha_source": None, "expected_sha": expected_sha,
                "expected_size": expected_size, "probe_error": True}

    if not info.get("model_found") or not info.get("model_path"):
        return {"kind": kind, "found": False, "selection": None, "size_match": "NO",
                "trusted_sha_match": "UNKNOWN", "sha_source": None, "expected_sha": expected_sha,
                "expected_size": expected_size, "probe_error": False}

    size = info.get("model_size_bytes")
    size_match = "YES" if size == int(expected_size) else "NO"
    sha_source = info.get("model_sha256_source")
    trusted_sha = info.get("model_sha256")
    if sha_source == "SHA256SUMS" and trusted_sha:
        trusted_sha_match = "YES" if trusted_sha == expected_sha else "NO"
    else:
        trusted_sha_match = "UNKNOWN"
    return {
        "kind": kind,
        "found": True,
        "selection": info.get("model_selection"),
        "size_match": size_match,
        "trusted_sha_match": trusted_sha_match,
        "sha_source": sha_source,
        "size_bytes": size,
        "expected_sha": expected_sha,
        "expected_size": expected_size,
        "probe_error": False,
    }


def probe_runtime(root: Path, cfg: dict) -> dict:
    """Read-only probe of the trusted prebuilt runtime fast-path vs source fallback.

    Prebuilt binaries only count as a fast path when their trusted manifest
    (runtime_manifest.verify_manifest) validates against the pinned repo/commit/arch.
    Never executes, copies, or builds any binary.
    """
    src_mode = (cfg.get("LLAMA_SOURCE_MODE") or "auto").strip().lower()
    dataset = cfg.get("KAGGLE_RUNTIME_DATASET", "")
    if not dataset:
        return {"prebuilt_present": False, "prebuilt_trusted": False, "source_build": False,
                "source_mode": src_mode, "error": False}
    input_root = _target_input_root(cfg)
    try:
        kr = _load_module("kaggle_runtime", Path(__file__).resolve().parent / "kaggle_runtime.py")
        info = kr.discover_runtime(input_root, dataset, FROZEN_TARGET_FILE, [])
    except Exception:
        return {"prebuilt_present": False, "prebuilt_trusted": False, "source_build": False,
                "source_mode": src_mode, "error": True}

    prebuilt = bool(info.get("llama_runtime_found"))
    bin_dir = info.get("llama_bin_dir")
    dataset_root = info.get("dataset_root")
    require_manifest = cfg.get("KAGGLE_RUNTIME_REQUIRE_MANIFEST", "1") != "0"
    trusted = False
    if prebuilt and require_manifest and bin_dir and dataset_root:
        manifest_name = cfg.get("KAGGLE_RUNTIME_MANIFEST_FILE", "runtime-manifest.json")
        manifest_path = Path(dataset_root) / manifest_name
        expected_repo = cfg.get("LLAMA_CPP_REPO", "https://github.com/z-lab/llama.cpp-fork.git")
        expected_commit = cfg.get("LLAMA_CPP_EXPECTED_COMMIT") or FROZEN_RUNTIME_COMMIT
        required_arch = cfg.get("CUDA_ARCHITECTURES", "75")
        try:
            rm = _load_module("runtime_manifest", Path(__file__).resolve().parent / "runtime_manifest.py")
            rm.verify_manifest(manifest_path, bin_dir, expected_repo, expected_commit, required_arch)
            trusted = True
        except Exception:
            trusted = False
    elif prebuilt:
        # operator disabled the manifest gate; dataset presence is the trust carrier
        trusted = True

    return {
        "prebuilt_present": prebuilt,
        "prebuilt_trusted": trusted,
        "source_build": bool(info.get("llama_source_found")),
        "source_mode": src_mode,
        "error": False,
    }


def _token_ok(token: str) -> bool:
    """Exact frozen security policy (auth_proxy.valid_secret / external.sh credential_preflight):
    length >= 32 and all characters printable (code 32..126, excluding DEL)."""
    return len(token) >= 32 and all(32 <= ord(ch) <= 126 for ch in token)


def probe_token() -> dict:
    token = os.environ.get("MUSE_API_TOKEN", "")
    if not token:
        return {"state": "MISSING"}
    if not _token_ok(token):
        return {"state": "WEAK"}
    return {"state": "PRESENT"}


def probe_cloudflared(mode: str, cfg: dict) -> dict:
    if mode == "proxy":
        return {"present": False, "required": False}
    binary = cfg.get("CLOUDFLARED_BIN", "cloudflared")
    if "/" in binary:
        present = Path(binary).is_file() and os.access(binary, os.X_OK)
    else:
        present = shutil.which(binary) is not None
    return {"present": present, "required": True}


def probe_named(cfg: dict) -> dict:
    public_url = os.environ.get("EXPOSURE_PUBLIC_URL", "")
    tunnel_token = os.environ.get("TUNNEL_TOKEN", "")
    url_ok = _is_clean_https_origin(public_url)
    return {
        "tunnel_token_present": bool(tunnel_token),
        "public_url": public_url,
        "public_url_ok": url_ok,
    }


def _is_clean_https_origin(value: str) -> bool:
    if not value:
        return False
    try:
        u = urlsplit(value)
        _ = u.port
    except ValueError:
        return False
    return (
        u.scheme == "https"
        and bool(u.hostname)
        and not u.username
        and not u.password
        and u.path in ("", "/")
        and not u.query
        and not u.fragment
    )


def determine_mode(cfg: dict) -> str:
    mode = os.environ.get("EXTERNAL_MODE") or os.environ.get("EXPOSURE_MODE") or cfg.get(
        "EXTERNAL_MODE") or cfg.get("EXPOSURE_MODE") or "quick"
    mode = mode.strip().lower()
    if mode not in ("quick", "proxy", "named"):
        return "quick"
    return mode


# --------------------------------------------------------------------------- assess
def assess(facts: dict) -> dict:
    mode = facts.get("mode", "quick") or "quick"
    blockers: list[str] = []
    warnings: list[str] = []
    error = False

    # source manifest
    manifest = facts.get("manifest", {}).get("status", "ERROR")
    if manifest == "ERROR":
        error = True
    elif manifest == "FAIL":
        blockers.append("SOURCE_MANIFEST")

    # gpu topology: count AND per-GPU minimum VRAM must satisfy the operator contract
    required = facts.get("required_gpu_count", 2)
    required_vram = facts.get("gpu_min_vram_required_mib")
    gpu_count = facts.get("gpu_count")
    min_vram = facts.get("gpu_min_vram_observed_mib")
    if gpu_count is None:
        error = True
    else:
        vram_ok = True
        if required_vram is not None and gpu_count > 0 and min_vram is not None:
            vram_ok = min_vram >= required_vram
        if gpu_count < required or not vram_ok:
            blockers.append("GPU_TOPOLOGY")

    # target model
    target = facts.get("target", {})
    t_status, t_blocker, t_warning = _model_status("TARGET", target)
    if t_status == "ERROR":
        error = True
    if t_blocker:
        blockers.append(t_blocker)
    if t_warning:
        warnings.append(t_warning)

    # draft model
    draft = facts.get("draft", {})
    d_status, d_blocker, d_warning = _model_status("DFLASH", draft)
    if d_status == "ERROR":
        error = True
    if d_blocker:
        blockers.append(d_blocker)
    if d_warning:
        warnings.append(d_warning)

    # runtime: trusted prebuilt fast-path vs source-build fallback
    runtime = facts.get("runtime", {})
    r_error = runtime.get("error", False)
    prebuilt = runtime.get("prebuilt_present", False)
    trusted = runtime.get("prebuilt_trusted", False)
    source_build = runtime.get("source_build", False)
    src_mode = (runtime.get("source_mode") or "auto").strip().lower()
    fallback_possible = source_build or src_mode in ("auto", "build")
    if r_error:
        error = True
        runtime_fast_path = "ERROR"
        source_build_fallback = "UNKNOWN"
    elif prebuilt and trusted:
        runtime_fast_path = "PASS"
    elif prebuilt and not trusted:
        runtime_fast_path = "FAIL"
        blockers.append("RUNTIME_UNAVAILABLE")
    else:
        runtime_fast_path = "UNAVAILABLE"
        if fallback_possible:
            warnings.append("FAST_START_RUNTIME_NOT_ATTACHED")
        else:
            blockers.append("RUNTIME_UNAVAILABLE")
    if not r_error:
        if fallback_possible:
            source_build_fallback = "POSSIBLE"
        elif src_mode == "kaggle":
            source_build_fallback = "UNAVAILABLE"
        else:
            source_build_fallback = "UNKNOWN"

    # api token
    token = facts.get("token", {}).get("state", "ERROR")
    if token == "MISSING":
        blockers.append("API_TOKEN_MISSING")
    elif token == "WEAK":
        blockers.append("API_TOKEN_WEAK")
    elif token == "ERROR":
        error = True

    # cloudflared
    cloud = facts.get("cloudflared", {"present": False, "required": False})
    if cloud.get("required") and not cloud.get("present"):
        blockers.append("CLOUDFLARED_MISSING")

    # named mode prerequisites (exact frozen external.sh contract)
    if mode == "named":
        named = facts.get("named", {})
        if not named.get("tunnel_token_present"):
            blockers.append("NAMED_MODE_TOKEN")
        if not named.get("public_url_ok"):
            blockers.append("NAMED_MODE_PUBLIC_URL")

    if error:
        start = "ERROR"
    elif blockers:
        start = "FAIL"
    else:
        start = "PASS"

    return {
        "schema_version": SCHEMA_VERSION,
        "release_version": facts.get("release_version", RELEASE_FALLBACK),
        "mode": mode,
        "start_readiness": start,
        "blockers": blockers,
        "warnings": warnings,
        "next_action_code": _next_action(start, blockers, mode),
        "source_manifest": manifest,
        "gpu_topology": _gpu_topology(gpu_count, required, required_vram, min_vram),
        "gpu_count": gpu_count,
        "required_gpu_count": required,
        "gpu_min_vram_observed_mib": min_vram,
        "gpu_min_vram_required_mib": required_vram,
        "target": _result_model("TARGET", target),
        "draft": _result_model("DFLASH", draft),
        "runtime_fast_path": runtime_fast_path,
        "source_build_fallback": source_build_fallback,
        "api_token": token,
        "cloudflared": ("PRESENT" if cloud.get("present") else ("NOT_REQUIRED" if not cloud.get("required") else "MISSING")),
    }


def _gpu_topology(gpu_count, required_count, required_vram, min_vram) -> str:
    """Return PASS/FAIL/ERROR for the GPU topology given count + min-VRAM contract."""
    if gpu_count is None:
        return "ERROR"
    if gpu_count < required_count:
        return "FAIL"
    if required_vram is not None and gpu_count > 0 and min_vram is not None and min_vram < required_vram:
        return "FAIL"
    return "PASS"


def _model_status(kind: str, model: dict) -> tuple[str, str | None, str | None]:
    if model.get("probe_error"):
        return "ERROR", None, None
    if model.get("download_required"):
        return "DOWNLOAD_REQUIRED", None, f"{kind}_HF_DOWNLOAD_REQUIRED"
    if not model.get("found"):
        missing = "TARGET_MODEL_MISSING" if kind == "TARGET" else "DFLASH_MODEL_MISSING"
        return "MISSING", missing, None
    size_match = model.get("size_match", "UNKNOWN")
    sha_match = model.get("trusted_sha_match", "UNKNOWN")
    if size_match == "NO" or sha_match == "NO":
        identity = "TARGET_IDENTITY" if kind == "TARGET" else "DFLASH_IDENTITY"
        return "IDENTITY_FAIL", identity, None
    warning = None
    if model.get("selection") and model.get("selection") != "exact":
        warning = f"{kind}_NON_EXACT_SELECTION"
    return "PRESENT", None, warning


def _result_model(kind: str, model: dict) -> dict:
    return {
        "status": _model_status(kind, model)[0],
        "size_match": model.get("size_match", "UNKNOWN"),
        "trusted_sha_match": model.get("trusted_sha_match", "UNKNOWN"),
        "full_runtime_verification": "DEFERRED_TO_SERVE",
        "source_mode": model.get("source_mode", "kaggle"),
        "hf_repo": model.get("hf_repo"),
        "hf_revision": model.get("hf_revision"),
        "local_path": model.get("local_path"),
        "download_required": bool(model.get("download_required", False)),
    }


def _next_action(start: str, blockers: list[str], mode: str) -> str:
    if start == "PASS":
        return "RUN_EXTERNAL_START"
    if not blockers:
        return "RERUN_AFTER_RESOLUTION"
    first = blockers[0]
    if first == "SOURCE_MANIFEST":
        return "FIX_SOURCE_MANIFEST"
    if first == "GPU_TOPOLOGY":
        return "ATTACH_GPU"
    if first in ("TARGET_MODEL_MISSING", "TARGET_IDENTITY"):
        return "ATTACH_TARGET_MODEL"
    if first in ("DFLASH_MODEL_MISSING", "DFLASH_IDENTITY"):
        return "ATTACH_DFLASH_MODEL"
    if first == "RUNTIME_UNAVAILABLE":
        return "ATTACH_RUNTIME"
    if first in ("API_TOKEN_MISSING", "API_TOKEN_WEAK"):
        return "FIX_API_TOKEN"
    if first == "CLOUDFLARED_MISSING":
        return "ATTACH_CLOUDFLARED"
    if first.startswith("NAMED_MODE"):
        return "FIX_NAMED_MODE"
    return "RERUN_AFTER_RESOLUTION"


# --------------------------------------------------------------------------- render
def render_env(result: dict, target: dict, draft: dict) -> str:
    fields = {
        "SCHEMA_VERSION": result["schema_version"],
        "RELEASE_VERSION": result["release_version"],
        "MODE": result["mode"],
        "START_READINESS": result["start_readiness"],
        "BLOCKERS": ",".join(result["blockers"]),
        "WARNINGS": ",".join(result["warnings"]),
        "NEXT_ACTION_CODE": result["next_action_code"],
        "SOURCE_MANIFEST": result["source_manifest"],
        "GPU_TOPOLOGY": result["gpu_topology"],
        "GPU_COUNT": result["gpu_count"] if result["gpu_count"] is not None else "unknown",
        "REQUIRED_GPU_COUNT": result["required_gpu_count"],
        "GPU_MIN_VRAM_MIB": result.get("gpu_min_vram_required_mib", 14000),
        "GPU_VRAM_MIN_OBSERVED_MIB": result.get("gpu_min_vram_observed_mib") if result.get("gpu_min_vram_observed_mib") is not None else "unknown",
        "TARGET_MODEL": target["status"],
        "TARGET_SIZE_MATCH": target["size_match"],
        "TARGET_TRUSTED_METADATA_SHA_MATCH": target["trusted_sha_match"],
        "TARGET_FULL_RUNTIME_VERIFICATION": target["full_runtime_verification"],
        "DFLASH_MODEL": draft["status"],
        "DFLASH_SIZE_MATCH": draft["size_match"],
        "DFLASH_TRUSTED_METADATA_SHA_MATCH": draft["trusted_sha_match"],
        "DFLASH_FULL_RUNTIME_VERIFICATION": draft["full_runtime_verification"],
        "RUNTIME_FAST_PATH": result["runtime_fast_path"],
        "SOURCE_BUILD_FALLBACK": result["source_build_fallback"],
        "API_TOKEN": result["api_token"],
        "CLOUDFLARED": result["cloudflared"],
    }
    return "".join(f"{key}={value}\n" for key, value in fields.items())


def render_text(result: dict) -> str:
    t = result["target"]
    d = result["draft"]
    gpu_count = result["gpu_count"] if result["gpu_count"] is not None else "unknown"
    lines = [
        "Muse-Glimmer Kaggle readiness",
        f"Release:              {result['release_version']}",
        f"Source manifest:      {result['source_manifest']}",
        "",
        f"GPU topology:         {result['gpu_topology']}",
        f"GPU count:            {gpu_count}",
        f"Required GPU count:   {result['required_gpu_count']}",
        f"GPU min VRAM:         {result.get('gpu_min_vram_observed_mib') if result.get('gpu_min_vram_observed_mib') is not None else 'unknown'} MiB (required {result.get('gpu_min_vram_required_mib', 14000)} MiB)",
        "",
        f"Target model:         {t['status']}",
        f"Draft model:          {d['status']}",
        "",
        f"Fast runtime:         {result['runtime_fast_path']}",
        f"External mode:        {result['mode']}",
        f"API token:            {result['api_token']}",
        f"cloudflared:          {result['cloudflared']}",
        "",
        f"START_READINESS:      {result['start_readiness']}",
    ]
    if result["blockers"]:
        lines.append(f"BLOCKER:              {result['blockers'][0]}")
    if result["warnings"]:
        lines.append(f"WARNING:              {','.join(result['warnings'])}")
    lines.append("")
    lines.append("Next action:")
    lines.append(f"  {_next_action_hint(result)}")
    return "\n".join(lines) + "\n"


def _next_action_hint(result: dict) -> str:
    if result["start_readiness"] == "PASS":
        return "./external.sh start"
    first = result["blockers"][0] if result["blockers"] else ""
    if first == "SOURCE_MANIFEST":
        return "Restore the canonical source manifest, then rerun ./readiness.sh."
    if first == "GPU_TOPOLOGY":
        return "Attach a Kaggle GPU runtime with >=2 GPUs (>=14000 MiB each), then rerun ./readiness.sh."
    if first in ("TARGET_IDENTITY", "DFLASH_IDENTITY"):
        return "Attach the exact Muse-Glimmer model matching the frozen identity, then rerun ./readiness.sh."
    if first in ("TARGET_MODEL_MISSING", "DFLASH_MODEL_MISSING"):
        return "Attach the required Muse-Glimmer Kaggle Model, then rerun ./readiness.sh."
    if first == "RUNTIME_UNAVAILABLE":
        return "Attach a Muse-Glimmer llama.cpp runtime or a buildable source tree, then rerun ./readiness.sh."
    if first in ("API_TOKEN_MISSING", "API_TOKEN_WEAK"):
        return "Set a valid MUSE_API_TOKEN (>=32 printable chars) and rerun ./readiness.sh."
    if first == "CLOUDFLARED_MISSING":
        return "Install/provide cloudflared (or use proxy mode) and rerun ./readiness.sh."
    if first.startswith("NAMED_MODE"):
        return "Set TUNNEL_TOKEN and a clean HTTPS EXPOSURE_PUBLIC_URL, then rerun ./readiness.sh."
    return "Review the blockers, resolve, then rerun ./readiness.sh."


def render_json(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=True) + "\n"


def build_facts(root: Path, mode: str, cfgs: dict | None = None, gpu_probe_fn=None) -> dict:
    cfg = cfgs if cfgs is not None else effective_config(root)
    version_file = root / "VERSION"
    release_version = version_file.read_text(encoding="utf-8", errors="replace").strip() if version_file.is_file() else RELEASE_FALLBACK
    facts = {
        "mode": mode,
        "release_version": release_version,
    }
    try:
        facts["manifest"] = probe_manifest(root)
    except Exception:
        facts["manifest"] = {"status": "ERROR"}
    try:
        if gpu_probe_fn is not None:
            gpu_facts = gpu_probe_fn() or {}
        else:
            gpu_facts = probe_gpu()
        facts["gpu_count"] = gpu_facts.get("count")
        facts["gpu_min_vram_observed_mib"] = gpu_facts.get("min_vram_observed_mib")
    except Exception:
        facts["gpu_count"] = None
        facts["gpu_min_vram_observed_mib"] = None
    facts["required_gpu_count"] = int(cfg.get("GPU_REQUIRED_COUNT", "2"))
    facts["gpu_min_vram_required_mib"] = int(cfg.get("GPU_MIN_VRAM_MIB", "14000"))
    facts["target"] = probe_model(root, "target", cfg)
    facts["draft"] = probe_model(root, "draft", cfg)
    facts["runtime"] = probe_runtime(root, cfg)
    facts["token"] = probe_token()
    facts["cloudflared"] = probe_cloudflared(mode, cfg)
    facts["named"] = probe_named(cfg) if mode == "named" else {}
    return facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Muse-Glimmer fresh-notebook readiness probe")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--mode", choices=("quick", "proxy", "named"), default=None,
                    help="external mode (default: EXTERNAL_MODE/EXPOSURE_MODE env or quick)")
    ap.add_argument("--format", choices=("text", "env", "json"), default="text")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    cfg = effective_config(root)
    mode = args.mode or determine_mode(cfg)
    try:
        facts = build_facts(root, mode, cfg)
    except Exception as exc:
        print(f"ERROR: readiness assessment could not be determined: {exc}", file=sys.stderr)
        return 3
    result = assess(facts)
    render = {k: v for k, v in result.items()}
    render["target"] = _result_model("TARGET", facts["target"])
    render["draft"] = _result_model("DFLASH", facts["draft"])
    if args.format == "json":
        sys.stdout.write(render_json(render))
    elif args.format == "env":
        sys.stdout.write(render_env(render, render["target"], render["draft"]))
    else:
        sys.stdout.write(render_text(render))
    if result["start_readiness"] == "PASS":
        return 0
    if result["start_readiness"] == "ERROR":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
