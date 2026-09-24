"""Liveness and readiness views whose payload is a CONTRACT with the deploy gate.

Wire both, and point the deploy gate at the READY path -- liveness passes with the database
down, so a gate on it proves nothing:

    from health import health_live, health_ready
    path("health/live/", health_live), path("health/ready/", health_ready),

THE READINESS SHAPE (patterns/deploy-contract.md, "The readiness payload"):

    {"status": "ok" | "degraded", "application": <slug>, "time": <iso>,
     "checks": [{"name": <str>, "status": "ok" | <failure word>, "detail": <str>}, ...]}

* `checks` is a LIST of {name, status, detail}; a dict keyed by check name reaches a gate
  that reads a list as ONE entry with no status, and a healthy app fails with an empty
  failing list.
* "ok" is the only passing status. A MODE the app can run in (armed/unarmed, configured or
  not) is reported as status "ok" with the mode in `detail`; a mode spelled as its own status
  word reddens a healthy deploy.
* An empty or missing `checks` is a failure at the gate: a readiness that inspected nothing
  proves nothing.
* A check on a SHARED dependency this app does not own (a model server, a directory, a shared
  database) is named so the gate can treat it as ADVISORY; it still reports honestly here.
* 503 while not ready is the probe WORKING; the error kit records it as a warning.
"""
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


def _app() -> str:
    return getattr(settings, "APP_SLUG", "") or "app"


def check_database() -> dict:
    """Touch a REAL table: `SELECT 1` answers green on a database with no tables at all."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            return {"name": "database", "status": "failed",
                    "detail": "%d unapplied migration(s)" % len(pending)}
        from django.apps import apps
        model = next(iter(m for m in apps.get_models() if m._meta.managed), None)
        if model is not None:
            model.objects.exists()
        return {"name": "database", "status": "ok",
                "detail": connection.vendor + (" via " + model._meta.db_table if model else "")}
    except Exception as exc:                                   # noqa: BLE001
        return {"name": "database", "status": "failed", "detail": type(exc).__name__}


def check_error_forwarder() -> dict:
    """A MODE, not a health state: always "ok", with armed/unarmed and delivery in detail."""
    try:
        from app_errors import capture
    except Exception:                                          # noqa: BLE001
        return {"name": "error_forwarder", "status": "ok", "detail": "kit not installed"}
    fwd = capture.forwarding_status()
    if not fwd["armed"]:
        return {"name": "error_forwarder", "status": "ok",
                "detail": "unarmed: failures stay local"}
    return {"name": "error_forwarder", "status": "ok",
            "detail": "armed; %d delivered, %d failed%s" % (
                fwd["sent"], fwd["failed"],
                ", last " + fwd["last_detail"] if fwd["last_detail"] else "")}


#: Add this app's own checks here (each returns {name, status, detail}).
CHECKS = [check_database, check_error_forwarder]


@never_cache
@require_GET
def health_live(request):
    return JsonResponse({"status": "ok", "application": _app(),
                         "time": timezone.now().isoformat()})


@never_cache
@require_GET
def health_ready(request):
    checks = []
    for check in CHECKS:
        try:
            checks.append(check())
        except Exception as exc:                               # noqa: BLE001
            checks.append({"name": getattr(check, "__name__", "check"), "status": "failed",
                           "detail": type(exc).__name__})
    ok = bool(checks) and all(c.get("status") == "ok" for c in checks)
    return JsonResponse({"status": "ok" if ok else "degraded", "application": _app(),
                         "time": timezone.now().isoformat(), "checks": checks},
                        status=200 if ok else 503)
