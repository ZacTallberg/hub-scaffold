"""One tick of the background role (``manage.py hubbackground``). See ``hub/roles.py``.

Each tick folds the ledger at its current head, materializes the delivery projection for that
exact key if no sidecar holds it yet, publishes a ``projection.delivery`` wake-up so every open
stream re-reads it, re-arms the lease truth timers, and stamps the backgrounder's clock. A tick
that fails still stamps the clock with the error, so a web process can tell "alive but failing"
(visible in perf.json) from "dead" (stale clock -> the web process builds for itself).
"""
import time

from . import delivery, hub_api, hub_app, realtime, roles


def tick() -> dict:
    t0 = time.perf_counter()
    report = {"cursor": None, "delivery": "skipped"}
    try:
        store = hub_app.store()
        try:
            cursor = store.latest_cursor()
            _events, state = hub_api._projected(store, cursor)
        finally:
            store.close()
        report["cursor"] = cursor.get("seq", 0)
        if delivery.repository_available():
            key = hub_api._delivery_key(cursor, None)
            if hub_api.read_delivery_sidecar(key) is None:
                block = delivery.block(state, served=None)
                hub_api.write_delivery_sidecar(key, block)
                realtime.publish(hub_app.HUB_DIR,
                                 {"kind": "projection.delivery", "cursor": cursor.get("seq", 0)},
                                 channel=hub_app.PROJECT_KEY)
                report["delivery"] = "materialized"
            else:
                report["delivery"] = "current"
        for lease in hub_app.leases():
            hub_app._schedule_lease_truth(lease)
    except Exception as exc:                                  # noqa: BLE001 - keep ticking
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    report["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    roles.write_clock(hub_app.HUB_DIR, **report)
    return report
