#!/usr/bin/env python3
"""Fail-closed documentation checks that stay useful without third-party packages.

Checks repository Markdown/template files (tracked plus unignored new files) for broken relative inline links and verifies that the
runnable example's schemas are byte-identical to the canonical PROJECT schemas. Generated
bootstrap parity remains the responsibility of build_bootstrap.py --check.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
SCHEMES = {"http", "https", "mailto", "tel", "data"}


def repository_docs() -> list[Path]:
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot enumerate repository documentation: {exc}") from exc
    paths = []
    for item in raw.decode("utf-8").split("\0"):
        if item and Path(item).suffix.lower() in {".md", ".template"}:
            paths.append(ROOT / item)
    return paths


def local_target(source: Path, raw: str) -> Path | None:
    target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
    target = unquote(target).replace("\\", "/")
    parsed = urlsplit(target)
    if parsed.scheme.lower() in SCHEMES or target.startswith(('#', '//')):
        return None
    path = parsed.path
    if not path or "{{" in path or "<" in path or "*" in path:
        return None
    return (source.parent / path).resolve()


def check_links(paths: list[Path]) -> list[str]:
    errors = []
    for source in paths:
        text = source.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            raw = match.group("target")
            target = local_target(source, raw)
            if target is not None and not target.exists():
                rel = source.relative_to(ROOT).as_posix()
                errors.append(f"{rel}: broken local link {raw!r}")
    return errors


def check_schema_mirror() -> list[str]:
    errors = []
    canonical = ROOT / "PROJECT" / "schema"
    example = ROOT / "example" / "PROJECT" / "schema"
    names = sorted(p.name for p in canonical.glob("*.schema.json"))
    example_names = sorted(p.name for p in example.glob("*.schema.json"))
    if names != example_names:
        return [f"schema mirror file set differs: PROJECT={names!r} example={example_names!r}"]
    for name in names:
        if (canonical / name).read_bytes() != (example / name).read_bytes():
            errors.append(f"example/PROJECT/schema/{name} differs from PROJECT/schema/{name}")
    return errors


def main() -> int:
    try:
        paths = repository_docs()
        errors = check_links(paths) + check_schema_mirror()
    except Exception as exc:
        print(f"DOCS: FAIL — internal error: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"DOCS: {error}", file=sys.stderr)
        print(f"DOCS: FAIL — {len(errors)} problem(s)", file=sys.stderr)
        return 2
    print(f"DOCS: PASS — {len(paths)} repository docs checked; schema mirror exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
