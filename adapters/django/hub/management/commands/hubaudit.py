"""manage.py hubaudit — the project's computed hub gate (exit 0 PASS / 2 violation / 1 internal). ASCII."""
import sys

from django.core.management.base import BaseCommand

from hub import hub_app


class Command(BaseCommand):
    help = "Run the computed hub audit (CI/pre-deploy gate)."

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
            self.stdout.write("AUDIT: %s  exit=%s  critical=%s high=%s warn=%s" % (
                "PASS" if r["ok"] and not r["violations"] else ("WARN" if r["ok"] else "FAIL"),
                r["exit_code"], r["counts"]["critical"], r["counts"]["high"], r["counts"]["warn"]))
            for v in r["violations"]:
                self.stdout.write("  [%s] %-22s %s" % (v["severity"].upper(), v["id"], v["observed"]))
        sys.exit(0 if r["exit_code"] in (0, 3) else r["exit_code"])
