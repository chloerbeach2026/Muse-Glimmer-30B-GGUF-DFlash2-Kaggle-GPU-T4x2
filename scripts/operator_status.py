#!/usr/bin/env python3
"""Muse-Glimmer operator-facing machine-readable status.

This is a read-only adapter above the existing lifecycle authorities. It does not
own processes, mutate state, or inspect secret-bearing environment variables.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
FROZEN_TARGET_SHA256 = "0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384"
FROZEN_DRAFT_SHA256 = "b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef"
FROZEN_RUNTIME_COMMIT = "64f765f5adefa4620dddda436ce56f1430435536"


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            out[key] = value.strip()
    return out


def as_bool(value: str) -> bool:
    return value == "1"


def is_loopback_http_origin(value: str) -> bool:
    if not value:
        return False
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        return False
    if u.scheme != "http" or u.hostname != "127.0.0.1" or not port:
        return False
    return not (u.username or u.password or u.path not in ("", "/") or u.query or u.fragment)


def is_clean_https_origin(value: str) -> bool:
    if not value:
        return False
    try:
        u = urlsplit(value)
        _ = u.port  # force validation of a present port value
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


def classify(external: dict[str, str], backend: dict[str, str]) -> tuple[str, list[str]]:
    diag: list[str] = []
    ex = external.get("EXTERNAL_STATUS", "")
    bs = backend.get("STATUS") or external.get("BACKEND_STATUS", "")
    exposure = external.get("EXPOSURE_STATUS", "")
    proxy = as_bool(external.get("PROXY_OWNED", "0"))
    tunnel = as_bool(external.get("TUNNEL_OWNED", "0"))
    backend_owned = as_bool(backend.get("OWNED", external.get("BACKEND_OWNED", "0")))
    backend_healthy = as_bool(backend.get("HEALTHY", external.get("BACKEND_HEALTHY", "0")))
    prov = backend.get("PROVENANCE", external.get("BACKEND_PROVENANCE", ""))
    local = external.get("LOCAL_PROXY_URL", "")
    public = external.get("PUBLIC_URL", "")

    if bs == "stale":
        diag.append("backend-stale")
    provenance_not_applicable = prov == "NA" and bs == "stopped" and not backend_owned
    if prov and prov != "PASS" and not provenance_not_applicable:
        diag.append("backend-provenance-failed")
    if local and not is_loopback_http_origin(local):
        diag.append("unsafe-local-proxy-url")
    if public and not is_clean_https_origin(public):
        diag.append("unsafe-public-url")
    if external.get("MODE") == "proxy" and public:
        diag.append("proxy-mode-invented-public-url")
    if external.get("MODE") == "proxy" and tunnel:
        diag.append("proxy-mode-invented-tunnel-ownership")

    if diag or ex == "degraded" or bs == "stale":
        return "degraded", diag
    if ex == "stopped" and bs == "stopped" and not backend_owned and not proxy and not tunnel:
        return "stopped", diag
    if ex == "backend-only" and bs == "ready" and backend_owned and backend_healthy and prov == "PASS" and not proxy and not tunnel:
        return "backend-only", diag
    if ex == "proxy-ready" and exposure == "proxy-ready" and bs == "ready" and backend_owned and backend_healthy and prov == "PASS" and proxy and not tunnel and not public and is_loopback_http_origin(local):
        return "proxy-ready", diag
    if ex == "ready" and exposure == "ready" and bs == "ready" and backend_owned and backend_healthy and prov == "PASS" and proxy and tunnel and is_clean_https_origin(public):
        return "public-ready", diag
    if ex == "starting" or bs == "starting" or exposure == "starting":
        return "starting", diag
    diag.append("state-contract-mismatch")
    return "degraded", diag


def read_status_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def run_status(root: Path, command: str) -> str:
    script = root / command
    if not script.is_file():
        raise RuntimeError(f"missing lifecycle command: {script}")
    p = subprocess.run([str(script), "status"], cwd=root, text=True, capture_output=True)
    # status is diagnostic and may intentionally return non-zero in degraded cases;
    # preserve stdout and only use stderr if stdout is empty.
    text = p.stdout if p.stdout.strip() else p.stderr
    if not text.strip():
        raise RuntimeError(f"{command} status produced no machine-readable output (rc={p.returncode})")
    return text


def read_demo_telemetry(path: Path | None) -> dict:
    """Load the demo gateway telemetry snapshot if it exists.

    The snapshot is an operator-facing diagnostic (atomic jsonl). A missing or
    unparseable file is tolerated: the gateway may simply not be running yet.
    """
    if not path:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def build_document(external: dict[str, str], backend: dict[str, str], version: str = "", demo: dict | None = None) -> dict:
    state, diag = classify(external, backend)
    doc: dict = {
        "schema_version": SCHEMA_VERSION,
        "release_version": version,
        "operator_state": state,
        "mode": external.get("MODE", ""),
        "ownership": {
            "backend": as_bool(backend.get("OWNED", external.get("BACKEND_OWNED", "0"))),
            "proxy": as_bool(external.get("PROXY_OWNED", "0")),
            "tunnel": as_bool(external.get("TUNNEL_OWNED", "0")),
        },
        "health": {
            "backend": as_bool(backend.get("HEALTHY", external.get("BACKEND_HEALTHY", "0"))),
            "auth_enabled": as_bool(external.get("AUTH_ENABLED", "0")),
        },
        "backend": {
            "status": backend.get("STATUS", external.get("BACKEND_STATUS", "")),
            "pid": backend.get("PID", ""),
            "profile": backend.get("PROFILE", ""),
            "host": backend.get("HOST", ""),
            "port": backend.get("PORT", ""),
            "provenance": backend.get("PROVENANCE", external.get("BACKEND_PROVENANCE", "")),
            "target_sha256": backend.get("TARGET_SHA256", ""),
            "draft_sha256": backend.get("DRAFT_SHA256", ""),
            "runtime_commit": backend.get("RUNTIME_COMMIT", ""),
        },
        "ingress": {
            "exposure_status": external.get("EXPOSURE_STATUS", ""),
            "local_proxy_url": external.get("LOCAL_PROXY_URL", ""),
            "public_url": external.get("PUBLIC_URL", ""),
        },
        "frozen_identity_match": {
            "target": backend.get("TARGET_SHA256", "") == FROZEN_TARGET_SHA256,
            "draft": backend.get("DRAFT_SHA256", "") == FROZEN_DRAFT_SHA256,
            "runtime": backend.get("RUNTIME_COMMIT", "") == FROZEN_RUNTIME_COMMIT,
        },
        "diagnostics": diag,
    }
    if demo:
        gateway_health = "present"
        if not demo.get("schema_version"):
            gateway_health = "absent"
        doc["gateway"] = {
            "schema_version": demo.get("schema_version"),
            "health": gateway_health,
            "mode": demo.get("mode", ""),
            "active_inference": demo.get("active_inference"),
            "rate_bucket_tokens": demo.get("rate_bucket_tokens"),
            "requests_total": demo.get("requests_total"),
            "inference_admitted_total": demo.get("inference_admitted_total"),
            "busy_rejected_total": demo.get("busy_rejected_total"),
            "rate_limited_total": demo.get("rate_limited_total"),
            "timeout_total": demo.get("timeout_total"),
            "backend_error_total": demo.get("backend_error_total"),
            "last_http_status": demo.get("last_http_status"),
            "last_finish_class": demo.get("last_finish_class"),
            "last_request_id": demo.get("last_request_id"),
            "last_request_duration_ms": demo.get("last_request_duration_ms"),
            "last_ttft_ms": demo.get("last_ttft_ms"),
        }
        if gateway_health != "present":
            diag.append("gateway-absent")
    return doc


def render_env(doc: dict) -> str:
    flat = {
        "SCHEMA_VERSION": doc["schema_version"],
        "RELEASE_VERSION": doc["release_version"],
        "OPERATOR_STATE": doc["operator_state"],
        "MODE": doc["mode"],
        "BACKEND_OWNED": int(doc["ownership"]["backend"]),
        "PROXY_OWNED": int(doc["ownership"]["proxy"]),
        "TUNNEL_OWNED": int(doc["ownership"]["tunnel"]),
        "BACKEND_HEALTHY": int(doc["health"]["backend"]),
        "AUTH_ENABLED": int(doc["health"]["auth_enabled"]),
        "BACKEND_STATUS": doc["backend"]["status"],
        "BACKEND_PROVENANCE": doc["backend"]["provenance"],
        "TARGET_SHA256": doc["backend"]["target_sha256"],
        "DRAFT_SHA256": doc["backend"]["draft_sha256"],
        "RUNTIME_COMMIT": doc["backend"]["runtime_commit"],
        "LOCAL_PROXY_URL": doc["ingress"]["local_proxy_url"],
        "PUBLIC_URL": doc["ingress"]["public_url"],
        "DIAGNOSTICS": ",".join(doc["diagnostics"]),
    }
    gateway = doc.get("gateway")
    if gateway:
        flat["GATEWAY_HEALTH"] = gateway["health"].upper()
        flat["GATEWAY_MODE"] = gateway["mode"] or ""
        flat["GATEWAY_REQUESTS_TOTAL"] = str(gateway["requests_total"] or 0)
        flat["GATEWAY_ADMITTED_TOTAL"] = str(gateway["inference_admitted_total"] or 0)
        flat["GATEWAY_BUSY_TOTAL"] = str(gateway["busy_rejected_total"] or 0)
        flat["GATEWAY_RATE_LIMITED_TOTAL"] = str(gateway["rate_limited_total"] or 0)
        flat["GATEWAY_TIMEOUT_TOTAL"] = str(gateway["timeout_total"] or 0)
        flat["GATEWAY_BACKEND_ERROR_TOTAL"] = str(gateway["backend_error_total"] or 0)
        flat["GATEWAY_ACTIVE_INFERENCE"] = str(int(bool(gateway["active_inference"])))
        flat["GATEWAY_LAST_FINISH_CLASS"] = gateway["last_finish_class"] or ""
    return "".join(f"{k}={v}\n" for k, v in flat.items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only operator status adapter")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--external-status-file")
    ap.add_argument("--backend-status-file")
    ap.add_argument("--demo-telemetry-file")
    ap.add_argument("--format", choices=("json", "env"), default="json")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if bool(args.external_status_file) != bool(args.backend_status_file):
            raise RuntimeError("provide both --external-status-file and --backend-status-file, or neither")
        if args.external_status_file:
            external_text = read_status_file(Path(args.external_status_file))
            backend_text = read_status_file(Path(args.backend_status_file))
        else:
            external_text = run_status(root, "external.sh")
            backend_text = run_status(root, "serve.sh")
        version = ""
        vf = root / "VERSION"
        if vf.is_file():
            version = vf.read_text(encoding="utf-8", errors="replace").strip()
        demo = read_demo_telemetry(Path(args.demo_telemetry_file) if args.demo_telemetry_file else None)
        doc = build_document(parse_env(external_text), parse_env(backend_text), version, demo)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_env(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
