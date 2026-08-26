#!/usr/bin/env python3
"""Board digest: one line per task/finding, so a fan-out agent never re-derives known work.

A discovery scout handed the whole board (or worse, the whole ledger) re-reads it every fan-out
and still misses the point — the ONLY thing it needs is a compact list of what already exists so
it does not re-report it. This emits one line per task/finding as `<status> <id> :: <title-slug>`
from the folded state (never a full-file dump), plus a `covered()` helper launchers use to ask
"is this candidate already on the board?" deterministically:

  covered() is decided by ID first, then by a normalized TITLE SLUG (lowercased alnum tokens, set
  overlap) — a deterministic signal, never fuzzy string similarity deciding identity. A candidate
  whose slug's significant tokens are a subset of an existing entity's is covered; a novel title
  is not.

Proven in production on two adopter boards (which converged on this tool independently — the
strongest argument that it belongs in the template).

CLI:  python tools/board_digest.py                 # print the digest
      python tools/board_digest.py --instruction   # print the digest + the do-not-redo preamble
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_STOP = frozenset(("the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "is", "be",
                   "so", "that", "with", "no", "not", "at", "it", "its", "as", "by", "fix",
                   "add", "make", "when", "than", "into", "from", "every"))
_DIGEST_TYPES = ("task", "note")
INSTRUCTION = (
    "BOARD DIGEST — one line per existing task/finding below. Do NOT re-report, re-file, or "
    "re-discover anything already covered here; surface only genuinely NEW work. Each line is "
    "`<status> <id> :: <title-slug>`.\n")


def _slug_tokens(title):
    return frozenset(t for t in re.findall(r"[a-z0-9]+", (title or "").lower())
                     if t not in _STOP and len(t) > 1)


def digest_lines(state):
    """One `<status> <id> :: <slug>` line per task/finding, id-sorted for stable diffs."""
    out = []
    by_type = state.get("by_type", {})
    for t in _DIGEST_TYPES:
        for e in sorted(by_type.get(t, []), key=lambda x: x.get("id", "")):
            slug = "-".join(sorted(_slug_tokens(e.get("title"))))
            out.append(f"{(e.get('status') or '?'):<10} {e['id']} :: {slug}")
    return out


def covered(candidate_title, state, candidate_id=None):
    """The id of the entity ALREADY representing this candidate on the board (truthy — callers
    that only need a boolean keep working), or None. ID match wins; else the candidate's
    significant title tokens being a subset of an existing task/finding's (>=2 shared, or a full
    subset for short titles) counts as covered. Deterministic, order-free."""
    entities = state.get("entities", {})
    if candidate_id and candidate_id in entities:
        return candidate_id
    cand = _slug_tokens(candidate_title)
    if not cand:
        return None
    for e in entities.values():
        if e.get("type") not in _DIGEST_TYPES:
            continue
        existing = _slug_tokens(e.get("title"))
        if not existing:
            continue
        shared = cand & existing
        if cand <= existing or (len(shared) >= 2 and len(shared) >= len(cand) - 1):
            return e.get("id")
    return None


def _state():
    from hub_core.site_package import apply as _site_settings
    _site_settings(ROOT)
    os.environ.setdefault("DEBUG", "1")
    os.environ.setdefault("SECRET_KEY", "board-digest")
    import django
    django.setup()
    from hub import hub_app
    return hub_app.current_state()


def main():
    ap = argparse.ArgumentParser(description="one-line-per-entity board digest for fan-out agents")
    ap.add_argument("--instruction", action="store_true",
                    help="prepend the do-not-re-report preamble (the discovery launcher form)")
    args = ap.parse_args()
    lines = digest_lines(_state())
    if args.instruction:
        print(INSTRUCTION)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
