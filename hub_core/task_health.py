"""Is an in-progress task actually MOVING, or is "in progress" a lie?

One classifier, read by the board's Workstream card, the operator inbox and the attention list,
so every surface agrees on a task's real state. A status word says only that somebody once
claimed the work; it cannot say whether anyone is still on it or whether it quietly finished.
Observed on a working board: a third of the "in progress" tasks held no live lease, half of
those had every checkpoint ticked and were never closed (masquerading as in flight forever),
and the rest had been abandoned mid-work a day or more earlier. Nothing managed the difference.

The buckets (for ``in_progress`` tasks; every other status passes through unchanged):

* ``moving``            — a step or an event touched it inside MOVING_WINDOW_S. Moving WINS,
                          including over a fully-ticked plan: a worker that narrates its steps
                          in order has done == total after every single step, and advertising
                          that as "finished and forgotten" invited a peer to close live work.
* ``complete_unclosed`` — every declared checkpoint done and nothing has moved since.
* ``stalled``           — checkpoints outstanding and nothing moved.
* ``orphaned``          — no checkpoints at all and nothing moved.

What this module refuses to do, each rule paid for once:

* It never asserts the OUTCOME. "All N checkpoints recorded" says every step was written down;
  it cannot see the deliverable. The notice says so, and a ticked checkpoint whose own words say
  work remains ("remaining:", "blocked on", "not done") makes the task never "closeable".
* Its evidence test is strict. A commit sha must contain a letter (a seven-digit figure in a
  note is not a commit), a URL needs a scheme and a host, a path needs two segments and an
  extension. The loose version accepted "and/or" as a path and told readers nearly every task
  carried something to verify against.
* A detector never names a CAUSE it has not established. The unstarted-request notice says
  what the hub can see (nothing holds it, nothing runs on it, no checkpoint) and who can act.
* Placeholders and a scheduler's own lifecycle rows never count (``hub_core.checkpoints``).
* A decision (``work_kind: decision``) waits on a person by design; it is never "stalled".

Pure and stdlib-only: callers pass the per-task activity map, leases and run rows.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import checkpoints

MOVING_WINDOW_S = 30 * 60          # a step or event this recent = genuinely in flight
STALE_AFTER_S = 4 * 3600           # in progress, no movement this long = paged, not just shown
UNSTARTED_AFTER_S = 15 * 60        # an unattended request nobody picked up this long = a wait to see

ACTIVE = "in_progress"
LIVE_RUN_STATES = ("working", "input_required", "handoff_pending", "cancel_requested")


def age_s(ts, now: float | None = None) -> float | None:
    """Seconds since an epoch number or ISO-8601 stamp; None when it cannot be read. Range-checked
    so a sentinel date is "absent", never an age of two thousand years."""
    now = time.time() if now is None else now
    if ts in (None, ""):
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if not 1990 <= parsed.year <= 2100:
            return None
        value = parsed.timestamp()
    return max(0.0, now - value)


def phrase(age) -> str:
    if age is None:
        return "an unknown time"
    if age < 3600:
        return "%d min" % int(age // 60)
    if age < 172800:
        return "%d h" % int(age // 3600)
    return "%d days" % int(age // 86400)


# A commit sha: 7-40 hex that is not a plain number.
_SHA_TOKEN = re.compile(r"(?<![0-9a-zA-Z])(?=[0-9a-f]{7,40}(?![0-9a-zA-Z]))"
                        r"[0-9a-f]*[a-f][0-9a-f]*(?![0-9a-zA-Z])")
# A URL with a scheme AND a host.
_URL_TOKEN = re.compile(r"(?i)\bhttps?://[^\s/]+\.[^\s/]+")
# A real path: a drive letter, or at least two segments ending in a file extension.
_PATH_TOKEN = re.compile(r"(?i)(?:[A-Za-z]:[\\/][^\s]+|[\w.-]+(?:[\\/][\w.-]+)+\.[a-z0-9]{1,6}\b)")

#: A checkpoint that says, in its own words, that work is still outstanding.
_SAYS_UNFINISHED = re.compile(
    r"(?i)\b(?:not done|isn'?t done|still outstanding|still to do|remaining\s*:|"
    r"parked until|blocked on|awaiting\b|pending\b|todo\s*:)")


def has_dereferencing_evidence(task) -> bool:
    """Is there SOMETHING to verify against — a structured commit, a sha/URL/path in a note, or
    recorded evidence? This decides only that; it is never evidence the outcome was reached."""
    if checkpoints.recorded_shas(task) or task.get("evidence_uri"):
        return True
    for s in task.get("plan") or []:
        note = str((s or {}).get("note") or "") if isinstance(s, dict) else ""
        if _SHA_TOKEN.search(note) or _URL_TOKEN.search(note) or _PATH_TOKEN.search(note):
            return True
    return False


def says_unfinished(task) -> str:
    """The step title or note in which a DONE checkpoint says work is still outstanding. Can
    only ever make a notice more cautious, never more confident."""
    for s in task.get("plan") or []:
        if not isinstance(s, dict) or not s.get("done"):
            continue
        for field in ("step", "note"):
            text = str(s.get(field) or "")
            if _SAYS_UNFINISHED.search(text):
                return text.strip()[:160]
    return ""


def is_decision(task) -> bool:
    return str((task or {}).get("work_kind") or "").strip().lower() == "decision"


def is_unattended(task) -> bool:
    return (task or {}).get("unattended") in (True, 1, "1", "true")


def owner_of(task) -> str:
    prov = task.get("provenance") or {}
    return str(task.get("owner") or prov.get("agent") or "")


def classify(task, activity=None, now: float | None = None) -> dict:
    """``{state, age_s, done, total, reason, ...}``. ``activity`` maps task id -> the epoch of
    its latest ledger event or live-console activity (max wins); the task's own provenance
    stamp is the fallback."""
    status = str(task.get("status") or "")
    if status != ACTIVE:
        return {"state": status or "todo", "age_s": None, "done": 0, "total": 0, "reason": ""}
    counts = checkpoints.progress(task)
    done, total = counts["done"], counts["total"]
    age = age_s((activity or {}).get(task.get("id")), now)
    if age is None:
        prov = task.get("provenance") or {}
        age = age_s(prov.get("updated_at") or prov.get("created_at"), now)
    if is_decision(task):
        return {"state": "moving", "age_s": age, "done": done, "total": total,
                "reason": "a decision waits on a person, not on work", "decision": True}
    if age is not None and age <= MOVING_WINDOW_S:
        return {"state": "moving", "age_s": age, "done": done, "total": total,
                "reason": ("all %d checkpoints recorded, but it moved within the last 30 min"
                           % total) if total and done >= total
                          else "it moved within the last 30 min"}
    if total and done >= total:
        unfinished = says_unfinished(task)
        return {"state": "complete_unclosed", "age_s": age, "done": done, "total": total,
                "reason": "all %d checkpoints recorded, still in progress" % total,
                "says_unfinished": unfinished,
                "closeable": has_dereferencing_evidence(task) and not unfinished}
    if total:
        return {"state": "stalled", "age_s": age, "done": done, "total": total,
                "reason": "%d/%d checkpoints recorded, nothing moved in %s" % (done, total, phrase(age))}
    return {"state": "orphaned", "age_s": age, "done": 0, "total": 0,
            "reason": "in progress with no checkpoints, untouched %s" % phrase(age)}


def summarize(tasks, activity=None, now: float | None = None) -> dict:
    """The Workstream card's buckets: every in-progress task in exactly one, with its reason."""
    buckets = {"moving": [], "complete_unclosed": [], "stalled": [], "orphaned": []}
    for t in tasks or []:
        c = classify(t, activity, now)
        if c["state"] in buckets:
            buckets[c["state"]].append({"id": t.get("id"), "title": t.get("title"),
                                        "owner": owner_of(t), **c})
    for rows in buckets.values():
        rows.sort(key=lambda r: -(r.get("age_s") or 0))
    buckets["counts"] = {k: len(v) for k, v in buckets.items() if isinstance(v, list)}
    buckets["counts"]["in_progress"] = sum(buckets["counts"].values())
    return buckets


