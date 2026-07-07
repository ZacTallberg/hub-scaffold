"""manage.py hubmaterialize — materialize a REVIEW-AND-REIMPL-PLAN gap ledger into the hub as
`gap` entities (doc-as-source, idempotent, schema-validated). Thin wrapper over
hub_core.materialize. ASCII."""
from django.core.management.base import BaseCommand

from hub import hub_app
from hub_core.materialize import materialize_gaps, materialize_proposed_adrs


class Command(BaseCommand):
    help = "Materialize REVIEW-AND-REIMPL-PLAN.md gap ledger -> gap entities (idempotent)."

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true")

    def handle(self, *a, dry_run=False, **o):
        doc = hub_app.PROJECT / "REVIEW-AND-REIMPL-PLAN.md"
        if not doc.exists():
            self.stderr.write("no REVIEW-AND-REIMPL-PLAN.md")
            return
        s = hub_app.store()
        ents = dict(hub_app.current_state(s)["entities"])
        reg = hub_app.registry(); head = hub_app._git_head(); text = doc.read_text(encoding="utf-8")
        g = materialize_gaps(s, reg, ents, hub_app.PROJECT_KEY, head, text, dry_run=dry_run)
        a = materialize_proposed_adrs(s, reg, ents, hub_app.PROJECT_KEY, head, text, dry_run=dry_run)
        for rej in g["rejects"] + a["rejects"]:
            self.stderr.write("  [REJECT] " + rej)
        self.stdout.write("hubmaterialize %s: gaps=%d proposed_adrs=%d skipped=%d rejected=%d" % (
            "(dry-run)" if dry_run else "DONE", g["gap"], a["adr"], g["skip"] + a["skip"], g["reject"] + a["reject"]))
