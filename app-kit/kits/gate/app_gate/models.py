"""The gate's two tables: the roster (who may open this app, at which rung) and the gate's own
append-only event log (every sign-in, refusal and roster change).

TABLE NAMES ARE THIS APP'S. Apps built from one kit often share a database, and left on the
kit's own table names they share one physical roster: one app's roster page can deactivate
another app's members, one app's migration can rewrite every app's rows, and a single row
admits its holder to every app on the table. So both tables are named from
``settings.APP_GATE_TABLE_PREFIX`` — a literal committed in settings, never an environment
read — and the module refuses to import without one. The migrations read the SAME constants,
so the table migrate creates is always the table the ORM queries.
"""
from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _table_prefix() -> str:
    prefix = (getattr(settings, "APP_GATE_TABLE_PREFIX", "") or "").strip()
    if not prefix:
        slug = (getattr(settings, "APP_SLUG", "") or "").strip()
        prefix = slug.replace("-", "_")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", prefix or ""):
        raise ImproperlyConfigured(
            "APP_GATE_TABLE_PREFIX must be this app's own lowercase identifier "
            f"(got {prefix!r}); the gate refuses to create shared tables.")
    return prefix


TABLE_PREFIX = _table_prefix()
ACCESS_USER_TABLE = f"{TABLE_PREFIX}_gate_access_user"
GATE_EVENT_TABLE = f"{TABLE_PREFIX}_gate_event"


class AccessUser(models.Model):
    """One person's standing in this app. Never deleted: a revoked row is deactivated, so the
    record of who once had access survives and a mistaken revoke is one command to undo."""

    ROLE_SUPERADMIN = "superadmin"
    ROLE_ADMIN = "admin"
    ROLE_CONTRIBUTOR = "contributor"
    ROLE_MEMBER = "member"
    ROLE_VISITOR = "visitor"
    ROLE_CHOICES = [
        (ROLE_SUPERADMIN, "Super admin"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_CONTRIBUTOR, "Contributor"),
        (ROLE_MEMBER, "Member"),
        (ROLE_VISITOR, "Visitor"),
    ]
    #: The ladder. One definition: the roster command, the decorator and the checks read it.
    ROLE_ORDER = {
        ROLE_VISITOR: 1,
        ROLE_MEMBER: 2,
        ROLE_CONTRIBUTOR: 3,
        ROLE_ADMIN: 4,
        ROLE_SUPERADMIN: 5,
    }
    #: While the gate is deliberately OFF (local development), surfaces up to this rung stay
    #: open and everything above it refuses unless APP_GATE_UNGATED_DEV is set.
    ROLE_UNGATED_BASELINE = ROLE_MEMBER

    username = models.CharField(max_length=128, unique=True)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = ACCESS_USER_TABLE
        ordering = ("username",)

    def __str__(self) -> str:
        return f"{self.username} ({self.role}{'' if self.active else ', deactivated'})"


class GateEvent(models.Model):
    """Append-only: every sign-in, refusal and roster change, naming actor, action and outcome."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=128)
    action = models.CharField(max_length=48)
    outcome = models.CharField(max_length=24)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = GATE_EVENT_TABLE
        ordering = ("-at",)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("GateEvent rows are append-only")
        super().save(*args, **kwargs)
