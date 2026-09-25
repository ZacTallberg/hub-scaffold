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
``RECONCILE_MIN_S``.
"""
from __future__ import annotations

import logging
import threading
import time

from hub_core import fix_tasks as core
from hub_core.process_lock import ProcessFileLock

from . import hub_app

log = logging.getLogger(__name__)

RECONCILE_MIN_S = 20.0
KICK_DELAY_S = 2.0
_LAST = {"at": 0.0}


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
    when the problem is out of scope. Idempotent: a task that already matches is not written."""
    from . import hub_write
    now = time.time() if now is None else now
    pid = str((p or {}).get("id") or "")
    if not pid:
        return None
    hub_dir = hub_app.HUB_DIR
    with ProcessFileLock(hub_dir, name=".problem-fix-tasks.lock", timeout=10):
        mapping = core.read_map(hub_dir)
        tid = (mapping.get(pid) or {}).get("task")
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
            resp, status = hub_write._append_create("task", {"type": "task", **first},
                                                    agent=core.AGENT, idem="fix-task:%s" % pid,
                                                    etype="task.created")
            if status != 200:
                log.warning("fix task for %s not created: %s %s", pid, status, resp)
                return None
            tid = (resp.get("data") or {}).get("id")
            mapping[pid] = {"task": tid, "app": p.get("where"), "at": round(now, 3)}
            core.write_map(hub_dir, mapping)
            existing = _entities().get(tid)
            if not existing:
                return tid
        body = core.payload(p, existing)
        tid, why = _write(tid, existing, body, pid)
        if why:
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
    """Every in-scope app problem has its mirror, and every mirror matches its problem."""
    from hub_core import problems as _problems
    now = time.time()
    if not force and now - _LAST["at"] < RECONCILE_MIN_S:
        return {"skipped": "throttled"}
    _LAST["at"] = now
    probs, _meta = _problems.read(hub_app.HUB_DIR, hub_app.current_state(), include="resolved",
                                  now=now)
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
    after = _entities()
    mapped = core.read_map(hub_app.HUB_DIR)
    written = sum(1 for m in mapped.values() if isinstance(m, dict)
                  and (after.get(m.get("task")) or {}).get("version") != before.get(m.get("task")))
    return {"mirrors": seen, "written": written}


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
