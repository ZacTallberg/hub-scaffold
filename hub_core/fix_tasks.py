"""EVERY APP ERROR IS A BOARD TASK, AND THE APP CAN WATCH IT GET FIXED.

An app that forwards its failures to the hub (``POST /hub/api/app-error``) sees them folded
into PROBLEMS (``hub_core.problems``): one per cause, with one state (unclaimed | in flight |
escalated | resolved), a claim that names who is on it, a resolve that carries the root cause
and its evidence, and a recurrence that REOPENS it. The people using the app see none of that.
This module adds two things on top of the problem and invents no second state:

* a MIRROR TASK per app problem on the board -- created when the problem first appears, moved
  along as it is claimed, escalated, resolved or reopened, and never able to say something the
  problem does not. A fixer adds checkpoints to it with the ordinary ``step <task>``; those are
  kept, in order, between "Picked up" and "Fixed".
* the per-app FIXES FEED an app's banner polls (``GET /hub/app-fixes.json?app=<slug>``), built
  from the problems and those tasks' plans, with a content tag so an unchanged answer is a 304.

THE STATUS MAP, and why in-flight is not a lease. A fix is held by a PROBLEM claim, not a task
lease, so the mirror reads ``todo`` (unclaimed), ``in_progress`` (claimed), ``blocked`` with the
escalation's ask or task as its dependency (escalated) and ``done`` (resolved). A mirror is
recognised by ``source: problem:<id>`` (``is_mirror``), and the lease sweep, the task-health
classifier and the readiness classifier (``flow``: never offered as a stale reclaim) leave it
alone: its "in progress" is true for as long as the PROBLEM is claimed, whatever the lease
table says.

NEVER UNATTENDED. The problem lane already routes an unclaimed app error to a responder; an
unattended mirror would send a second one at the same fault.

``done`` IS GRANTED, NOT WRITTEN. The resolve is the proof -- it requires a root cause and acks
every row -- and the adapter carries it through the hub's one done path (a short lease the hub
holds, then the shared terminal write), with ``verified_by`` = the root cause and
``evidence_uri`` = the resolve's evidence plus ``hub:problem:<id>``. A recurrence puts the task
back in flight with a "Came back" step.

Scope: problems of kind ``app`` that the bar puts ON the board. CI, machine and hub rows are not
an app user's business, and deferred rows are not work.

Pure and stdlib-only apart from the small id map (``problem-fix-tasks.json`` under the hub
dir); the adapter does the writes.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import atomic
from .canonical import content_hash

MAP_NAME = "problem-fix-tasks.json"
#: The writer named on every mirror event.
AGENT = "hub"
#: A problem resolved longer ago than this, and never mirrored, is history, not a new task.
MIRROR_RESOLVED_WITHIN_S = 3600
#: How long a fixed problem stays in an app's Fixes list.
FEED_RESOLVED_DAYS = 7
FEED_LIMIT = 20
SOURCE_PREFIX = "problem:"

AUTO_STEPS = ("Reported", "Picked up", "Waiting on a person", "Came back", "Fixed")
#: Auto rows a fixer's default ``step`` can land on (the first undone work step).
_UNDONE_AUTO = ("Waiting on a person", "Fixed")
_NOISE = re.compile(r"^(?:(?:console\.error|console\.warn|error|uncaught)\s*:\s*|\[(?:debug|info|error)\]\s*)+",
                    re.I)
_LABEL = {"unclaimed": "Reported", "in_flight": "Being fixed", "escalated": "Waiting on a person",
          "resolved": "Fixed"}


def is_mirror(task) -> bool:
    """A board task that mirrors a problem: its status is the PROBLEM's, not a lease's."""
    return isinstance(task, dict) and str(task.get("source") or "").startswith(SOURCE_PREFIX)


def short_title(message, limit: int = 110) -> str:
    """The error in words a person can read in a list: the prefixes a browser console or a
    logger puts in front ("console.error: [debug] ...") are not part of what went wrong."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    text = _NOISE.sub("", text).strip() or text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso(epoch) -> str:
    """An epoch as ISO-8601 UTC, or "" -- zero and garbage are absent, never 1970."""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return ""


# ------------------------------------------------------------------ the id map

def map_path(hub_dir) -> Path:
    return Path(hub_dir) / MAP_NAME


