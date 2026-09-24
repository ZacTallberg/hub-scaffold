"""One table for everything that went wrong, on either side of the wire.

A JS exception, a 500, a failed background job and a dead stream are the same question --
"what is broken right now?" -- asked from different places, so ONE read answers it.
"""
from django.db import models
from django.utils import timezone

# Apps assembled from a shared kit often share a database. Django's automatic table naming
# keeps an app's own models apart, but every copy of THIS package carries the same label, so
# two apps in one database would share one error table. If yours do, rename this BEFORE the
# first migrate (the migration reads it). Index names derive from it on purpose: an index name
# is unique per database, and unnamed ones freeze to the table name at migration time.
APP_ERROR_TABLE = "app_errors_app_error"
_IDX = APP_ERROR_TABLE[:18]                       # index names are capped at 30 characters


class AppError(models.Model):
    """A distinct failure with a count -- not a log line per occurrence."""

    class Kind(models.TextChoices):
        JS = "js", "Uncaught JavaScript error"
        PROMISE = "promise", "Unhandled promise rejection"
        HTTP = "http", "Failed request"
        STREAM = "stream", "Live stream error"
        SERVER = "server", "Server exception"
        BACKGROUND = "background", "Background job failure"
        DJANGO = "django", "Framework error"
        DATA = "data", "Data fault"
        OTHER = "other", "Logged error"
        AGENT = "agent", "Agentic chat fault"

    fingerprint = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.JS)
    message = models.TextField()
    stack = models.TextField(blank=True, default="")
    source = models.CharField(max_length=500, blank=True, default="")
    page_url = models.CharField(max_length=500, blank=True, default="")
    status = models.IntegerField(null=True, blank=True)
    actor = models.CharField(max_length=150, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    count = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    # Closing a row is a decision, not a delete: a recurrence re-opens it.
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        db_table = APP_ERROR_TABLE
        ordering = ["-last_seen"]
        indexes = [
            models.Index(fields=["-last_seen"], name=f"{_IDX}_last_idx"),
            models.Index(fields=["kind", "-last_seen"], name=f"{_IDX}_kind_idx"),
            models.Index(fields=["resolved_at"], name=f"{_IDX}_res_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message[:80]}"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @property
    def severity(self) -> str:
        """What a reader looks at first. Server-side kinds are the app itself failing and are
        critical regardless of count; a data fault escalates when it recurs; the unclassified
        buckets escalate by count so ordinary logging never becomes a pager."""
        if self.kind in ("server", "background", "django", "agent"):
            return "critical"
        if self.kind == "http" and (self.status or 0) >= 500:
            return "critical"
        if self.kind == "data":
            return "critical" if self.count > 5 else "high"
        return "high" if self.count > 5 else "medium"
