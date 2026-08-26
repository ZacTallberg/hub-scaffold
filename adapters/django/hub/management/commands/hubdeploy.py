"""manage.py hubdeploy — record one post-canary release closure from the deploy pipeline.

Called BY the deploy script after its front-door canary, never by hand: the point of the
record is that it carries what the live artifact reported back, not what someone believed
shipped. ``audit_ok`` is COMPUTED here by re-running the gate — a deploy cannot record a
green it did not earn — which is why this lives in a pipeline step and not in the HTTP
write path (the live mutation path must not rerun repository audit). The append itself
follows the served write seam's contract exactly: the record keys on its full SHA, the
canary-observed SHA must equal what was built, and every closed task must exist and be done.
ASCII.
"""
import datetime
import re
import sys

from django.core.management.base import BaseCommand

from hub import hub_app
from hub_core import ids, validate


class Command(BaseCommand):
    help = "Append a post-canary deploy record (sha, served sha, computed audit result) to the hub event log."

    def add_arguments(self, p):
        p.add_argument("--sha", required=True, help="the hex sha that was built and shipped")
        p.add_argument("--served", required=True,
                       help="the sha the independent front-door canary observed; must equal --sha")
        p.add_argument("--method", default="pipeline", help="how this release shipped")
        p.add_argument("--tasks", default="", help="comma-separated task ids/locals this release closes")

    def handle(self, *a, sha, served, method, tasks, **o):
        sha = str(sha).strip().lower()
        served = str(served).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
            self.stderr.write("hubdeploy: --sha must be a 7..64 character hex identity")
            sys.exit(1)
        if served != sha:
            self.stderr.write("hubdeploy: served sha %r does not equal built sha %r — the "
                              "record is refused, not written optimistically; fix the canary "
                              "or the deploy before recording the release" % (served, sha))
            sys.exit(1)

        st = hub_app.store()
        try:
            state = hub_app.current_state()
            entities = state.get("entities", {})
            pk = hub_app.PROJECT_KEY

            closed = []
            for raw in (t.strip() for t in tasks.split(",") if t.strip()):
                closed.append(raw if ":" in raw else ids.make_id(pk, "task", raw.lower()))
            problems = []
            for tid in closed:
                ent = entities.get(tid)
                if not ent or ent.get("type") != "task":
                    problems.append("%s is not a task on this board" % tid)
                elif ent.get("status") != "done":
                    problems.append("%s is not done" % tid)
            if problems:
                self.stderr.write("hubdeploy: refused — " + "; ".join(problems))
                sys.exit(1)
            if len(closed) != len(set(closed)):
                self.stderr.write("hubdeploy: refused — duplicate ids in --tasks")
                sys.exit(1)

            # COMPUTED, never attested: the gate runs here, against this ledger, right now.
            try:
                result = hub_app.run_audit(st, served=served)
            except Exception as e:
                self.stderr.write("hubdeploy: audit internal error (fail-closed): %s" % e)
                sys.exit(1)
            audit_ok = not [v for v in result.get("violations", [])
                            if v.get("severity") in ("critical", "high")]

            eid = ids.make_id(pk, "deploy", sha)
            payload = {
                "type": "deploy",
                "sha": sha,
                "served_sha": sha,
                "build": "build-%s" % sha,
                "method": str(method),
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "audit_ok": audit_ok,
                "tasks_closed": sorted(closed),
            }
            errs = validate({**payload, "id": eid, "version": 0}, "deploy", hub_app.registry())
            if errs:
                self.stderr.write("hubdeploy: REJECT %s: %s" % (eid, errs))
                sys.exit(1)

            if eid in entities:
                self.stdout.write("hubdeploy: %s already recorded (a deploy record is immutable)" % eid)
                return

            hub_app.publish_event(st.append(
                aggregate=eid, type="deploy.created", payload=payload,
                expected_version=0, agent_id="deploy", git_sha=sha,
                idem_key="deploy:%s" % eid))
        finally:
            st.close()
        self.stdout.write(self.style.SUCCESS(
            "hubdeploy: recorded %s audit_ok=%s served=%s tasks_closed=%d"
            % (eid, audit_ok, served, len(closed))))
