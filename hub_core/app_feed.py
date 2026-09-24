"""ONE APP'S SLICE OF THE BOARD -- what an app's own pages can show its people about itself.
Framework-free; the Django adapter serves it at /hub/app-feed.json?app=<slug>.

An app built around this hub wants three short lists on its own pages: what is being built
for it next (the checklist), what was built because somebody asked (announcements), and what
changed (what's new). Each app inventing its own source for those produces N subtly
different answers; this is the one answer, derived from the board's own records.

THE PATH. The app's SERVER fetches its feed (with its own hub credential when the adopter has
put the hub's reads behind authentication) and serves it to its signed-in readers from its
own origin. A reader's browser never needs to reach the hub for it.

MATCHING IS NAMED, NOT HIDDEN. Tasks and notes carry no app field, so a row belongs to an
app when the app's slug or display name appears in its title, acceptance, phase or touched
paths -- and every row says WHICH field matched, so a reader is never told a list is
complete when it is a text match.

WHAT'S NEW IS EMPTY BY DESIGN. A deploy record is not a change note: "a1b2c3d deployed"
tells a person nothing about what they can now do. Only the app knows which of its changes a
person would notice, and in what words, so an app publishes its own user-language notes by
replacing that key in the feed it serves. An empty list here is the honest answer, and the
metadata says so.
"""
from __future__ import annotations

import re

#: What a person will sit and read. Past this, a panel is a wall rather than a surface.
LIMIT = 25
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
_CLOSED = {"done", "dropped", "shadow"}


def terms_for(slug: str, name: str = "") -> list[str]:
    """The strings that mean "this app" in free text. Terms under four characters are
    dropped: a two-letter term matches everything and would make every row "this app's"."""
    out = {slug.lower(), slug.replace("-", " ").lower()}
    if name:
        out.add(name.strip().lower())
    return sorted(t for t in out if len(t) >= 4)


def _mentions(row: dict, terms: list[str]) -> str:
    for field in ("title", "acceptance", "phase"):
        value = str(row.get(field) or "").lower()
        if any(t in value for t in terms):
            return field
    for path in row.get("touches") or []:
        if any(t in str(path).lower() for t in terms):
            return "touches"
    return ""


def _preview(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > n * 0.6 else cut).rstrip(" .,;:") + "…"


def checklist(tasks: list, terms: list[str]) -> list[dict]:
    """Open board work that names the app -- the standing answer to "what's next"."""
    out = []
    for r in tasks:
        status = str(r.get("status") or "").lower()
        if status in _CLOSED:
            continue
        where = _mentions(r, terms)
        if not where:
            continue
        plan = [s for s in (r.get("plan") or []) if isinstance(s, dict)]
        done = sum(1 for s in plan if s.get("done"))
        meta = status or "open"
        if plan:
            meta += " · step %d of %d" % (min(done + 1, len(plan)), len(plan))
        out.append({
            "id": r.get("id"),
            "title": r.get("title") or r.get("id") or "(untitled)",
            "detail": _preview(r.get("acceptance"), 220) or None,
            "status": status or "open",
            "meta": "%s · matched on %s" % (meta, where),
            "matched_on": where,
            "urgent": str(r.get("priority") or "").lower() in {"p0", "p1"},
        })
    # Work in hand first, then alphabetical: the reader's question is "what is moving".
    out.sort(key=lambda x: (0 if x["status"] in ("in_progress", "active") else 1, x["title"]))
    return out


def announcements(notes: list, terms: list[str]) -> list[dict]:
    """Things built because somebody asked, naming this app. A capability delivered without
    telling the person who asked is one they never learn they have; this is where they learn."""
    out = []
    for n in notes:
        tags = [str(t).lower() for t in (n.get("tags") or [])]
        if "built-on-request" not in tags:
            continue
        body = str(n.get("body_md") or "")
        in_title = any(t in str(n.get("title") or "").lower() for t in terms)
        in_body = any(t in body.lower() for t in terms)
        if not (in_title or in_body):
            continue
        where = "title and body" if in_title and in_body else ("title" if in_title else "body")
        first = re.split(r"(?<=[.!?])\s", body.strip())[0] if body.strip() else ""
        provenance = n.get("provenance") or {}
        date = str(provenance.get("created_at") or "")[:10]
        out.append({
            "id": n.get("id"),
            "date": date,
            "kind": "built",
            "title": n.get("title") or "Built on request",
            "detail": _preview(first, 240) or None,
            "meta": " · ".join(x for x in (date, "matched on " + where) if x),
            "matched_on": where,
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out


def build(state: dict, slug: str, name: str = "") -> dict:
    """The feed for one app from a projected board state."""
    terms = terms_for(slug, name)
    by_type = state.get("by_type", {}) if isinstance(state, dict) else {}
    all_checklist = checklist(by_type.get("task") or [], terms)
    all_announce = announcements(by_type.get("note") or [], terms)
    rows_checklist, rows_announce = all_checklist[:LIMIT], all_announce[:LIMIT]
    return {
        "checklist": rows_checklist,
        "announcements": rows_announce,
        "whats-new": [],
        "metadata": {
            "app": slug,
            "matched_on": terms,
            # Shown / matched: a capped panel must say it is a head, not the whole list.
            "counts": {"checklist": len(rows_checklist),
                       "announcements": len(rows_announce), "whats-new": 0},
            "matched": {"checklist": len(all_checklist), "announcements": len(all_announce)},
            "limit": LIMIT,
            "how": ("Tasks and notes are matched on the app's slug or name appearing in them, "
                    "and each row names the field that matched: this is what NAMED the app, "
                    "not everything that concerns it."),
            "whats_new": ("The hub publishes no change notes -- a deploy record is not one. An "
                          "app serves its own user-language notes by replacing this key in the "
                          "feed it serves."),
        },
    }
