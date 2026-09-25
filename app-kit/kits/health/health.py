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


def _loaded_head_sha(base) -> str:
    """The commit the checkout HEAD named when this module was imported ('' without one).
    Read from .git directly (worktree gitdir, loose and packed refs), never a subprocess. The
    same reader as app_health/commit.py; this file stays a single drop-in."""
    from pathlib import Path
    try:
        base = Path(base)
        git = base / ".git"
        if git.is_file():
            ref = git.read_text(encoding="utf-8").strip()
            if ref.startswith("gitdir:"):
                git = Path(ref.split(":", 1)[1].strip())
                if not git.is_absolute():
                    git = (base / git).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:40]
        name = head.split(":", 1)[1].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for root in (git, common):
            if (root / name).is_file():
                return (root / name).read_text(encoding="utf-8").strip()[:40]
        if (common / "packed-refs").is_file():
            for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + name):
                    return line.split(" ", 1)[0][:40]
    except OSError:
        pass
    return ""


#: What THIS process loaded, read once at import: a deploy stamp is the deploy's claim, and a
#: failed restart or a rollback leaves the new stamp beside a process serving the old code.
LOADED_SHA = (getattr(settings, "LOADED_SHA", None)
              if getattr(settings, "LOADED_SHA", None) is not None
              else _loaded_head_sha(getattr(settings, "BASE_DIR", ".")))


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
    """``commit`` is the checkout HEAD this process loaded (the deploy stamp only when there is
    no checkout); ``stamp_commit`` is what the deploy wrote; ``stamp_mismatch`` says the deploy
    claims one commit while the process runs another."""
    import os
    stamp = str(getattr(settings, "BUILD_ID", "") or getattr(settings, "DEPLOY_SHA", "")
                or os.environ.get("BUILD_ID", "") or "").strip()
    body = {"status": "ok", "application": _app(), "time": timezone.now().isoformat(),
            "commit": LOADED_SHA or stamp or "unknown",
            "commit_source": "checkout" if LOADED_SHA else ("stamp" if stamp else "none"),
            "stamp_commit": stamp or None}
    if LOADED_SHA and stamp and not (LOADED_SHA.startswith(stamp) or stamp.startswith(LOADED_SHA)):
        body["stamp_mismatch"] = True
    return JsonResponse(body)


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