def hygiene_items(tasks, activity=None, live_projects=None, now: float | None = None) -> list:
    """Inbox items (kind ``task-stall``) for the operator: a task nobody closed after every
    checkpoint was recorded, or one stalled/orphaned past STALE_AFTER_S. A task about a project
    a live console is standing in RIGHT NOW is not abandoned, and is not paged."""
    live = {str(p or "").strip().lower() for p in (live_projects or ()) if p}
    out = []
    for t in tasks or []:
        c = classify(t, activity, now)
        state, age = c["state"], c.get("age_s")
        if state not in ("complete_unclosed", "stalled", "orphaned"):
            continue
        project = str(t.get("project") or "").strip().lower()
        if live and project and project in live:
            continue
        tid = t.get("id")
        owner = owner_of(t) or "someone"
        if state == "complete_unclosed":
            if c.get("says_unfinished"):
                why = ("A ticked checkpoint still says work is outstanding: %r, so this is very "
                       "likely NOT finished." % c["says_unfinished"])
            elif c.get("closeable"):
                why = "A checkpoint names a commit, URL or path to start the check from."
            else:
                why = ("No checkpoint names a commit, URL or path, so there is nothing here to "
                       "verify it against.")
            out.append({
                "kind": "task-stall", "id": "taskhealth:%s" % tid, "task": tid, "state": state,
                "owner": owner, "closeable": bool(c.get("closeable")),
                "title": "%s has all %d checkpoints recorded and is still in progress"
                         % (tid, c["total"]),
                "body": "Every checkpoint %s declared is marked done and nothing has moved since, "
                        "so the board still shows it in flight. Read this as 'every step was "
                        "written down', NOT as 'the task achieved its outcome'. %s Confirm the "
                        "outcome, then finish it with proof." % (tid, why),
                "reply_cmd": "python -m hub_core.client recall %s" % tid,
                "age_s": age})
        elif age is not None and age >= STALE_AFTER_S:
            out.append({
                "kind": "task-stall", "id": "taskhealth:%s" % tid, "task": tid, "state": state,
                "owner": owner, "closeable": False,
                "title": "%s is %s: %s" % (tid, state, phrase(age)),
                "body": "%s is in progress but %s: %s. Reclaim and finish it, hand it back to the "
                        "queue, or let go of it." % (tid, state, c["reason"]),
                "reply_cmd": "python -m hub_core.client hand %s" % tid,
                "age_s": age})
    out.sort(key=lambda i: -(i.get("age_s") or 0))
    return out


