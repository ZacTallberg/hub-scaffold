"""The Django side of "every app error is a board task" (engine: hub_core/fix_tasks.py).

Writes go through the ordinary write seam (``hub_write._append`` / ``_append_create``), so
schema validation, optimistic concurrency, idempotency and the realtime publish all apply; the
one terminal transition (``done``, when the problem is resolved) goes through
``hub_write.hub_grant_done`` -- the same lease-then-commit path a verified deploy's automatic
close takes. There is no second way to mint ``done``.

WHEN IT RUNS. Synchronously after a problem is claimed, resolved, released or escalated (the
caller's response names the mirror task), and as a RECONCILE pass that catches what no verb
announces: a new error, an auto-resolve, a recurrence that reopened a problem. The reconcile is
a job of the background tick (``manage.py hubbackground``), but a single-process hub has no
background clock at all, so it is also KICKED from the read and ingest paths -- an app error
landing, and an app's banner polling its fixes -- onto a debounced daemon thread. Neither
caller waits on the fold or the ledger write, and a poll kicks at most once per
``RECONCILE_MIN_S``. In the ``web`` role the kick does nothing: the background process owns the
reconcile, and a second pass from the web process only fought it for the mirror lock.

THE MIRROR LOCK GUARDS THE MAP FILE, NOTHING ELSE. Held across the ledger fold it timed the
other process out; held across the LEDGER WRITE it did again, because the append waits on the
ledger's own write lock and a create re-reads the fold. The ledger write needs no outer lock: a
create carries the idempotency key ``fix-task:<pid>`` (the store dedupes it under its own lock),
an update carries ``expected_version``. Only the map's read-modify-write is a race, and it takes
milliseconds. A busy lock -- the mirror's or the ledger's (``StoreBusy``, retryable by contract) --
skips the pass; only a streak of busy passes is a stuck lock worth failing the job for.

A mirror whose problem AGED OUT of the retained error window is never visited again, so a task
frozen at the mirrored ``in_progress`` stayed there for good -- not claimable, so not finishable,
while every problem verb answered "no such problem". Such a task is handed back to the task lane
as ``todo``, once, with a checkpoint saying why; nothing is closed or deleted.
"""
from __future__ import annotations

import logging
import threading
import time

from hub_core import fix_tasks as core
from hub_core.process_lock import LockBusy, ProcessFileLock
from hub_core.store import StoreBusy

from . import hub_app

log = logging.getLogger(__name__)

RECONCILE_MIN_S = 20.0
KICK_DELAY_S = 2.0
_LAST = {"at": 0.0}
#: Consecutive reconcile passes that met a busy lock (the mirror's or the ledger's). One is two
#: processes racing for the same work; a streak this long is a stuck lock, and it fails the job.
_BUSY = {"n": 0}
BUSY_PASSES_BEFORE_REPORT = 3


def _entities() -> dict:
    return hub_app.current_state().get("entities") or {}


def _write(tid, existing, body, pid):
    """One write of the fields that changed. Returns (task id, reason-or-'')."""
    from . import hub_write
    delta = core.changed_fields(existing, body)
    if not delta:
        return tid, ""
    if delta.get("status") == "done" or (body.get("status") == "done" and existing.get("status") != "done"):
        # THE ONE DONE PATH: the rest of the fields first, then the hub grants done.
        rest = {k: v for k, v in delta.items()
                if k not in ("status", "verified_by", "evidence_uri")}
        if rest:
            resp, status = hub_write._append("task", tid, {"type": "task", **rest},
                                             expected_version=existing.get("version"),
                                             agent=core.AGENT, idem=None, etype="task.updated")
            if status != 200:
                return tid, "update refused: %s %s" % (status, resp)
        why = hub_write.hub_grant_done(
            tid, {"type": "task", "status": "done", "verified_by": body["verified_by"],
                  "evidence_uri": body["evidence_uri"]},
            credential_id="hub-problem-resolve",
            idem="fix-done:%s:%s" % (pid, "|".join(body["evidence_uri"])[:80]))
        return tid, why
    resp, status = hub_write._append("task", tid, {"type": "task", **delta},
                                     expected_version=existing.get("version"),
                                     agent=core.AGENT, idem=None, etype="task.updated")
    return tid, ("" if status == 200 else "update refused: %s %s" % (status, resp))


