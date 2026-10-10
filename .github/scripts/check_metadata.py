#!/usr/bin/env python3
"""GitHub metadata and CI contract validation.

Checks the mandatory .github/ files, the CI action major versions
(actions/checkout@v7, actions/setup-python@v7), CODEOWNERS owner, README badge
slug and placeholder tokens, the exact MIT copyright line, the bilingual PR
template pair, and that every Dependabot ecosystem has a recognized manifest in
its configured directory.

Expected:
    GITHUB_METADATA=PASS
    LICENSE_AUTHOR_EXACT=PASS
    PR_TEMPLATE_BILINGUAL=PASS
    DEPENDABOT_CONFIG=PASS
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAILURES: list[str] = []

MANDATORY = [
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/ci.yml",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/PULL_REQUEST_TEMPLATE.vi.md",
    ".github/dependabot.yml",
]

LICENSE_COPYRIGHT = "Copyright (c) 2026 Đăng Khoa"
CODEOWNER = "@chloerbeach2026"

ECOSYSTEM_MANIFESTS: dict[str, tuple[str, ...]] = {
    "pip": ("requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "Pipfile"),
    "github-actions": (".github/workflows",),
    "gomod": ("go.mod",),
    "npm": ("package.json",),
    "docker": ("Dockerfile",),
    "terraform": (".terraform.lock.hcl",),
}


def main() -> int:
    for rel in MANDATORY:
        if not (ROOT / rel).is_file():
            FAILURES.append(f"missing {rel}")

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if "actions/checkout@v7" not in ci:
        FAILURES.append("ci.yml does not use actions/checkout@v7")
    if "actions/setup-python@v7" not in ci:
        FAILURES.append("ci.yml does not use actions/setup-python@v7")

    codeowners = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    if CODEOWNER not in codeowners:
        FAILURES.append(f"CODEOWNERS does not contain {CODEOWNER}")

    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    dep_entries = re.findall(
        r"package-ecosystem:\s*\"?([a-z-]+)\"?\s*\n\s*directory:\s*\"?([^\"\n]+)\"?", dependabot
    )
    if not dep_entries:
        FAILURES.append("dependabot.yml has no package-ecosystem entries")
    for eco, directory in dep_entries:
        if eco == "pip":
            FAILURES.append("dependabot pip ecosystem is not supported for v1.0.0 (no recognized pip manifest in config/)")
            continue
        if eco not in ECOSYSTEM_MANIFESTS:
            FAILURES.append(f"unexpected dependabot ecosystem: {eco}")
            continue
        base = ROOT / directory.lstrip("/")
        manifest_hits = [m for m in ECOSYSTEM_MANIFESTS[eco] if (base / m).exists()]
        if not manifest_hits:
            FAILURES.append(
                f"dependabot ecosystem {eco} ({directory}) has no recognized manifest in {directory}"
            )

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    if "MIT License" not in license_text:
        FAILURES.append("LICENSE is not the MIT license")
    if LICENSE_COPYRIGHT not in license_text:
        FAILURES.append(f"LICENSE does not contain the exact line {LICENSE_COPYRIGHT!r}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for placeholder in ("YOUR_USER", "YOUR_REPO", "TODO", "TBD", "example/repo"):
        if placeholder in readme:
            FAILURES.append(f"README contains placeholder {placeholder!r}")

    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"], check=True, capture_output=True
        ).stdout.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        remote = ""
    if remote:
        m = re.match(r"https?://(?:[^@/]+@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote)
        if not m:
            FAILURES.append(f"cannot parse origin url: {remote}")
        else:
            owner, repo = m.group(1), m.group(2)
            if f"{owner}/{repo}" not in readme:
                FAILURES.append(f"README badge slug does not match origin {owner}/{repo}")

    print()
    print(f"LICENSE_AUTHOR_EXACT={'PASS' if LICENSE_COPYRIGHT in license_text else 'FAIL'}")
    print(f"PR_TEMPLATE_BILINGUAL={'PASS' if (ROOT / '.github/PULL_REQUEST_TEMPLATE.md').is_file() and (ROOT / '.github/PULL_REQUEST_TEMPLATE.vi.md').is_file() else 'FAIL'}")
    print(f"DEPENDABOT_CONFIG={'FAIL' if any('dependabot' in f for f in FAILURES) else 'PASS'}")
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL  {f}")
        print("GITHUB_METADATA=FAIL")
        return 1
    print("GITHUB_METADATA=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())