def live_run_subjects(runs) -> set:
    """Task ids with a run that is still going (spawned, not reaped)."""
    return {str(r.get("task") or "") for r in runs or []
            if isinstance(r, dict) and str(r.get("status") or "") in LIVE_RUN_STATES}


def unstarted_requests(tasks, runs=None, leases=None, now: float | None = None) -> list:
    """Unattended ``todo`` tasks with no live lease and no live run, older than UNSTARTED_AFTER_S,
    split into the ones with NOTHING behind them and the ones plainly worked and never closed.

    STATUS IS NOT PROGRESS: a handed-back task that carries every checkpoint and a pushed commit
    is finished-not-closed, and calling it "nobody has started it" sends a reader to chase a lane
    that worked. The count is reported with the scheduler's share named; the claim rests on the
    hard fact (a structured commit, URL or path), never on the tick count."""
    now = time.time() if now is None else now
    held = {str(l.get("task") or "") for l in leases or []
            if isinstance(l, dict) and float(l.get("expires") or 0) > now}
    running = live_run_subjects(runs)
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or str(t.get("status") or "") != "todo":
            continue
        if not is_unattended(t) or is_decision(t):
            continue
        tid = str(t.get("id") or "")
        if tid in held or tid in running:
            continue
        prov = t.get("provenance") or {}
        age = age_s(prov.get("updated_at") or prov.get("created_at"), now)
        if age is None or age < UNSTARTED_AFTER_S:
            continue
        owner = owner_of(t) or "someone"
        priority = str(t.get("priority") or "")
        counts = checkpoints.progress(t)
        done, total, lifecycle = counts["done"], counts["total"], counts["lifecycle"]
        evidenced = has_dereferencing_evidence(t)
        unfinished = says_unfinished(t)
        if done or total or lifecycle:
            counted = "%d of %d checkpoint%s recorded" % (done, total, "" if total == 1 else "s")
            if lifecycle:
                counted += " (plus %d scheduler row%s, not counted)" % (
                    lifecycle, "" if lifecycle == 1 else "s")
            if evidenced:
                head = "%s reads as finished and is still open" % tid
                tail = ("A checkpoint says work remains: %r. Read it before closing." % unfinished
                        if unfinished else
                        "Verify that evidence, then close it: an open task that is done reads as "
                        "in flight to everyone.")
            else:
                head = "%s has checkpoints but nothing to verify against, and is still open" % tid
                tail = ("A checkpoint says work remains: %r." % unfinished if unfinished else
                        "Read the checkpoints: step it with evidence and close it, or say what is "
                        "left.")
            out.append({
                "kind": "task-stall", "id": "taskhealth:worked-open:%s" % tid, "task": tid,
                "state": "worked-not-closed" if evidenced else "worked-unverifiable",
                "owner": owner, "closeable": evidenced and not unfinished, "priority": priority,
                "done": done, "total": total, "lifecycle_steps": lifecycle, "evidenced": evidenced,
                "title": "%s (%s)" % (head, counted),
                "body": "%s is still todo and unattended (filed by %s), with %s. %s"
                        % (tid, owner, counted, tail),
                "reply_cmd": "python -m hub_core.client recall %s" % tid,
                "age_s": age})
            continue
        out.append({
            "kind": "task-stall", "id": "taskhealth:unstarted:%s" % tid, "task": tid,
            "state": "unstarted", "owner": owner, "closeable": False, "priority": priority,
            "done": 0, "total": 0, "evidenced": False,
            "title": "%s was filed for an unattended worker %s ago and nobody has started it"
                     % (tid, phrase(age)),
            "body": "%s is an unattended %s task (filed by %s). Nothing holds it, no run is live "
                    "on it, and no checkpoint has ever been recorded against it. The hub cannot "
                    "see from here why nothing picked it up. Start it yourself, or hand it to "
                    "someone who can." % (tid, priority or "unprioritised", owner),
            "reply_cmd": "python -m hub_core.client start %s" % tid,
            "age_s": age})
    out.sort(key=lambda i: -(i.get("age_s") or 0))
    return out
