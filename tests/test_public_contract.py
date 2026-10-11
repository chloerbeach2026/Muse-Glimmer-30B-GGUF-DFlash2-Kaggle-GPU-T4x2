#!/usr/bin/env python3
"""CPU-safe public contract tests (no GPU, model, tunnel, or network).

Asserts deterministic facts about the public tree and the gateway contract so
CI can verify them without any runtime workload.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_VERSION = "1.0.0"
FAILURES = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    status = "ok" if ok else "FAIL"
    print(f"{status}   {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)



def exercise_venv_fallback_log_transport() -> dict[str, object]:
    '''Exercise the real bootstrap through stdout command-substitution.

    This models the external.sh/serve.sh path that previously consumed the
    marker on stdout while stderr remained visible to the notebook.
    '''
    bootstrap = ROOT / "scripts" / "bootstrap_venv.sh"
    raw_normal = "Error: Command simulated ensurepip failure"
    raw_fallback = "Error: simulated fallback venv failure"

    def run_case(*, fail_fallback: bool) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td_text:
            td = Path(td_text)
            fake_python = td / "python3"
            fake_python.write_text(
                f'''#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  if [[ " $* " != *" --without-pip "* ]]; then
    echo "{raw_normal}" >&2
    exit 1
  fi
  if [[ "{str(fail_fallback).lower()}" == "true" ]]; then
    echo "{raw_fallback}" >&2
    exit 9
  fi
  venv_dir="${{@: -1}}"
  mkdir -p "$venv_dir/bin"
  cat > "$venv_dir/bin/python" <<'VENV_PY'
#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "--version" ]]; then
  echo "pip 25.0 from synthetic-ci-venv"
  exit 0
fi
exit 0
VENV_PY
  chmod +x "$venv_dir/bin/python"
  exit 0
fi
if [[ "${{1:-}}" == "-" && "$#" -eq 2 ]]; then
  printf '%s/lib/python3.12/site-packages\n' "$2"
  exit 0
fi
if [[ "${{1:-}}" == "-" ]]; then
  exit 20
fi
exit 0
''',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = dict(os.environ)
            env["PYTHON_BIN"] = str(fake_python)
            env["VENV_DIR"] = str(td / "venv")
            # stdout is deliberately consumed by command substitution; stderr
            # is left visible to the parent, exactly matching the regression.
            shell = 'captured="$(bash "$BOOTSTRAP")"; rc=$?; printf "%s\\n" "$captured"; exit "$rc"'
            env["BOOTSTRAP"] = str(bootstrap)
            return subprocess.run(
                ["bash", "-c", shell],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    success = run_case(fail_fallback=False)
    failure = run_case(fail_fallback=True)
    success_combined = success.stdout + success.stderr
    failure_combined = failure.stdout + failure.stderr
    return {
        "success_rc": success.returncode,
        "success_stdout": success.stdout,
        "success_stderr": success.stderr,
        "success_combined": success_combined,
        "failure_rc": failure.returncode,
        "failure_combined": failure_combined,
        "raw_normal": raw_normal,
        "raw_fallback": raw_fallback,
    }

def main() -> int:
    version = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
    check("project version singleton", version == PUBLIC_VERSION, f"VERSION={version!r}")
    readme_en = ROOT.joinpath("README.md").read_text(encoding="utf-8")
    readme_vi = ROOT.joinpath("README.vi.md").read_text(encoding="utf-8")

    notebook_path = ROOT / "notebooks" / "kaggle-production.ipynb
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    notebook_meta = notebook.get("metadata", {}).get("muse_glimmer", {})
    notebook_code = "\n".join(
        "".join(cell.get("source", "")) if isinstance(cell.get("source", ""), list) else cell.get("source", "")
        for cell in notebook.get("cells", [])
        if cell.get("cell_type") == "code"
    )
    check("notebook fork identity metadata is commit based",
          notebook_meta.get("expected_version") == PUBLIC_VERSION
          and notebook_meta.get("source_identity_contract") == "fork_main_commit+version+clean_worktree+source_manifest"
          and "expected_head" not in notebook_meta
          and "expected_tree" not in notebook_meta)
    check("notebook release identity removes hardcoded commit and tree pins",
          "EXPECTED_HEAD" not in notebook_code
          and "EXPECTED_TREE" not in notebook_code)
    check("notebook pins this fork main branch to the exact resolved commit",
          'REPO = "chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2"' in notebook_code
          and 'SOURCE_REF = "main"' in notebook_code
          and '"git", "ls-remote", "--heads", REPO_URL' in notebook_code
          and '"--branch", SOURCE_REF' in notebook_code
          and 'assert head == resolved_source_sha' in notebook_code)
    check("notebook source identity verifies version, manifest and clean worktree",
          'release_version == EXPECTED_VERSION' in notebook_code
          and 'SESSION["resolved_source_sha"] = resolved_source_sha' in notebook_code
          and 'SESSION["worktree_clean"] = True' in notebook_code
          and 'SESSION.get("source_manifest") == "PASS"' in notebook_code)
    check("project-owned repository references use the fork",
          "chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2" in readme_en
          and 'REPO = "chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2"' in notebook_code
          and "dangkhoa2016/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2" not in notebook_code)


    check("cloudflared release is pinned to an exact official version and SHA-256",
          notebook_meta.get("cloudflared_supply_chain_contract") == "release_tag+official_sha256+exact_version"
          and 'CLOUDFLARED_VERSION = "2026.10.0"' in notebook_code
          and 'CLOUDFLARED_EXPECTED_SHA256 = "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db"' in notebook_code
          and "releases/download/" in notebook_code
          and "releases/latest/download" not in notebook_code)
    cloud_hash_guard = "if digest != CLOUDFLARED_EXPECTED_SHA256:"
    cloud_version_probe = 'probe = run([str(candidate_path), "--version"], check=False)'
    cloud_download_verify = 'temp_candidate = verify_cloudflared_candidate(temp_cloudflared, "download")'
    cloud_atomic_install = "os.replace(temp_cloudflared, canonical_cloudflared)"
    check("cloudflared checksum is verified before execution and installation",
          cloud_hash_guard in notebook_code
          and cloud_version_probe in notebook_code
          and notebook_code.index(cloud_hash_guard) < notebook_code.index(cloud_version_probe)
          and cloud_download_verify in notebook_code
          and notebook_code.index(cloud_download_verify) < notebook_code.index(cloud_atomic_install)
          and 'SESSION["cloudflared_sha256_verified"] = sha == CLOUDFLARED_EXPECTED_SHA256' in notebook_code
          and 'SESSION.get("cloudflared_sha256_verified") is True' in notebook_code)
    check("notebook interaction transcript metadata contract",
          notebook_meta.get("interaction_evidence_contract")
          == "full_sanitized_request_response+raw_sse+secret_redaction"
          and notebook_meta.get("interaction_evidence_artifact")
          == "/kaggle/working/muse-glimmer-30b-v1.0.0-interaction-evidence.json")
    check("notebook nonstream exposes full sanitized request response",
          "=== LOCAL NON-STREAM REQUEST (SANITIZED) ===" in notebook_code
          and "--- raw response body ---" in notebook_code
          and "--- parsed response JSON ---" in notebook_code
          and "Bearer <REDACTED>" in notebook_code
          and "reasoning emitted by backend (non-stream)" in notebook_code)
    check("notebook SSE exposes full raw protocol transcript",
          "=== RAW SSE PROTOCOL TRANSCRIPT ===" in notebook_code
          and "sse_raw_lines.append(line)" in notebook_code
          and "print(line)" in notebook_code
          and 'line == "data: [DONE]"' in notebook_code
          and '"parsed_events": sse_parsed_events' in notebook_code
          and "reconstructed reasoning emitted by backend (SSE)" in notebook_code)
    check("notebook interaction evidence is a required secret-safe core gate",
          "interaction_evidence_gate = \"FAIL\"" in notebook_code
          and 'SESSION["interaction_evidence_gate"] = interaction_evidence_gate' in notebook_code
          and 'SESSION.get("interaction_evidence_gate") == "PASS"' in notebook_code
          and "assert token not in interaction_text" in notebook_code)


    bootstrap_venv = ROOT.joinpath("scripts/bootstrap_venv.sh").read_text(encoding="utf-8")
    check("venv fallback marker uses visible stderr transport",
          "echo 'VENV_ENSUREPIP_FALLBACK=USED' >&2" in bootstrap_venv)
    check("notebook enforces venv log polish evidence gate",
      notebook_meta.get("venv_log_polish_contract")
      == "warning_marker_parity+raw_ensurepip_error_suppression"
      and 'VENV_FALLBACK_MARKER = "VENV_ENSUREPIP_FALLBACK=USED"' in notebook_code
      and "venv_fallback_warning_seen == venv_fallback_marker_seen" in notebook_code
      and 'SESSION["venv_log_polish_gate"] = "PASS"' in notebook_code
      and "VENV_RAW_ENSUREPIP_ERROR_SUPPRESSED=PASS" in notebook_code
      and "VENV_LOG_POLISH_GATE=PASS" in notebook_code
      and "FINAL_VENV_LOG_POLISH_GATE" in notebook_code)

    venv_transport = exercise_venv_fallback_log_transport()
    check("venv fallback marker survives stdout command substitution",
          venv_transport["success_rc"] == 0
          and "VENV_ENSUREPIP_FALLBACK=USED" in venv_transport["success_stderr"]
          and "VENV_ENSUREPIP_FALLBACK=USED" not in venv_transport["success_stdout"]
          and "using ensurepip-less fallback" in venv_transport["success_stderr"])
    check("successful venv fallback suppresses raw ensurepip error",
          venv_transport["raw_normal"] not in venv_transport["success_combined"])
    check("double venv failure preserves both diagnostics and fails closed",
          venv_transport["failure_rc"] != 0
          and venv_transport["raw_normal"] in venv_transport["failure_combined"]
          and venv_transport["raw_fallback"] in venv_transport["failure_combined"])

    # BEGIN canonical DFlash2 dedicated q4-k-m input contract
    canonical_dflash_url = "https://www.kaggle.com/models/dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf/Gguf/q4-k-m/1"
    legacy_dflash_url = "https://www.kaggle.com/models/dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf/Gguf/default/1"
    kaggle_en = ROOT.joinpath("docs/KAGGLE_PRODUCTION.md").read_text(encoding="utf-8")
    kaggle_vi = ROOT.joinpath("docs/KAGGLE_PRODUCTION.vi.md").read_text(encoding="utf-8")
    notebook_markdown = "\n".join(
        "".join(cell.get("source", "")) if isinstance(cell.get("source", ""), list) else cell.get("source", "")
        for cell in notebook.get("cells", [])
        if cell.get("cell_type") == "markdown"
    )

    check("bilingual README uses dedicated DFlash2 q4-k-m variation",
          all(canonical_dflash_url in text for text in (readme_en, readme_vi))
          and all("GGUF / `q4-k-m` / `1`" in text for text in (readme_en, readme_vi))
          and all(legacy_dflash_url not in text for text in (readme_en, readme_vi)))
    check("bilingual Kaggle operator docs use dedicated DFlash2 q4-k-m variation",
          all(canonical_dflash_url in text for text in (kaggle_en, kaggle_vi))
          and all("GGUF / `q4-k-m` / version `1`" in text for text in (kaggle_en, kaggle_vi))
          and all(legacy_dflash_url not in text for text in (kaggle_en, kaggle_vi)))
    check("notebook documents dedicated DFlash2 q4-k-m input",
          notebook_meta.get("dflash2_kaggle_variation") == "q4-k-m"
          and canonical_dflash_url in notebook_markdown
          and legacy_dflash_url not in notebook_markdown
          and "legacy all-in-one" in notebook_markdown)
    check("notebook input gate rejects legacy DFlash2 default bundle",
          'DRAFT_VARIATION = "q4-k-m"' in notebook_code
          and "is_canonical_variation_path" in notebook_code
          and 'SESSION["draft_variation_gate"] = "PASS"' in notebook_code
          and "Detach the legacy `default` all-in-one variation" in notebook_code)
    # END canonical DFlash2 dedicated q4-k-m input contract

    license_text = ROOT.joinpath("LICENSE").read_text(encoding="utf-8")
    check("MIT license header", "MIT License" in license_text)
    check("MIT copyright", "Copyright (c) 2026 Đăng Khoa" in license_text)

    for name in ("README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
                 "CODE_OF_CONDUCT.md", "SUPPORT.md"):
        check(f"required doc present {name}", ROOT.joinpath(name).is_file())
    for en in ("README", "CHANGELOG", "SECURITY", "CONTRIBUTING",
               "CODE_OF_CONDUCT", "SUPPORT"):
        check(f"required doc present {en}.vi.md", ROOT.joinpath(f"{en}.vi.md").is_file())

    sys.path.insert(0, str(ROOT / "scripts"))
    import demo_policy as P  # noqa: E402

    check("gateway sequential inference with bounded queue", P.MAX_ACTIVE_INFERENCE == 1 and P.MAX_QUEUED_INFERENCE > 0)
    check("gateway bounded agent queue", P.MAX_QUEUED_INFERENCE == 8 and P.QUEUE_WAIT_SECONDS == 600.0)
    check("gateway busy retry-after", P.BUSY_RETRY_AFTER == "5")
    check("gateway error codes", P.ERROR_BUSY == "muse_demo_busy"
          and P.ERROR_AUTH_REQUIRED == "muse_auth_required"
          and P.ERROR_RATE_LIMITED == "muse_rate_limited")
    check("gateway agent output ceiling is full model context", P.MAX_TOKENS == 131072 and P.DEFAULT_MAX_TOKENS == 131072)
    check("gateway accepts tool history and developer messages",
          P.validate_payload({"messages":[
              {"role":"assistant","content":None,"tool_calls":[
                  {"id":"call_1","type":"function","function":{"name":"read_file","arguments":"{}"}}
              ]},
              {"role":"tool","tool_call_id":"call_1","content":"ok"},
              {"role":"developer","content":"Be careful"}
          ]})[0])
    check("gateway full agent serving uses pinned HF sources and context",
          "MODEL_SOURCE_MODE=hf" in cfg
          and "CONTEXT_SIZE=131072" in cfg
          and "MODEL_HF_REVISION=16f5840bef28e4018cf41a7ec9709d6fd89176f6" in cfg
          and "HF_XET_NUM_CONCURRENT_RANGE_GETS=32" in cfg
          and "DFLASH_HF_REVISION=43c7eadd41352a299ea8e0a36b3157978dd63596" in cfg)
    check("gateway /ready public route allowed", P.ROUTE_ALLOW is None)

    import auth_proxy  # noqa: E402
    check("gateway server version", getattr(auth_proxy.AuthProxyHandler, "server_version", None) == "MuseExternalProxy/1.0.0")

    cfg = ROOT.joinpath("config/default.env").read_text(encoding="utf-8")
    ports = {}
    for line in cfg.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in ("SERVER_PORT", "SERVER_HOST", "SERVER_ALLOW_NONLOOPBACK", "PARALLEL_SLOTS", "GPU_REQUIRED_COUNT", "GPU_MIN_VRAM_MIB"):
            ports[k] = v
    check("backend loopback port 8088", ports.get("SERVER_PORT") == "8088")
    check("backend loopback host", ports.get("SERVER_HOST") == "127.0.0.1")
    check("backend loopback only", ports.get("SERVER_ALLOW_NONLOOPBACK") == "0")
    check("single parallel slot", ports.get("PARALLEL_SLOTS") == "1")
    check("gpu count 2 / vram 14000",
          ports.get("GPU_REQUIRED_COUNT") == "2" and ports.get("GPU_MIN_VRAM_MIB") == "14000")

    source_manifest = ROOT.joinpath("scripts/source_manifest.py").read_text(encoding="utf-8")
    check("manifest covers tracked root entrypoints",
          all(f"'{n}'" in source_manifest for n in
              ("serve.sh", "expose.sh", "external.sh", "readiness.sh", "doctor.sh",
               "release-export.sh", "demo-smoke.sh", "package-llama-runtime.sh", "VERSION")))
    check("manifest excludes benchmark tooling",
          "run.sh" not in source_manifest and "compare-models.sh" not in source_manifest)

    external_sh = ROOT.joinpath("external.sh").read_text(encoding="utf-8")
    usage = re.search(r"Usage:\s*\./external\.sh \{([^}]+)\}", external_sh)
    known_commands = set()
    if usage:
        known_commands.update(c.strip() for c in usage.group(1).split("|"))
    for m in re.finditer(
        r"^\s*(preflight|start|status|endpoint|local-endpoint|stop|stop-all)\s*\)",
        external_sh, re.M):
        known_commands.add(m.group(1))

    docs = ""
    for name in ("README.md", "README.vi.md", "CHANGELOG.md", "CHANGELOG.vi.md"):
        docs += ROOT.joinpath(name).read_text(encoding="utf-8") + "\n"
    documented = set(re.findall(r"\./external\.sh\s+([a-z][a-z-]*)", docs))
    check("release docs reference only real external.sh commands",
          documented <= known_commands, f"documented={sorted(documented - known_commands)}")
    check("release docs never use invalid ./external.sh expose",
          "external.sh expose" not in docs)
    check("external.sh usage lists the public operator commands",
          {"preflight", "start", "status", "endpoint", "local-endpoint", "stop", "stop-all"}
          <= known_commands)

    print(f"\npassed={TOTAL - len(FAILURES)} failed={len(FAILURES)}")
    return 1 if FAILURES else 0


class PublicContractTests(unittest.TestCase):
    def test_public_contract(self):
        self.assertEqual(main(), 0)


if __name__ == "__main__":
    sys.exit(main())