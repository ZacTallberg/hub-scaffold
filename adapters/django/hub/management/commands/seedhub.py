"""manage.py seedhub — genesis import of the reconciled board (tasks + ADRs) into the hub event log.

This is the ONE sanctioned hand-authored entry point: the project's birth state, taken from the
reconciled plan. After this, the board only changes through the discover->claim->implement->
record->verify loop (typed events), never by hand. Idempotent: an id that already exists is skipped,
so re-running is safe. Direct-store append (the genesis path) — the HTTP done-guard is bypassed only
for already-RESOLVED decision tasks, which must carry their own evidence (verified_by).
"""
import json

from django.core.management.base import BaseCommand

from hub import hub_app
from hub_core import ids, validate


class Command(BaseCommand):
    help = "Seed the hub from PROJECT/seed.json (adrs + tasks). Idempotent genesis import."

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true", help="validate only; append nothing")

    def handle(self, *a, dry_run=False, **o):
        path = hub_app.PROJECT / "seed.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        s = hub_app.store()
        state = hub_app.current_state(s)
        existing = set(state["entities"].keys())
        reg = hub_app.registry()
        head = hub_app._git_head()
        n = {"adr": 0, "task": 0, "note": 0, "skip": 0, "reject": 0}
        pk = hub_app.PROJECT_KEY

        def emit(type_, ref, payload):
            eid = ids.make_id(pk, type_, str(ref).lower())
            if eid in existing:
                n["skip"] += 1
                return
            ent = {**payload, "id": eid, "type": type_, "version": 1}
            errs = validate(ent, type_, reg)
            if errs:
                n["reject"] += 1
                self.stderr.write(f"  REJECT {eid}: {errs}")
                return
            if not dry_run:
                s.append(aggregate=eid, type=f"{type_}.created",
                         payload={**payload, "type": type_}, expected_version=None,
                         agent_id="seed", git_sha=head, idem_key=f"seed:{eid}")
            existing.add(eid)
            n[type_] += 1

        for adr in data.get("adrs", []):
            a2 = dict(adr)
            emit("adr", a2.pop("ref"), a2)

        for note in data.get("notes", []):
            nn = dict(note)
            emit("note", nn.pop("ref"), nn)

        for t in data.get("tasks", []):
            t2 = dict(t)
            ref = t2.pop("ref")
            deps = [ids.make_id(pk, "task", str(d).lower()) for d in t2.pop("deps", [])]
            decided = [ids.make_id(pk, "adr", str(d).lower()) for d in t2.pop("decided_by", [])]
            if deps:
                t2["deps"] = deps
            if decided:
                t2["decided_by"] = decided
            t2["legacy_ref"] = ref
            emit("task", ref, t2)

        self.stdout.write(self.style.SUCCESS(
            f"seedhub: +{n['adr']} adr, +{n['task']} task, +{n['note']} note, {n['skip']} skip, {n['reject']} reject"
            + ("  (dry-run)" if dry_run else "")))
        if n["reject"]:
            raise SystemExit(1)