def sync(p, now: float | None = None) -> str | None:
    """Make the problem's mirror task say what the problem says. Returns the task id, or None
    when the problem is out of scope. Idempotent: a task that already matches is not written.
    The ledger fold and write run OUTSIDE the mirror lock; the lock covers the map file only.
    A busy ledger raises ``StoreBusy`` so the reconcile can skip the pass."""
    from . import hub_write
    now = time.time() if now is None else now
    pid = str((p or {}).get("id") or "")
    if not pid:
        return None
    hub_dir = hub_app.HUB_DIR
    tid = (core.read_map(hub_dir).get(pid) or {}).get("task")
    existing = _entities().get(tid) if tid else None
    if not existing:
        if not core.mirrorable(p, now):
            return None
        body = core.payload(p, None)
        first = dict(body)
        if first["status"] == "done":
            # Created open, then granted done like any other task.
            first["status"] = "todo"
            first.pop("verified_by", None)
            first.pop("evidence_uri", None)
        if first["status"] == "blocked" and not first.get("deps"):
            first["status"] = "todo"
        # No outer lock: the store dedupes "fix-task:<pid>" under its own (an EXACT key -- the
        # body changes as the problem recurs), so a racing or retried create answers with the
        # first one's record, including a create whose map write a busy lock refused.
        resp, status = hub_write._append_create("task", {"type": "task", **first},
                                                agent=core.AGENT, idem="fix-task:%s" % pid,
                                                etype="task.created", idem_exact=True)
        if status == 503:
            raise StoreBusy("ledger busy creating the fix task for %s" % pid)
        if status != 200:
            log.warning("fix task for %s not created: %s %s", pid, status, resp)
            return None
        tid = (resp.get("data") or {}).get("id")
        with ProcessFileLock(hub_dir, name=".problem-fix-tasks.lock", timeout=10):
            mapping = core.read_map(hub_dir)
            mapping[pid] = {"task": tid, "app": p.get("where"), "at": round(now, 3)}
            core.write_map(hub_dir, mapping)
        existing = _entities().get(tid)
        if not existing:
            return tid
    body = core.payload(p, existing)
    tid, why = _write(tid, existing, body, pid)
    if why:
        if why.startswith("update refused: 503"):
            raise StoreBusy("ledger busy updating fix task %s" % tid)
        log.warning("fix task %s for %s: %s", tid, pid, why)
    return tid


def sync_pid(pid) -> str | None:
    """After a claim / resolve / release / escalate: fail-soft, never the caller's problem."""
    try:
        from hub_core import problems as _problems
        p = _problems.find(hub_app.HUB_DIR, pid, hub_app.current_state())
        return sync(p) if p else None
    except Exception:                                        # noqa: BLE001
        log.warning("fix task sync for %s failed", pid, exc_info=True)
        return None


def reconcile(force: bool = False) -> dict:
    """Every in-scope app problem has its mirror, every mirror matches its problem, and a mirror
    whose problem aged out is handed back. A busy lock skips the pass (the next one picks up);
    only ``BUSY_PASSES_BEFORE_REPORT`` busy passes in a row raise."""
    from hub_core import problems as _problems
    now = time.time()
    if not force and now - _LAST["at"] < RECONCILE_MIN_S:
        return {"skipped": "throttled"}
    _LAST["at"] = now
    try:
        # The cold fold happens here, before any lock is touched.
        state = hub_app.current_state()
        probs, _meta = _problems.read(hub_app.HUB_DIR, state, include="resolved", now=now)
        mapped = core.read_map(hub_app.HUB_DIR)
        before = {m.get("task"): (_entities().get(m.get("task")) or {}).get("version")
                  for m in mapped.values() if isinstance(m, dict)}
        seen = 0
        for p in probs:
            if p.get("kind") != "app":
                continue
            if p["id"] in mapped or core.mirrorable(p, now):
                if sync(p, now):
                    seen += 1
        released = release_aged_out(probs, core.read_map(hub_app.HUB_DIR), now)
    except (LockBusy, StoreBusy):
        _BUSY["n"] += 1
        if _BUSY["n"] >= BUSY_PASSES_BEFORE_REPORT:
            raise
        log.info("fix task reconcile skipped: a lock is busy (%d in a row)", _BUSY["n"])
        return {"skipped": "busy", "busy_in_a_row": _BUSY["n"]}
    _BUSY["n"] = 0
    after = _entities()
    mapped = core.read_map(hub_app.HUB_DIR)
    written = sum(1 for m in mapped.values() if isinstance(m, dict)
                  and (after.get(m.get("task")) or {}).get("version") != before.get(m.get("task")))
    return {"mirrors": seen, "written": written, "released": released}


