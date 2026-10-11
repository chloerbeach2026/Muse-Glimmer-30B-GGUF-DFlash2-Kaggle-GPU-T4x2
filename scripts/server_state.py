#!/usr/bin/env python3
"""Persistent server state with PID-reuse-safe ownership checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "stale", "state_error": f"{type(exc).__name__}: {exc}"}
    return data if isinstance(data, dict) else {"status": "stale", "state_error": "state root is not an object"}


def proc_identity(pid: int, command_token: str = "") -> tuple[str, str] | None:
    stat_path = Path(f"/proc/{pid}/stat")
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        parts = stat_path.read_text(encoding="utf-8").split()
        if len(parts) < 22 or parts[2] == "Z":
            return None
        cmdline = cmdline_path.read_bytes()
    except OSError:
        return None
    if not cmdline:
        return None
    if command_token and command_token.encode() not in cmdline:
        return None
    return parts[21], hashlib.sha256(cmdline).hexdigest()


def capture_identity(pid: int, command_token: str, timeout: float) -> tuple[str, str] | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        identity = proc_identity(pid, command_token)
        if identity is not None:
            return identity
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def state_owned(data: dict[str, Any]) -> bool:
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = proc_identity(pid)
    if current is None:
        return False
    return str(data.get("pid_start_ticks", "")) == current[0] and str(data.get("command_sha256", "")) == current[1]


def probe(urls: list[str], timeout: float = 0.5) -> bool:
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if 200 <= response.status < 400:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    return False


def derived_urls(data: dict[str, Any]) -> list[str]:
    host = data.get("host")
    port = data.get("port")
    if not host or not port:
        return []
    return [f"http://{host}:{port}/health", f"http://{host}:{port}/v1/models"]


def status_report(path: Path, urls: list[str]) -> dict[str, Any]:
    data = load_state(path)
    if not data:
        return {"status": "stopped", "owned": False, "healthy": False, "state_file": str(path)}
    stored_status = str(data.get("status") or "stale")
    if stored_status == "stopped" or not data.get("pid"):
        out = dict(data)
        out.update(status="stopped", owned=False, healthy=False, state_file=str(path))
        return out
    owned = state_owned(data)
    if not owned:
        out = dict(data)
        out.update(status="stale", owned=False, healthy=False, state_file=str(path))
        return out
    effective_urls = urls or derived_urls(data)
    healthy = probe(effective_urls) if effective_urls else False
    if stored_status == "ready":
        effective_status = "ready" if healthy else "degraded"
    elif stored_status in {"starting", "stopping"}:
        effective_status = stored_status
    else:
        effective_status = "degraded" if not healthy else "ready"
    out = dict(data)
    out.update(status=effective_status, owned=True, healthy=healthy, state_file=str(path))
    context_value = out.get("context_size")
    requested_output = out.get("max_tokens")
    if isinstance(context_value, int) and context_value > 0:
        out["effective_context_size"] = context_value
        if isinstance(requested_output, int) and requested_output > 0:
            out["effective_max_output_tokens"] = min(context_value, requested_output, 131072)
    return out


def print_report(report: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    keys = [
        ("STATUS", "status"), ("PID", "pid"), ("PROFILE", "profile"),
        ("HOST", "host"), ("PORT", "port"), ("OWNED", "owned"),
        ("HEALTHY", "healthy"), ("TARGET", "target_path"),
        ("TARGET_SHA256", "target_sha256"), ("DRAFT", "draft_path"),
        ("DRAFT_SHA256", "draft_sha256"), ("CONTEXT_SIZE", "context_size"),
        ("EFFECTIVE_CONTEXT_SIZE", "effective_context_size"),
        ("REQUESTED_MAX_TOKENS", "max_tokens"), ("MAX_TOKENS", "effective_max_output_tokens"),
        ("MM_PROJ_SHA256", "mmproj_sha256"),
        ("RUNTIME_SOURCE", "runtime_source"),
        ("RUNTIME_COMMIT", "runtime_commit"), ("CUDA_VISIBLE_DEVICES", "cuda_visible_devices"),
        ("GPU_COUNT", "gpu_count"), ("LOG", "log"), ("STATE_FILE", "state_file"),
    ]
    for label, key in keys:
        value = report.get(key)
        if isinstance(value, bool):
            value = "1" if value else "0"
        print(f"{label}={'' if value is None else value}")


def begin(args: argparse.Namespace) -> int:
    identity = capture_identity(args.pid, args.command_token, args.command_timeout)
    if identity is None:
        suffix = f" containing command token {args.command_token!r}" if args.command_token else ""
        print(f"ERROR: cannot capture live server identity for PID {args.pid}{suffix}", file=sys.stderr)
        return 10
    metadata: dict[str, Any] = {}
    if args.metadata_json:
        try:
            candidate = json.loads(Path(args.metadata_json).read_text(encoding="utf-8"))
            if not isinstance(candidate, dict):
                raise ValueError("metadata JSON root must be an object")
            metadata.update(candidate)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: invalid server metadata: {exc}", file=sys.stderr)
            return 2
    now = iso_now()
    payload = dict(metadata)
    payload.update(
        schema_version=1,
        status="starting",
        pid=args.pid,
        pid_start_ticks=identity[0],
        command_sha256=identity[1],
        profile=args.profile,
        host=args.host,
        port=args.port,
        log=args.log,
        started_at=now,
        ready_at=None,
        stopping_at=None,
        stopped_at=None,
    )
    atomic_write(Path(args.state), payload)
    return 0


def mark_ready(args: argparse.Namespace) -> int:
    path = Path(args.state)
    data = load_state(path)
    if not data or not state_owned(data):
        print("ERROR: cannot mark READY: stored PID is not the owned live server", file=sys.stderr)
        return 10
    data.update(status="ready", ready_at=iso_now())
    atomic_write(path, data)
    return 0


def mark_stopping(args: argparse.Namespace) -> int:
    path = Path(args.state)
    data = load_state(path)
    if not data or not state_owned(data):
        print("ERROR: cannot mark STOPPING: stored PID is not the owned live server", file=sys.stderr)
        return 10
    data.update(status="stopping", stopping_at=iso_now())
    atomic_write(path, data)
    return 0


def mark_stopped(args: argparse.Namespace) -> int:
    path = Path(args.state)
    data = load_state(path)
    previous_pid = data.get("pid") if data else None
    if not data:
        data = {"schema_version": 1}
    data.update(
        status="stopped",
        previous_pid=previous_pid,
        pid=None,
        pid_start_ticks=None,
        command_sha256=None,
        healthy=False,
        stop_reason=args.reason or None,
        stopped_at=iso_now(),
    )
    atomic_write(path, data)
    return 0


def owned_pid(args: argparse.Namespace) -> int:
    data = load_state(Path(args.state))
    if data and state_owned(data):
        print(data["pid"])
        return 0
    print("ERROR: state does not identify the currently running owned server", file=sys.stderr)
    return 10


def clear_stale(args: argparse.Namespace) -> int:
    path = Path(args.state)
    if not path.exists():
        return 0
    report = status_report(path, [])
    if report.get("owned"):
        print("ERROR: refusing to clear state for a live owned server", file=sys.stderr)
        return 12
    if report.get("status") == "stale":
        path.unlink(missing_ok=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Persistent Muse-Glimmer server state helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin")
    b.add_argument("--state", required=True)
    b.add_argument("--pid", required=True, type=int)
    b.add_argument("--profile", required=True)
    b.add_argument("--host", required=True)
    b.add_argument("--port", required=True, type=int)
    b.add_argument("--log", required=True)
    b.add_argument("--metadata-json")
    b.add_argument("--command-token", default="")
    b.add_argument("--command-timeout", type=float, default=2.0)
    b.set_defaults(func=begin)

    r = sub.add_parser("mark-ready")
    r.add_argument("--state", required=True)
    r.set_defaults(func=mark_ready)

    sp = sub.add_parser("mark-stopping")
    sp.add_argument("--state", required=True)
    sp.set_defaults(func=mark_stopping)

    sd = sub.add_parser("mark-stopped")
    sd.add_argument("--state", required=True)
    sd.add_argument("--reason", default="")
    sd.set_defaults(func=mark_stopped)

    st = sub.add_parser("status")
    st.add_argument("--state", required=True)
    st.add_argument("--url", action="append", default=[])
    st.add_argument("--format", choices=["json", "env"], default="env")

    op = sub.add_parser("owned-pid")
    op.add_argument("--state", required=True)
    op.set_defaults(func=owned_pid)

    cs = sub.add_parser("clear-stale")
    cs.add_argument("--state", required=True)
    cs.set_defaults(func=clear_stale)

    args = ap.parse_args()
    if args.cmd == "status":
        print_report(status_report(Path(args.state), args.url), args.format)
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
