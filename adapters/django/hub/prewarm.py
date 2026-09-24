"""Startup prewarm: pay the cold costs once, off the request path.

A restarted process otherwise makes its FIRST requests pay for everything at once -- the ledger
open and reconcile, the fold, the board snapshot (with its audit) -- exactly when every open
board and every worker reconnects after a deploy. ``start()`` runs those steps once on a daemon
thread. Requests are never blocked by it: an arrival that needs the fold meanwhile builds or
joins it exactly as it would have without the thread, and then finds the memo filled.

Call ``start()`` from the SERVED entrypoint (wsgi.py / asgi.py) only, never from settings or an
app ready() hook, so tests and management commands do not warm. ``HUB_PREWARM=0`` disables it.
Each step's duration is reported by ``GET /hub/perf.json`` under ``prewarm``.
"""
import os
import threading
import time

_STATUS = {"state": "not_started", "steps": {}}
_LOCK = threading.Lock()


def status() -> dict:
    with _LOCK:
        return {"state": _STATUS["state"], "steps": dict(_STATUS["steps"]),
                **({"error": _STATUS["error"]} if "error" in _STATUS else {})}


def _step(name, fn):
    t0 = time.perf_counter()
    fn()
    with _LOCK:
        _STATUS["steps"][name + "_ms"] = round((time.perf_counter() - t0) * 1000, 1)


def _run():
    from . import hub_api, hub_app
    with _LOCK:
        _STATUS["state"] = "running"
    try:
        def fold():
            store = hub_app.store()          # the open itself reconciles the index with the log
            try:
                hub_api._projected(store, store.latest_cursor())
            finally:
                store.close()
        _step("fold", fold)
        _step("snapshot", lambda: hub_api._snapshot())
        with _LOCK:
            _STATUS["state"] = "done"
    except Exception as exc:                                  # noqa: BLE001 - never fatal
        # A failed warm-up only means the first request pays the cost, as it always did; the
        # error is recorded where perf.json shows it and nothing else changes.
        with _LOCK:
            _STATUS["state"] = "failed"
            _STATUS["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])


def start() -> bool:
    if (os.environ.get("HUB_PREWARM") or "1").strip() == "0":
        with _LOCK:
            _STATUS["state"] = "disabled"
        return False
    with _LOCK:
        if _STATUS["state"] != "not_started":
            return False
        _STATUS["state"] = "queued"
    threading.Thread(target=_run, name="hub-prewarm", daemon=True).start()
    return True
