"""Run hub_core.lease_sweep from the board's own read paths -- throttled, fail-soft.

A board that cleans itself needs nothing scheduled, armed or remembered, which is the class of
failure the sweep answers: a hand-back that depends on a cron job is one more thing that can die
with the machine it runs on. Every write goes through the ordinary validated append (OCC, schema,
realtime publication), attributed to a visible system actor.
"""
from __future__ import annotations

import logging
import os
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
    return {"handed_back": done, "refused": refused}
