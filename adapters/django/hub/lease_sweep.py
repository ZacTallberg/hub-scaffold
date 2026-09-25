"""Run hub_core.lease_sweep from the board's own read paths -- throttled, fail-soft.

A board that cleans itself needs nothing scheduled, armed or remembered, which is the class of
failure the sweep answers: a hand-back that depends on a cron job is one more thing that can die
with the machine it runs on. Every write goes through the ordinary validated append (OCC, schema,
realtime publication), attributed to a visible system actor.
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
    from .hub_write import _append
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
        try:
            resp, status = _append("task", task["id"], _core.handback_payload(task, why),
                                   expected_version=task.get("version"), agent=ACTOR,
                                   idem="lease-handback:%s:v%s" % (task["id"], task.get("version")),
                                   etype="task.transitioned")
        except Exception as exc:                             # noqa: BLE001
            resp, status = {"error": type(exc).__name__}, 500
        if status == 200:
            done.append(task["id"])
            log.info("handed %s back to the queue: %s", task["id"], why[:120])
        else:
            refused[task["id"]] = "%s %s" % (status, str(resp)[:160])
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
