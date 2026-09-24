"""Budget lines and the append-only log of every change made to them."""
from django.db import models


class BudgetLine(models.Model):
    STATUS = [("open", "Open"), ("over", "Over budget"), ("archived", "Archived")]

    code = models.CharField(max_length=24, unique=True)
    title = models.CharField(max_length=200)
    team = models.CharField(max_length=80)
    status = models.CharField(max_length=12, choices=STATUS, default="open")
    planned = models.DecimalField(max_digits=12, decimal_places=2)
    #: None means NOT MEASURED YET -- rendered as a dash, never as 0.
    actual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.code

    @property
    def variance(self):
        return None if self.actual is None else self.actual - self.planned


class LineEvent(models.Model):
    """Append-only: actor, action, target and time for every change."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=128)
    action = models.CharField(max_length=48)
    target = models.CharField(max_length=24)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-at",)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("LineEvent rows are append-only")
        super().save(*args, **kwargs)