def read_map(hub_dir) -> dict:
    """{problem id: {task, app, at}}."""
    try:
        data = json.loads(map_path(hub_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_map(hub_dir, mapping: dict) -> None:
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    atomic.write_json(map_path(hub_dir), mapping, sort_keys=True)


# ------------------------------------------------------------------ problem -> task

def in_scope(p) -> bool:
    return isinstance(p, dict) and p.get("kind") == "app" and bool(p.get("on_board"))


def mirrorable(p, now: float | None = None) -> bool:
    """Should a problem with no mirror yet GET one? Open app problems on the board, and ones
    resolved within the last hour (a fix that raced the first reconcile still shows up)."""
    if not in_scope(p):
        return False
    if p.get("state") != "resolved":
        return True
    now = time.time() if now is None else now
    epoch = float((p.get("resolved") or {}).get("epoch") or 0)
    return epoch > 0 and (now - epoch) <= MIRROR_RESOLVED_WITHIN_S


def status_of(p) -> str:
    return {"unclaimed": "todo", "in_flight": "in_progress", "escalated": "blocked",
            "resolved": "done"}.get(str(p.get("state") or ""), "todo")


def _who(entry) -> str:
    entry = entry or {}
    agent = str(entry.get("agent") or entry.get("by") or "").strip()
    name = str(entry.get("name") or "").strip()
    return (agent + (" (" + name + ")" if name else "")).strip() or "a console"


def _rescued(row: dict) -> dict:
    """A fixer's checkpoint that landed on an auto row. ``step`` with no target marks the first
    undone work step done -- on a mirror that is "Fixed" (or "Waiting on a person"), which the
    problem, not the fixer, decides. The report is kept as the fixer's own checkpoint instead of
    being overwritten on the next sync."""
    note = str(row.get("note") or "").strip()
    out = {"step": (note.splitlines()[0][:120] if note else "Checkpoint"), "done": True,
           "kind": "checkpoint"}
    if note:
        out["note"] = note
    if row.get("note_at"):
        out["note_at"] = str(row["note_at"])
    for key in ("sha", "pipeline_id", "pipeline_url"):
        if row.get(key):
            out[key] = row[key]
    return out


def fixer_steps(p, existing) -> list:
    """The checkpoints a fixer wrote on the mirror, in the order they were written."""
    state = p.get("state")
    out = []
    for s in existing or []:
        if not isinstance(s, dict) or s.get("lifecycle"):
            continue
        name = s.get("step")
        if name not in AUTO_STEPS:
            out.append(dict(s))
        elif name in _UNDONE_AUTO and s.get("done") and s.get("note") and not (
                name == "Fixed" and state == "resolved"):
            out.append(_rescued(s))
    return out


def build_plan(p, existing=None) -> list:
    """The mirror's plan, derived from the problem, keeping every checkpoint a fixer added."""
    first = p.get("open_first") or p.get("first_seen") or 0
    count = int(p.get("count") or 1)
    reported = {"step": "Reported", "done": True, "kind": "checkpoint",
                "note": "%d occurrence%s in %s (%s)." % (count, "" if count == 1 else "s",
                                                         p.get("where"), p.get("subject") or "app")}
    if _iso(first):
        reported["note_at"] = _iso(first)
    plan = [reported]
    holder = p.get("holder") or {}
    res = p.get("resolved") or {}
    state = p.get("state")
    if holder or state == "resolved" or p.get("escalation"):
        step = {"step": "Picked up", "done": True, "kind": "checkpoint"}
        if holder:
            step["note"] = "%s is on it%s" % (
                _who(holder), (": " + str(holder.get("note"))) if holder.get("note") else ".")
            since = _iso(holder.get("since") or holder.get("at"))
            if since:
                step["note_at"] = since
        else:
            # The resolve releases the claim, so the note the fixer wrote when they picked it up
            # is kept from the task rather than replaced by whoever resolved it.
            prior = next((s for s in (existing or []) if isinstance(s, dict)
                          and s.get("step") == "Picked up" and s.get("note")), None)
            if prior:
                step.update({k: prior[k] for k in ("note", "note_at") if prior.get(k)})
            elif res:
                step["note"] = "%s took it." % _who(res)
            elif p.get("escalation"):
                step["note"] = "%s diagnosed it." % _who(p.get("escalation"))
        plan.append(step)
    plan.extend(fixer_steps(p, existing))
    esc = p.get("escalation") or {}
    if esc and state != "resolved":
        wait = {"step": "Waiting on a person", "done": False, "kind": "checkpoint",
                "note": "Blocked on %s%s" % (esc.get("blocked_on"),
                                             (": " + str(esc.get("note"))) if esc.get("note") else "")}
        if _iso(esc.get("at")):
            wait["note_at"] = _iso(esc.get("at"))
        plan.append(wait)
    if p.get("reopened"):
        when = str(res.get("at") or "")[:16].replace("T", " ")
        plan.append({"step": "Came back", "done": True, "kind": "checkpoint",
                     "note": "It happened again after it was marked fixed%s." % (
                         (" (" + when + "Z)") if when else "")})
    fixed = {"step": "Fixed", "done": state == "resolved", "kind": "checkpoint"}
    if state == "resolved" and res:
        fixed["note"] = str(res.get("note") or res.get("evidence") or "Resolved.")
        if res.get("at"):
            fixed["note_at"] = str(res["at"])
    plan.append(fixed)
    return plan


def evidence_for(p) -> list:
    """Where the fix can be checked: the resolve's own evidence, then the problem itself."""
    res = p.get("resolved") or {}
    out = []
    ev = str(res.get("evidence") or "").strip()
    if ev:
        out.append(ev[:600])
    out.append("hub:problem:%s" % p["id"])
    return out


def payload(p, existing=None) -> dict:
    """The mirror task the problem implies (the full field set a sync owns)."""
    status = status_of(p)
    esc = p.get("escalation") or {}
    body = {
        "title": ("Fix %s: %s" % (p.get("where"), short_title(p.get("message") or p.get("title"))))[:200],
        "status": status,
        "source": SOURCE_PREFIX + str(p["id"]),
        "work_kind": "product",
        "unattended": False,
        "priority": "P1" if str(p.get("severity") or "").lower() == "critical" else "P2",
        "acceptance": ("The %s problem %s is resolved on the board with its root cause, and it does "
                       "not come back. This task mirrors the problem: claim, escalate and resolve "
                       "the PROBLEM (POST /hub/api/problem/*); add checkpoints here with `step`."
                       % (p.get("where"), p["id"])),
        "plan": build_plan(p, (existing or {}).get("plan")),
        "touches": ["app:%s" % p.get("where")],
        # A blocked task names what it waits on; the escalation's ask or task is exactly that.
        "deps": [str(esc["blocked_on"])] if status == "blocked" and esc.get("blocked_on") else [],
    }
    if status == "done":
        res = p.get("resolved") or {}
        body["verified_by"] = [str(res.get("note") or res.get("evidence") or "resolved on the board")[:4000]]
        body["evidence_uri"] = evidence_for(p)
    return body


def _same(a, b) -> bool:
    empty = (None, [], "", {})
    if a in empty and b in empty:
        return True
    return a == b


def changed_fields(existing: dict, body: dict) -> dict:
    """The part of ``body`` the existing task does not already say. Empty = nothing to write,
    which is what makes a second reconcile pass write nothing."""
    return {k: v for k, v in body.items() if not _same((existing or {}).get(k), v)}


# ------------------------------------------------------------------ the feed

def fix_row(p, task=None, tid=None) -> dict:
    plan = (task or {}).get("plan") or build_plan(p)
    # A fixer's checkpoint is titled by its note's first line; the note is not repeated under it.
    steps = [{"step": s.get("step"), "done": bool(s.get("done")),
              "note": "" if (s.get("note") or "").strip() == str(s.get("step") or "").strip() else (s.get("note") or ""),
              "at": s.get("note_at") or ""}
             for s in plan if isinstance(s, dict) and not s.get("lifecycle")]
    done = sum(1 for s in steps if s["done"])
    state = p.get("state")
    holder = p.get("holder") or {}
    res = p.get("resolved") or {}
    label = _LABEL.get(state, "Reported")
    if p.get("reopened") and state != "resolved":
        label = "Came back"
    return {
        "id": p["id"],
        "task": tid,
        "title": short_title(p.get("message") or p.get("title")),
        "detail": str(p.get("message") or "")[:600],
        "state": state,
        "label": label,
        "severity": p.get("severity"),
        "count": p.get("count"),
        "first_seen": _iso(p.get("open_first") or p.get("first_seen")),
        "last_seen": _iso(p.get("last_seen")),
        # Who has it now; once fixed, who fixed it.
        "who": (_who(holder) if holder else ("fixed by %s" % _who(res) if res and state == "resolved" else "")),
        "steps": steps,
        "progress": {"done": done, "total": len(steps)},
        "fixed": ({"at": res.get("at") or "", "note": res.get("note") or "",
                   "evidence": res.get("evidence") or ""} if state == "resolved" else None),
    }


def app_fixes(problems, mapping: dict, entities: dict, slug: str, now: float | None = None) -> dict:
    """``{fixes, metadata, etag}`` for ONE app: its open problems first (most recent activity
    first), then the ones fixed in the last FEED_RESOLVED_DAYS. ``problems`` is the fold with
    resolved rows included; rows of any other app or kind never appear."""
    now = time.time() if now is None else now
    slug = str(slug or "").strip().lower()
    open_, fixed = [], []
    for p in problems or []:
        if not in_scope(p) or p.get("where") != slug:
            continue
        tid = (mapping.get(p["id"]) or {}).get("task")
        row = fix_row(p, (entities or {}).get(tid) if tid else None, tid)
        if p.get("state") == "resolved":
            epoch = float((p.get("resolved") or {}).get("epoch") or 0)
            if now - epoch <= FEED_RESOLVED_DAYS * 86400:
                fixed.append((epoch, row))
        else:
            since = p.get("since_last_s")
            open_.append((since if since is not None else 0, row))
    open_.sort(key=lambda x: x[0])
    fixed.sort(key=lambda x: -x[0])
    rows = ([r for _k, r in open_] + [r for _k, r in fixed])[:FEED_LIMIT]
    return {"fixes": rows,
            "metadata": {"app": slug, "open": len(open_), "fixed_recent": len(fixed),
                         "shown": len(rows), "limit": FEED_LIMIT,
                         "fixed_window_days": FEED_RESOLVED_DAYS,
                         "how": ("Each row is one error this app forwarded that reached the board, "
                                 "folded by cause. It is a board task too (`task`); its steps are the "
                                 "fixer's checkpoints between Reported and Fixed.")},
            "etag": content_hash(rows)[:16]}
