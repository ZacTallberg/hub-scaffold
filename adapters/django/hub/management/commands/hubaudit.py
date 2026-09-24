"""manage.py hubaudit — the project's computed hub gate (exit 0 PASS / 2 violation / 1 internal). ASCII."""
import sys

from django.core.management.base import BaseCommand

from hub import hub_app


class Command(BaseCommand):
    help = "Run the computed hub audit as an explicit structural-maintenance action."

    def add_arguments(self, p):
        p.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        try:
            r = hub_app.run_audit()
        except Exception as e:
            self.stderr.write("[FAIL] audit internal error (fail-closed): %s" % e)
            sys.exit(1)
        if opts.get("json"):
            import json
            self.stdout.write(json.dumps(r, indent=2))
        else:
            # The verdict AND its denominator: an all-clear that does not say how much it looked
            # at cannot be told apart from one that looked at nothing.
            ev = r.get("evaluated") or {}
            label = r.get("verdict") or ("PASS" if r["ok"] else "FAIL")
            if label == "PASS" and r["violations"]:
                label = "WARN"
            self.stdout.write("AUDIT: %s  exit=%s  critical=%s high=%s warn=%s  evaluated: %s "
                              "entities, %s of %s adapters" % (
                label, r["exit_code"], r["counts"]["critical"], r["counts"]["high"],
                r["counts"]["warn"], ev.get("entities", "?"), ev.get("adapters_ran", "?"),
                ev.get("adapters_requested", "?")))
            for v in r["violations"]:
                self.stdout.write("  [%s] %-22s %s" % (v["severity"].upper(), v["id"], v["observed"]))
        sys.exit(0 if r["exit_code"] in (0, 3) else r["exit_code"])
