"""Run hub_core.lease_sweep from the board's own read paths -- throttled, fail-soft.

A board that cleans itself needs nothing scheduled, armed or remembered, which is the class of
failure the sweep answers: a hand-back that depends on a cron job is one more thing that can die
with the machine it runs on. Every write goes through the ordinary validated append (OCC, schema,
realtime publication), attributed to a visible system actor.

TWO REFUSALS ARE NOT FAILURES. The sweep folds the board once and appends a hand-back per
abandoned task with THAT fold's version, so any write to the task in between (a deploy-record
step, a checkpoint) turns the hand-back into a 409 that nothing re-read: the task sat in progress
with no holder and the board got a refusal row per pass. A 409 conflict now re-reads the CURRENT
task, re-asks whether it is still due (if the write was somebody picking it up, it is not), and
retries, bounded at ``CONFLICT_ATTEMPTS``. A 503 busy (the ledger's write lock unavailable) wrote
nothing and the next pass asks again, so it reaches the board only after
``BUSY_PASSES_BEFORE_REPORT`` consecutive busy passes on the same task -- a hub stall must not
read as a stuck task. Every other refusal still surfaces on its first pass.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from hub_core import lease_sweep as _core

from . import hub_app

log = logging.getLogger("hub.lease_sweep")

ACTOR = "hub-lease-sweep"
SWEEP_INTERVAL_S = 300
_LAST = {"at": 0.0}
CONFLICT_ATTEMPTS = 3
BUSY_PASSES_BEFORE_REPORT = 3
#: task id -> consecutive sweep passes whose hand-back was refused 503 busy.
_BUSY_PASSES: dict = {}


def _setting(name, default):
    try:
        return max(60, int(hub_app._dj_setting(name, os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def sweep(force: bool = False, now: float | None = None) -> dict:
    """Hand back every in-progress task whose lease holder is provably gone. Never raises."""
    now = time.time() if now is None else now
    if not force and now - _LAST["at"] < SWEEP_INTERVAL_S:
        return {"handed_back": [], "skipped": "throttled"}
    _LAST["at"] = now
    done, refused = [], {}
    try:
        roster = hub_app.roster()
    except Exception:                                        # noqa: BLE001 - unprovable, not gone
        roster = None
    leases = {}

    def _read(task_id):
        lease = hub_app._read_lease(task_id)
        leases[task_id] = lease
        return lease

    try:
        entities = hub_app.current_state().get("entities") or {}
        rows = _core.candidates(entities, _read, now,
                                grace_s=_setting("HUB_LEASE_SWEEP_GRACE_S", _core.LEASE_GRACE_S),
                                unheld_s=_setting("HUB_LEASE_SWEEP_UNHELD_S",
                                                  _core.UNHELD_AFTER_S),
                                roster=roster,
                                gone_grace_s=hub_app.gone_grace_s())
    except Exception as exc:                                 # noqa: BLE001 - never break a read
        log.warning("lease sweep could not read the board (%s)", type(exc).__name__)
        return {"handed_back": [], "skipped": type(exc).__name__}
    for task, why in rows:
        lease = leases.get(task["id"]) or {}
        if float(lease.get("expires") or 0) > now:
            # The holder's console is provably gone past its grace but its clock still runs:
            # void exactly THAT lease (by its fencing token, never a successor's) before handing
            # the task back, so the queue can offer it and the gone console cannot close it.
            if not hub_app.void_lease(task["id"], lease.get("token"), why):
                refused[task["id"]] = "lease changed hands; left alone"
                continue
        resp, status, why = _hand_back(task, why, lease, now, roster)
        if status == 503:
            _BUSY_PASSES[task["id"]] = _BUSY_PASSES.get(task["id"], 0) + 1
        else:
            _BUSY_PASSES.pop(task["id"], None)
        if status == 200:
            done.append(task["id"])
            log.info("handed %s back to the queue: %s", task["id"], why[:120])
        elif status == 0:
            log.info("%s is no longer due for a hand-back: %s", task["id"], why[:120])
        elif status == 503 and _BUSY_PASSES[task["id"]] < BUSY_PASSES_BEFORE_REPORT:
            log.info("hand-back of %s deferred: the ledger is busy (%d pass(es) in a row)",
                     task["id"], _BUSY_PASSES[task["id"]])
        else:
            refused[task["id"]] = "%s %s%s" % (
                status, str(resp)[:160],
                (" (busy on %d consecutive passes)" % _BUSY_PASSES[task["id"]])
                if status == 503 else "")
    if refused:
        log.warning("lease sweep: %d hand-back(s) refused: %s", len(refused), refused)
        # A refused hand-back leaves a task reading "in progress" with nobody on it -- the lie
        # this module exists to end -- so it reaches the operational stream, not only the
        # service log. One row per task, folded by the problem queue and throttled by the
        # error log itself. A lease that merely changed hands is not a failure.
        for tid, err in list(refused.items())[:5]:
            if str(err).startswith("lease changed hands"):
                continue
            try:
                hub_app.record_error(
                    "hub.lease_sweep", "lease sweep could not hand %s back to the queue: %s"
                    % (tid, str(err)[:160]), severity="error", code="lease_handback_refused",
                    details="the task stays in progress with no holder until this is cleared",
                    context={"component": "hub", "task": tid})
            except Exception:                                # noqa: BLE001 - never break a sweep
                pass
    if done:
        # A task the deploy record already proved live, refused only because this lease was
        # still held, can close now -- in the close sweep's own thread, never on this read.
        try:
            from . import deploy_close
            deploy_close.sweep_async(force=True)
        except Exception:                                    # noqa: BLE001
            log.warning("could not start the deploy-close sweep", exc_info=True)
    return {"handed_back": done, "refused": refused}


def _hand_back(task, why, lease, now, roster):
    """``(response, status, why)`` for one hand-back. A 409 re-reads the task, re-asks
    ``handback_reason`` of its CURRENT state and retries; ``status`` 0 means it is no longer due
    (somebody picked it up, or it moved on), which is nobody's failure."""
    from .hub_write import _append
    resp, status = {}, 0
    for attempt in range(CONFLICT_ATTEMPTS):
        try:
            resp, status = _append("task", task["id"], _core.handback_payload(task, why),
                                   expected_version=task.get("version"), agent=ACTOR,
                                   idem="lease-handback:%s:v%s" % (task["id"], task.get("version")),
                                   etype="task.transitioned")
        except Exception as exc:                             # noqa: BLE001
            return {"error": type(exc).__name__}, 500, why
        if status != 409 or attempt == CONFLICT_ATTEMPTS - 1:
            return resp, status, why
        try:
            fresh = (hub_app.current_state().get("entities") or {}).get(task["id"])
            current = hub_app._read_lease(task["id"])
        except Exception:                                    # noqa: BLE001
            return resp, status, why
        if not fresh or str(fresh.get("status") or "") != "in_progress":
            return {"no_longer_due": True}, 0, "it is no longer in progress"
        if current and float(current.get("expires") or 0) > time.time() \
                and current.get("token") != (lease or {}).get("token"):
            return {"reclaimed": True}, 0, "somebody claimed it meanwhile"
        if not (lease and float(lease.get("expires") or 0) > now):
            # The verdict came from the task itself (an expired lease, or nobody holding it): ask
            # it again of what the task and its lease say NOW. A lease this pass VOIDED because
            # its console is provably gone keeps its verdict -- the void stands.
            again = _core.handback_reason(
                fresh, current, now,
                grace_s=_setting("HUB_LEASE_SWEEP_GRACE_S", _core.LEASE_GRACE_S),
                unheld_s=_setting("HUB_LEASE_SWEEP_UNHELD_S", _core.UNHELD_AFTER_S),
                roster=roster, gone_grace_s=hub_app.gone_grace_s())
            if not again:
                return {"no_longer_due": True}, 0, "no longer due after it moved"
            why = again
        task = fresh
    return resp, status, why


_ASYNC_LOCK = threading.Lock()


def sweep_async() -> bool:
    """The board read's entry point: the pass runs in ONE daemon thread, never on the request.

    sweep() reads a lease file per in-progress task and appends a hand-back per abandoned one;
    on a read path that was the reader's latency, and on a cold process it measured seconds on
    the instance this was lifted from while clients with an 8 s budget timed out on the board.
    The throttle is checked here (cheap) and the pass is single-flight: a second reader while
    one runs returns at once. Returns whether a pass was started."""
    if time.time() - _LAST["at"] < SWEEP_INTERVAL_S:
        return False
    if not _ASYNC_LOCK.acquire(blocking=False):
        return False

    def _run():
        try:
            sweep()
        except Exception:                                    # noqa: BLE001
            log.warning("lease sweep: background pass failed", exc_info=True)
        finally:
            _ASYNC_LOCK.release()
    try:
        threading.Thread(target=_run, name="hub-lease-sweep", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        _ASYNC_LOCK.release()
        return False
    return True
