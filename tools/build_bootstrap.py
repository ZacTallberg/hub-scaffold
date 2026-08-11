#!/usr/bin/env python3
"""Insert the canonical PROJECT/ template files into PROJECT-PLANE-BOOTSTRAP.md.

The bootstrap spec embeds every template verbatim between marker pairs:
    <!-- TPL:PROJECT/path/file.ext -->
    ...generated fenced block...
    <!-- /TPL -->
Run with no args to (re)build the embeds in place. Run with --check to verify the
document matches a fresh build byte-for-byte (exit 1 on drift) — wire this wherever
templates or the spec can change. Fail-closed: a missing file or marker is an error.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "PROJECT-PLANE-BOOTSTRAP.md"
FENCE = "````"
LANG = {".md": "markdown", ".json": "json", ".sh": "bash", ".py": "python"}

BEGIN = re.compile(r"^<!-- TPL:(?P<path>[^ ]+) -->$")
END = "<!-- /TPL -->"


def build(text: str) -> str:
    out, i, lines, seen = [], 0, text.split("\n"), 0
    while i < len(lines):
        m = BEGIN.match(lines[i])
        out.append(lines[i])
        if m:
            seen += 1
            rel = m.group("path")
            src = ROOT / rel
            if not src.exists():
                sys.exit(f"ERROR: embedded template missing on disk: {rel}")
            body = src.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
            if FENCE in body:
                sys.exit(f"ERROR: {rel} contains a 4-backtick fence; embed scheme breaks")
            lang = LANG.get(src.suffix, "")
            out.append(f"{FENCE}{lang}")
            out.append(body)
            out.append(FENCE)
            # skip the old block up to and including END
            i += 1
            while i < len(lines) and lines[i].strip() != END:
                i += 1
            if i >= len(lines):
                sys.exit(f"ERROR: unterminated TPL block for {rel}")
            out.append(lines[i])
        i += 1
    if seen == 0:
        sys.exit("ERROR: no TPL markers found")
    return "\n".join(out)


def main() -> None:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    built = build(text)
    if "--check" in sys.argv:
        if built != text:
            sys.exit("DRIFT: PROJECT-PLANE-BOOTSTRAP.md embeds != canonical templates. "
                     "Re-run tools/build_bootstrap.py.")
        print("OK: bootstrap spec matches canonical templates (%d embeds)"
              % text.count("<!-- TPL:"))
        return
    DOC.write_text(built + ("" if built.endswith("\n") else "\n"), encoding="utf-8", newline="\n")
    print("Built %s (%d embeds)" % (DOC.name, built.count("<!-- TPL:")))


if __name__ == "__main__":
    main()
