"""manage.py hubimport — ingest CAPABILITY-LEDGER.md into the hub event log as LIVE cap entities
(the capability-fabric pattern: a markdown ledger becomes a queryable, machine-readable registry).
Idempotent; validates before append."""
import hashlib
import re

from django.core.management.base import BaseCommand

from hub import hub_app
from hub_core import ids, validate

_MAT = {"✅": "reusable", "🟡": "prototype", "🔬": "prototype", "🧩": "prototype", "📐": "concept"}


def _slug(name):
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]) or "cap"


class Command(BaseCommand):
    help = "Ingest CAPABILITY-LEDGER.md -> cap entities (idempotent). The capability-fabric pattern."

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true")

    def handle(self, *a, dry_run=False, **o):
        ledger = hub_app.BASE_DIR / "CAPABILITY-LEDGER.md"
        if not ledger.exists():
            self.stderr.write("no CAPABILITY-LEDGER.md found")
            return
        s = hub_app.store()
        ents = dict(hub_app.current_state(s)["entities"])
        reg = hub_app.registry()
        head = hub_app._git_head()
        seen = {e.get("legacy_ref") for e in ents.values() if e.get("legacy_ref")}
        made = {"cap": 0, "skip": 0, "reject": 0}
        for raw in ledger.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("| **"):  # only bold-name capability rows
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            m = re.match(r"\*\*(.+?)\*\*", cells[0])
            name = (m.group(1) if m else cells[0])[:120]
            where, mat_cell = cells[1], cells[2]
            mat = next((v for g, v in _MAT.items() if g in mat_cell), "concept")
            pivot = cells[3] if len(cells) > 3 else ""
            needs = cells[4] if len(cells) > 4 else ""
            legacy = "cap:" + _slug(name)
            if legacy in seen:
                made["skip"] += 1
                continue
            local = _slug(name)
            nid = ids.make_id(hub_app.PROJECT_KEY, "cap", local)
            if nid in ents:
                local = local + "-" + hashlib.sha256(cells[0].encode()).hexdigest()[:4]
                nid = ids.make_id(hub_app.PROJECT_KEY, "cap", local)
            payload = {"id": nid, "type": "cap", "name": name, "maturity": mat,
                       "pivot_notes": ("[%s] %s" % (where, pivot))[:600], "needs": needs[:500],
                       "legacy_ref": legacy, "version": 1}
            errs = validate(payload, "cap", reg)
            if errs:
                made["reject"] += 1
                self.stderr.write("  [REJECT] %s: %s" % (name, errs[0]))
                continue
            if not dry_run:
                s.append(aggregate=nid, type="capability.registered", payload=payload, expected_version=0,
                         idem_key="import:" + legacy, agent_id="hubimport", git_sha=head)
            ents[nid] = payload
            seen.add(legacy)
            made["cap"] += 1
        self.stdout.write("hubimport %s: caps=%d skipped=%d rejected=%d" % (
            "(dry-run)" if dry_run else "DONE", made["cap"], made["skip"], made["reject"]))
