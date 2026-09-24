"""A lease that expires is a hand-back nobody wrote down -- so the hub writes it.

WHAT BREAKS WITHOUT THIS. The loop's promise is that a worker which picks up a task either
finishes it with evidence or hands it back with a reason. Both halves are spoken by the WORKER
(release, fail) and so both need the worker to still be alive and to still know what it held.
A worker killed with its machine, a harness that never started, a console closed mid-work, a
laptop that slept through its own clock: an announcement whose author is the casualty is the one
announcement that never arrives. The task keeps reading `in_progress` on every surface -- the
most expensive lie a board can tell, because a surface that lies about what is in flight stops
being read.

WHAT THIS IS. The hub holds the one fact that survives every one of those deaths: the LEASE. An
expired lease on an in-progress task is proof -- not inference -- that nobody is on it. This
module turns that proof into the missing half of the promise: one `handed_back` lifecycle row
naming who held it and how long ago it lapsed, and the task back to `todo`, where the queue
offers it again. The row is a lifecycle row (hub_core.plan), so no completeness count ever reads
an abandonment as progress.

WHY IT IS SAFE TO BE WRONG. The worst it can do is put a task back on the queue with a note
saying why: nothing is deleted, no checkpoint is lost, and a claim makes it in-progress again in
one call. It is deliberately slow to fire -- a grace period past the lease's own expiry, or a
long silence on an in-progress task that no lease holds -- so a worker mid-step when its lease
lapses is never taken out from under itself. Decision tasks wait on a person by definition and
are never handed back.

Pure: the adapter supplies the entities, the lease reader and the append.
"""
from __future__ import annotations

import datetime as _dt
import time

from . import plan as _plan

#: How long a lease must have been EXPIRED before its task counts as abandoned: margin for a
#: worker mid-step when its lease lapses, and for clock skew between worker and hub.
LEASE_GRACE_S = 3600
#: An in-progress task with no lease at all (released, or its sidecar lost) is abandoned once
#: nothing has moved on it for this long.
UNHELD_AFTER_S = 4 * 3600
#: Bound one pass: a sweep that rewrote forty tasks inside a board read would make the board slow
#: exactly when it is worst. The rest are caught on the next pass.
MAX_PER_SWEEP = 12


def _age_s(stamp, now: float) -> float | None:
    if stamp in (None, ""):
        return None
    try:
        return max(0.0, now - float(stamp))
    except (TypeError, ValueError):
        pass
    try:
        parsed = _dt.datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, now - parsed.timestamp())
    except (TypeError, ValueError):
        return None


def phrase(age_s) -> str:
    if age_s is None:
        return "an unknown time"
    if age_s < 3600:
        return "%d min" % int(age_s // 60)
    if age_s < 172800:
        return "%d h" % int(age_s // 3600)
    return "%d days" % int(age_s // 86400)


def handback_reason(task: dict, lease: dict | None, now: float | None = None, *,
                    grace_s: float = LEASE_GRACE_S, unheld_s: float = UNHELD_AFTER_S) -> str:
    """WHY this task should go back on the queue, in the words a person will read -- or "" when
    it should be left exactly where it is. Every uncertain case returns "": held, recently
    lapsed, a decision waiting on a person, or simply not in progress."""
    now = time.time() if now is None else now
    if not isinstance(task, dict) or task.get("type") != "task":
        return ""
    if str(task.get("status") or "") != "in_progress":
        return ""
    if task.get("work_kind") == "decision":
        return ""
    if lease:
        try:
            expires = float(lease.get("expires") or 0)
        except (TypeError, ValueError):
            return ""                         # an unreadable lease is not proof of anything
        if expires > now:
            return ""                         # somebody holds it
        lapsed = now - expires
        if lapsed < grace_s:
            return ""                         # just lapsed: the holder may be mid-step
        who = str(lease.get("agent") or "").strip() or "a worker"
        where = str(lease.get("machine") or "").strip()
        return ("The lease %s%s held on this task expired %s ago and nothing renewed it, so "
                "nobody is on it. Handed back by the hub so the board stops reading it as in "
                "progress; its checkpoints stand."
                % (who, " on %s" % where if where else "", phrase(lapsed)))
    prov = task.get("provenance") or {}
    age = _age_s(prov.get("updated_at") or prov.get("created_at"), now)
    if age is None or age < unheld_s:
        return ""
    return ("Nobody holds this task and nothing has moved on it for %s, so 'in progress' is not "
            "true of it. Handed back by the hub so the queue can offer it again; its checkpoints "
            "stand." % phrase(age))


def handback_payload(task: dict, why: str) -> dict:
    """The minimal delta that hands one task back: status todo and one self-counting row."""
    return {"type": "task", "status": "todo",
            "plan": _plan.with_lifecycle_row(task, "handed_back", why[:80], why)}


def candidates(entities: dict, read_lease, now: float | None = None, *,
               grace_s: float = LEASE_GRACE_S, unheld_s: float = UNHELD_AFTER_S,
               limit: int = MAX_PER_SWEEP) -> list:
    """``[(task, why)]`` for every in-progress task whose holder is provably gone, oldest id
    first, bounded by `limit`. An unreadable lease skips its task (fail closed)."""
    now = time.time() if now is None else now
    out = []
    for task in sorted((entities or {}).values(), key=lambda e: str(e.get("id") or "")):
        if len(out) >= limit:
            break
        if not isinstance(task, dict) or task.get("type") != "task" \
                or task.get("status") != "in_progress":
            continue
        try:
            lease = read_lease(task.get("id"))
        except Exception:                                    # noqa: BLE001
            continue
        why = handback_reason(task, lease, now, grace_s=grace_s, unheld_s=unheld_s)
        if why:
            out.append((task, why))
    return out
