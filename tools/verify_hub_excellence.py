#!/usr/bin/env python3
"""Verify that the Hub Excellence contract is canonical and unavoidable."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = {
    "PROJECT/HUB-QUALITY.md": (
        "## 1. Product and visual excellence",
        "## 2. Required invariants",
        "## 3. Proof matrix",
        "## 4. Elevation workflow",
        "Last-Event-ID",
        "LCP <= 2.5 s",
        "unmeasured",
        "atomic leases/fencing",
        "https://www.w3.org/TR/WCAG22/",
        "https://kanbanguides.org/the-kanban-guide/",
    ),
    "hub_core/frontend/README.md": ("PROJECT/HUB-QUALITY.md", "320/768/1440", "zero-CDN"),
    "campaigns/elevate-hub.md": ("PROJECT/HUB-QUALITY.md", "Research", "Audit rendered reality", "Crystallize"),
    "AGENTS.md": ("PROJECT/HUB-QUALITY.md", "campaigns/elevate-hub.md"),
    "README.md": ("PROJECT/HUB-QUALITY.md", "verify_hub_excellence.py --contract"),
    "OPERATING-AGREEMENT.md": ("PROJECT/HUB-QUALITY.md",),
    "PROJECT/README.md": ("HUB-QUALITY.md",),
    "PROJECT/CHARTER.md": ("HUB-QUALITY.md",),
    "PROJECT/DOCTRINE.md": ("HUB-QUALITY.md",),
    "governance/AGENTS.md.template": ("PROJECT/HUB-QUALITY.md",),
    "governance/CLAUDE.md.template": ("PROJECT/HUB-QUALITY.md",),
    "adapters/django/MOUNTING.md": ("PROJECT/HUB-QUALITY.md",),
    "campaigns/README.md": ("elevate-hub.md",),
    "campaigns/augment-hub.md": ("elevate-hub.md",),
    "campaigns/feature-buildout.md": ("elevate-hub.md",),
    "docs/ARCHITECTURE.md": ("PROJECT/HUB-QUALITY.md",),
    "init.sh": ("PROJECT/HUB-QUALITY.md",),
    "PROJECT-PLANE-BOOTSTRAP.md": ("TPL:PROJECT/HUB-QUALITY.md",),
    "CHANGELOG.md": ("Hub Excellence Contract",),
}


def failures(files: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for name, needles in REQUIRED.items():
        body = files.get(name)
        if body is None:
            missing.append(f"missing file: {name}")
            continue
        for needle in needles:
            if needle not in body:
                missing.append(f"{name}: missing {needle!r}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", action="store_true", help="verify the canonical contract and propagation")
    args = parser.parse_args()
    if not args.contract:
        parser.error("choose --contract")

    files = {name: (ROOT / name).read_text(encoding="utf-8") for name in REQUIRED if (ROOT / name).is_file()}
    errors = failures(files)

    seeded = dict(files)
    seeded["PROJECT/HUB-QUALITY.md"] = seeded.get("PROJECT/HUB-QUALITY.md", "").replace("Last-Event-ID", "", 1)
    if not any("Last-Event-ID" in error for error in failures(seeded)):
        errors.append("seeded-negative self-check did not detect a removed invariant")

    bootstrap = subprocess.run(
        [sys.executable, str(ROOT / "tools/build_bootstrap.py"), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if bootstrap.returncode:
        errors.append("generated bootstrap is stale: " + (bootstrap.stdout + bootstrap.stderr).strip())

    if errors:
        print("Hub Excellence contract: FAIL", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Hub Excellence contract: PASS ({sum(len(v) for v in REQUIRED.values())} assertions; seeded removal detected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
