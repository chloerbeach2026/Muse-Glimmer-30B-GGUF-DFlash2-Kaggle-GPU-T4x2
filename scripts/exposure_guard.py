#!/usr/bin/env python3
"""Fail-closed validation for the proven persistent server status."""
from __future__ import annotations

import argparse
import sys
import urllib.parse

EXPECTED_TARGET = "0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384"
EXPECTED_DRAFT = "b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef"
EXPECTED_COMMIT = "64f765f5adefa4620dddda436ce56f1430435536"


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key.replace("_", "").isalnum():
            out[key] = value
    return out


def validate_backend(data: dict[str, str]) -> list[str]:
    errors: list[str] = []
    required = {
        "STATUS": "ready", "OWNED": "1", "HEALTHY": "1", "PROVENANCE": "PASS",
        "HOST": "127.0.0.1", "TARGET_SHA256": EXPECTED_TARGET, "RUNTIME_COMMIT": EXPECTED_COMMIT,
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            errors.append(f"{key} must be {expected!r}")
    profile = data.get("PROFILE", "")
    if profile not in {"dflash2", "baseline"}:
        errors.append("PROFILE must be dflash2 or baseline")
    if profile == "dflash2" and data.get("DRAFT_SHA256") != EXPECTED_DRAFT:
        errors.append("DRAFT_SHA256 is not canonical DFlash2")
    try:
        port = int(data.get("PORT", ""))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        errors.append("PORT must be an integer in 1..65535")
    return errors


def cmd_validate_backend() -> int:
    data = parse_env(sys.stdin.read())
    errors = validate_backend(data)
    if errors:
        for error in errors:
            print(f"ERROR: backend gate: {error}", file=sys.stderr)
        return 30
    port = int(data["PORT"])
    print("BACKEND_GATE=PASS")
    print(f"PROFILE={data['PROFILE']}")
    print("HOST=127.0.0.1")
    print(f"PORT={port}")
    print(f"BACKEND_URL=http://127.0.0.1:{port}")
    return 0


def validate_waitable_starting_backend(data: dict[str, str]) -> list[str]:
    errors: list[str] = []
    required = {
        "STATUS": "starting", "OWNED": "1", "PROVENANCE": "PASS",
        "HOST": "127.0.0.1", "TARGET_SHA256": EXPECTED_TARGET, "RUNTIME_COMMIT": EXPECTED_COMMIT,
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            errors.append(f"{key} must be {expected!r}")
    if data.get("HEALTHY") not in {"0", "1"}:
        errors.append("HEALTHY must be '0' or '1' while starting")
    profile = data.get("PROFILE", "")
    if profile not in {"dflash2", "baseline"}:
        errors.append("PROFILE must be dflash2 or baseline")
    if profile == "dflash2" and data.get("DRAFT_SHA256") != EXPECTED_DRAFT:
        errors.append("DRAFT_SHA256 is not canonical DFlash2")
    try:
        port = int(data.get("PORT", ""))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        errors.append("PORT must be an integer in 1..65535")
    return errors


def cmd_validate_backend_starting() -> int:
    data = parse_env(sys.stdin.read())
    errors = validate_waitable_starting_backend(data)
    if errors:
        for error in errors:
            print(f"ERROR: backend starting gate: {error}", file=sys.stderr)
        return 30
    print("BACKEND_STARTING_GATE=PASS")
    return 0


def validate_public_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public URL is malformed") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("public URL must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public URL must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("public URL must not contain path, query, or fragment")
    if port not in {None, 443}:
        raise ValueError("public URL must use the default HTTPS port")
    host = parsed.hostname
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise ValueError("public URL hostname is invalid") from exc
    if any(ch.isspace() for ch in host):
        raise ValueError("public URL hostname is invalid")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}"


def cmd_validate_public_url() -> int:
    raw = sys.stdin.read()
    try:
        url = validate_public_url(raw)
    except ValueError as exc:
        print(f"ERROR: public URL gate: {exc}", file=sys.stderr)
        return 31
    print(f"PUBLIC_URL={url}")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-backend")
    sub.add_parser("validate-backend-starting")
    sub.add_parser("validate-public-url")
    args = ap.parse_args()
    if args.cmd == "validate-backend":
        return cmd_validate_backend()
    if args.cmd == "validate-backend-starting":
        return cmd_validate_backend_starting()
    if args.cmd == "validate-public-url":
        return cmd_validate_public_url()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
