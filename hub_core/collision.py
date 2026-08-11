"""Mint-time collision detection: does this NEW task already have a twin in flight?

Two seats built the same unit twice in one hour on 2026-08-03 (the live-seat lander fence, and the
oracle-tamper refactor-into-helper false positive) because each seat was bitten by the same
incident, each filed its own task minutes apart, and nothing on the write path compared them. The
existing dedup (tools/board_digest.covered) reads TITLE tokens and only the discovery-scout mints
run it; a worker filing discovered work POSTs straight to /hub/api/task and is compared to nothing.
Titles were never going to catch that pair anyway — "land-worktree.ps1 must not reap a LIVE seat's
worktree" and "worktree reaper eats live seats" share two loose tokens and no stem.

What they DID share is what the work touches. Every acceptance here names its own surfaces: file
paths, the command that proves it, the entity ids it argues about. Those are deterministic tokens
- extracted by pattern, compared by set intersection, never by string similarity deciding identity
(the no-fragile-text-matching law). Measured on the live board (50 non-terminal tasks): the
oracle-tamper pair shares 3 signals and the lander pair shares exactly 1 - so a rarity cutoff would
have MISSED the pair that motivated this, and the rule takes any shared signal.

The finding is ADVISORY, never a refusal: 42% of that backlog shares at least one signal with some
other task (median 2 partners), which is a glance when it rides along with the write and a wall if
it blocks one. Filing is never the place to lose work; the ranked list is there so the second seat
notices the first before it spends an hour.
"""
import re

_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|ps1|sh|json|md|txt|js|css|html|ts|tsx|jsx|yml|yaml|toml|sql|go|rs|rb|java|c|h|cpp)\b")
# ANY project's id grammar, matching hub_core.ids.ID_RE — never one project's literal key. A
# hardcoded prefix here silently matches nothing on every board but the one it was written for,
# and a detector that quietly finds nothing is indistinguishable from a clean board.
_EID = re.compile(r"\b[a-z0-9][a-z0-9-]*:(?:task|adr|feat|gap|cap|deploy|commit|note|finding|lesson|method|review|telemetry):[A-Za-z0-9_.-]+")
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
