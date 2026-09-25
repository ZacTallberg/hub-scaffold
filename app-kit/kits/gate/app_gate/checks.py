"""Boot-time refusals: a half-configured gate must never ship as an open door."""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags
from django.core.checks import Warning as CheckWarning
from django.core.checks import register


@register(Tags.security)
def gate_settings(_app_configs=None, **_kwargs):
    errors = []
    owners = getattr(settings, "APP_GATE_SUPERADMINS", [])
    if isinstance(owners, str) or not isinstance(owners, (list, tuple)):
        errors.append(Error(
            "APP_GATE_SUPERADMINS must be a list of usernames, not a string.",
            hint="Build it with a split (the settings kit's env_list does). Iterating a string "
                 "iterates its characters, so the owner guard would match nobody.",
            id="app_gate.E001"))
    elif settings.APP_GATE_REQUIRED and not owners:
        errors.append(CheckWarning(
            "The gate is on and APP_GATE_SUPERADMINS names nobody: this app has no owner "
            "the roster cannot lock out.", id="app_gate.W001"))
    spec = getattr(settings, "APP_GATE_AUTHENTICATOR", "")
    if ":" not in spec:
        errors.append(Error("APP_GATE_AUTHENTICATOR must be 'module:callable'", id="app_gate.E002"))
    public = tuple(getattr(settings, "APP_GATE_PUBLIC_PREFIXES", ()))
    if "/" in public or "" in public:
        errors.append(Error("APP_GATE_PUBLIC_PREFIXES may not contain '/' or '': one character "
                            "would publish the whole app.", id="app_gate.E003"))
    return errors


@register(Tags.database)
def gate_tables_are_app_scoped(_app_configs=None, **_kwargs):
    from . import models
    errors = []
    for model in (models.AccessUser, models.GateEvent):
        table = model._meta.db_table
        if table.startswith("app_gate_"):
            errors.append(Error(
                f"{model.__name__} is on the shared table {table!r}.",
                hint="Set APP_GATE_TABLE_PREFIX to this app's own identifier. A shared roster "
                     "lets another app's roster changes and migrations touch this app's members.",
                id="app_gate.E006"))
    return errors


def gate_ledger_collision(ours, recorded, tables, table):
    """The E007 decision, pure. ``ours``: this app's gate migration names on disk;
    ``recorded``: names django_migrations already holds for the app_gate label; ``tables``:
    tables that exist; ``table``: this app's roster table. Fires only in the silent state —
    every migration we ship is already recorded (by ANOTHER app on a shared database, since
    Django keys an applied migration by (app label, name)) and yet our table is absent, so
    ``migrate`` would apply nothing and the first sign-in would fail."""
    ours, recorded = set(ours), set(recorded)
    if not ours or not ours <= recorded or table in set(tables):
        return []
    return [Error(
        "django_migrations already records every app_gate migration this app ships "
        f"({', '.join(sorted(ours))}), but this app's gate table {table!r} does not exist.",
        hint="Another app built from this kit has already migrated app_gate on this database. "
             "Give this app its own database, or vendor the kit with `kits.py add gate <app> "
             "--slug <this app>` (it names the migrations and their dependencies from the slug) "
             "before migrating.",
        id="app_gate.E007")]


@register(Tags.database)
def gate_migrations_can_create_tables(_app_configs=None, databases=None, **_kwargs):
    from django.db import connections
    from django.db.migrations.loader import MigrationLoader

    from . import models
    errors = []
    for alias in databases or []:
        connection = connections[alias]
        try:
            tables = set(connection.introspection.table_names())
            if "django_migrations" not in tables:
                continue
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM django_migrations WHERE app = %s", ["app_gate"])
                recorded = [row[0] for row in cursor.fetchall()]
            ours = [name for app, name in
                    MigrationLoader(None, ignore_no_migrations=True).disk_migrations
                    if app == "app_gate"]
        except Exception as exc:                          # noqa: BLE001 — say so, never pass
            errors.append(CheckWarning(
                f"app_gate could not read the migration ledger on {alias!r} "
                f"({type(exc).__name__}), so it cannot tell whether migrate will create this "
                "app's gate tables.", id="app_gate.W007"))
            continue
        errors.extend(gate_ledger_collision(ours, recorded, tables,
                                            models.AccessUser._meta.db_table))
    return errors