def release_aged_out(probs, mapped, now) -> list:
    """Hand back, as ``todo``, every ``in_progress`` mirror whose problem is no longer readable
    (aged out of the retained window), once, with a checkpoint naming the problem. Nothing is
    closed or deleted: a person or a responder finishes it on the work's own evidence."""
    from . import hub_write
    live = {p.get("id") for p in probs}
    entities = _entities()
    released = []
    for pid, entry in (mapped or {}).items():
        if pid in live or not isinstance(entry, dict):
            continue
        tid = entry.get("task")
        task = entities.get(tid) if tid else None
        if not isinstance(task, dict) or str(task.get("status") or "") != "in_progress":
            continue
        why = ("%s is no longer in the retained error window, so the problem lane cannot move this "
               "task any more. Handed back to the task lane as todo: claim it and finish it on the "
               "work's own evidence, or drop it." % pid)
        plan = [dict(s) for s in (task.get("plan") or []) if isinstance(s, dict)]
        plan.append({"step": "Released: its problem aged out of the error window", "done": True,
                     "kind": "lifecycle", "lifecycle": True, "note": why,
                     "note_at": core._iso(now)})
        resp, status = hub_write._append("task", tid, {"type": "task", "status": "todo",
                                                       "plan": plan},
                                         expected_version=task.get("version"), agent=core.AGENT,
                                         idem=None, etype="task.updated")
        if status == 503:
            raise StoreBusy("ledger busy releasing %s" % tid)
        if status == 200:
            released.append(tid)
        else:
            log.warning("could not hand back aged-out fix task %s: %s %s", tid, status, resp)
    return released


# ------------------------------------------------------------------ the kick

_KICK = {"thread": None, "pending": False}
_KICK_LOCK = threading.Lock()


def _kick_run():
    while True:
        time.sleep(KICK_DELAY_S)
        with _KICK_LOCK:
            _KICK["pending"] = False
        try:
            reconcile(force=True)
        except Exception:                                    # noqa: BLE001 - a pass, not a caller
            log.warning("fix task reconcile failed", exc_info=True)
        with _KICK_LOCK:
            if not _KICK["pending"]:
                _KICK["thread"] = None
                return


def kick(throttled: bool = False) -> None:
    """Ask for one reconcile pass soon. Never blocks and never raises. ``throttled`` (a poll):
    a pass that ran within RECONCILE_MIN_S is enough."""
    try:
        from . import roles
        if roles.role() == "web":
            return                       # the background process owns the reconcile
        if throttled and time.time() - _LAST["at"] < RECONCILE_MIN_S:
            return
        with _KICK_LOCK:
            _KICK["pending"] = True
            if _KICK["thread"] is None:
                t = threading.Thread(target=_kick_run, name="hub-fix-task-kick", daemon=True)
                _KICK["thread"] = t
                t.start()
    except Exception:                                        # noqa: BLE001
        pass


def app_fixes(slug: str) -> dict:
    """The feed body for one app (engine: hub_core.fix_tasks.app_fixes)."""
    from hub_core import problems as _problems
    now = time.time()
    probs, _meta = _problems.read(hub_app.HUB_DIR, hub_app.current_state(), include="resolved",
                                  app=slug, now=now)
    return core.app_fixes(probs, core.read_map(hub_app.HUB_DIR), _entities(), slug, now)
