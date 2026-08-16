"""Mint-time collision detection: does this new task already have a twin in flight?

Titles alone are weak identity. Duplicate work is more reliably signaled by the concrete surfaces
both tasks name: touched paths, verification subjects, and entity references. Those deterministic
tokens are extracted by pattern and compared by set intersection; fuzzy text similarity never
decides identity.

The result is advisory, never a refusal. Shared infrastructure legitimately appears in unrelated
tasks, while blocking a task mint can lose work. Candidates ride with the write response so a
worker can fold a real duplicate before implementation begins.
"""
import re

_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|ps1|sh|json|md|txt|js|css|html|ts|tsx|jsx|yml|yaml|toml|sql|go|rs|rb|java|c|h|cpp)\b")
# ANY project's id grammar, matching hub_core.ids.ID_RE — never one project's literal key. A
# hardcoded prefix here silently matches nothing on every board but the one it was written for,
# and a detector that quietly finds nothing is indistinguishable from a clean board.
_EID = re.compile(r"\b[a-z0-9][a-z0-9-]*:(?:task|adr|feat|gap|cap|deploy|note):[A-Za-z0-9_.-]+")
_TERMINAL = ("done", "dropped")
_SIGNAL_FIELDS = ("acceptance", "verification_command", "title")


def _norm_path(p):
    return p.replace("\\", "/").lstrip("./").lower()


def signals(entity):
    """The surfaces one task names: file paths (slash-normalized, lowercased) and hub entity ids.

    Read from `touches` FIRST — that field exists precisely to state which surfaces a task
    changes, so it is the signal stated structurally rather than parsed back out of prose — and
    then from acceptance, verification_command and title, because most tasks name their surfaces
    there whether or not anyone filled in `touches`. Pure tokens, no judgement: set intersection
    decides, never string similarity (the no-fragile-text-matching law)."""
    ent = entity or {}
    touches = ent.get("touches") or []
    if isinstance(touches, str):
        touches = [touches]
    out = {_norm_path(str(t)) for t in touches if str(t).strip()}
    blob = " ".join(str(ent.get(f) or "") for f in _SIGNAL_FIELDS)
    return (out
            | {_norm_path(p) for p in _PATH.findall(blob)}
            | {i.lower() for i in _EID.findall(blob)})


def mint_collisions(candidate, state, limit=3):
    """Non-terminal tasks that name a surface this candidate also names, most-shared first.

    Returns [{"id", "status", "title", "shared": [sorted signals]}], at most `limit`. The candidate
    never collides with itself, and a task that is done or dropped is history, not a twin."""
    mine = signals(candidate)
    if not mine:
        return []
    cid = (candidate or {}).get("id")
    hits = []
    for t in (state or {}).get("by_type", {}).get("task", []):
        if t.get("id") == cid or (t.get("status") or "").lower() in _TERMINAL:
            continue
        shared = mine & signals(t)
        if shared:
            hits.append({"id": t.get("id"), "status": t.get("status"),
                         "title": t.get("title"), "shared": sorted(shared)})
    hits.sort(key=lambda h: (-len(h["shared"]), h["id"] or ""))
    return hits[:limit]
