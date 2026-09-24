"""Load clearly-labelled SAMPLE lines so the example has something to show.

The load is recorded as a ``sample-load`` event, and every page says the data includes samples
while that event exists -- sample rows must never pass as real ones.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from budget.models import BudgetLine, LineEvent

SAMPLE = [
    ("OPS-101", "Cloud hosting", "platform", "open", "42000", "38950.40"),
    ("OPS-102", "Monitoring licences", "platform", "over", "8000", "9420.00"),
    ("PPL-201", "Contractor hours", "delivery", "over", "60000", "71210.55"),
    ("PPL-202", "Training", "delivery", "open", "12000", None),
    ("FAC-301", "Office equipment", "facilities", "open", "15000", "6400.00"),
    ("FAC-302", "Travel", "facilities", "over", "9000", "9310.00"),
]


class Command(BaseCommand):
    help = "Load labelled sample budget lines (idempotent)."

    def handle(self, *args, **opts):
        made = 0
        for code, title, team, status, planned, actual in SAMPLE:
            _, created = BudgetLine.objects.get_or_create(code=code, defaults={
                "title": title, "team": team, "status": status, "planned": Decimal(planned),
                "actual": Decimal(actual) if actual else None})
            made += created
        LineEvent.objects.create(actor="manage.py", action="sample-load", target="*",
                                 detail={"created": made, "of": len(SAMPLE)})
        self.stdout.write(f"sample lines: {made} created, {len(SAMPLE) - made} already present "
                          f"(of {len(SAMPLE)})")
