"""Liveness and readiness -- two different questions, answered by two different probes.

``/health/live/`` answers "is the process serving requests?" and touches nothing else, so a
supervisor restarts only a process that is actually wedged.

``/health/ready/`` answers "can this process do its job?" and therefore MUST touch the database
with a query that needs the app's own tables. ``SELECT 1`` is not readiness: it succeeds against
a database with no tables at all, so a deploy that forgot to migrate reports ready and serves
500s. A readiness probe that is byte-identical to liveness -- 200 with the database gone -- is
the defect a deploy gate exists to catch, so the two must be able to disagree.

Both paths are exempt from the identity gate (a deploy has no session). Neither returns data.
"""
from __future__ import annotations

import time

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import commit as _commit

_commit.prime(settings)          # the commit this process LOADED, read once at import


@never_cache
@require_GET
def live(request):
    """``commit`` is what this process loaded; ``stamp_commit`` is what the deploy claims.
    ``stamp_mismatch`` says a deploy claims one commit while the process runs another -- a
    failed restart or a rollback -- which a deploy gate reading the stamp would pass."""
    return JsonResponse({"status": "live", "build": getattr(settings, "BUILD_ID", ""),
                         **_commit.identity(settings)})


@never_cache
@require_GET
def ready(request):
    started = time.monotonic()
    from django.db.migrations.executor import MigrationExecutor

    try:
        with connection.cursor() as cursor:
            # A real read of a table migrate creates; fails on an unmigrated database.
            cursor.execute("SELECT COUNT(*) FROM django_migrations")
            applied = cursor.fetchone()[0]
        pending = MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes())
    except Exception as exc:                                     # noqa: BLE001
        return JsonResponse({"status": "not-ready", "reason": type(exc).__name__},
                            status=503)
    if pending:
        return JsonResponse({"status": "not-ready",
                             "reason": f"{len(pending)} migration(s) not applied"}, status=503)
    return JsonResponse({"status": "ready", "migrations_applied": applied,
                         "ms": round((time.monotonic() - started) * 1000)})
