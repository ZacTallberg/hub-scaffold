#!/usr/bin/env python3
"""Focused contract proof for the canonical accessible realtime cockpit."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = {name: (ROOT / name).read_text(encoding="utf-8") for name in (
        "hub_core/frontend/hub.js", "hub_core/frontend/hub_shell.html",
        "hub_core/frontend/palette.js", "hub_core/frontend/print.css",
        "hub_core/frontend/shell.css", "hub_core/frontend/theme.js", "hub_core/shell.py")}
    required = {
        "hub_core/frontend/hub.js": ("If-None-Match", "AbortController", "aria-controls",
            "aria-sort", "ArrowRight", "data-label", "Board stale", "publishClientState",
            "deliveryCard", "status === \"working\""),
        "hub_core/frontend/hub_shell.html": ("{{theme_js}}", "hidden title=\"Launch one",
            "role=\"tablist\"", "aria-describedby=\"modalSubtitle\""),
        "hub_core/frontend/palette.js": ("function refresh(nextData)", "shell.inert = true"),
        "hub_core/frontend/print.css": ("@media print", ".tab-content", ".data-table"),
        "hub_core/frontend/shell.css": (".activity-feed", ".sort-btn", ".delivery-flow"),
        "hub_core/frontend/theme.js": ("prefers-color-scheme", "hub:themechange", "storage"),
        "hub_core/shell.py": ('"theme_js"',),
    }
    errors = [f"{name}: missing {needle}" for name, needles in required.items()
              for needle in needles if needle not in files[name]]
    for name in ("hub.js", "palette.js", "theme.js"):
        run = subprocess.run(["node", "--check", str(ROOT / "hub_core/frontend" / name)],
                             capture_output=True, text=True)
        if run.returncode:
            errors.append(f"{name}: JavaScript syntax failed: {run.stderr.strip()}")
    if errors:
        print("Hub visual contract: FAIL", file=sys.stderr)
        print("\n".join("- " + error for error in errors), file=sys.stderr)
        return 1
    print("Hub visual contract: PASS (theme, responsive table, WAI tabs/sort, print, palette, ETag and freshness seams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
