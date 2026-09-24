"""`manage.py errors` -- read what is broken without a browser or a log path.

The verb an AGENT reaches for: it prints the rows /errors/ shows, from anywhere the app's
database is reachable, plus whether forwarding to the hub is armed in this process.

    python manage.py errors                    # open failures, newest first
    python manage.py errors --since 2h         # only what broke recently
    python manage.py errors --kind server      # one kind
    python manage.py errors --id 14 --trace    # one row with its full stack
    python manage.py errors --json             # machine-readable
    python manage.py errors --resolve 14       # close it (a recurrence re-opens)

Dark, or quiet? `python manage.py error_selftest` fires every producer for real.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from app_errors import capture


def _parse_since(text: str) -> timedelta | None:
    m = re.fullmatch(r"(\d+)\s*([mhd])", (text or "").strip(), re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


class Command(BaseCommand):
    help = "Show captured failures (browser, server, background, data, agent) -- newest first."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="window like 30m, 2h, 7d")
        parser.add_argument("--kind", help="js|promise|http|stream|server|background|django|data|other|agent")
        parser.add_argument("--id", type=int, help="show one row in full")
        parser.add_argument("--trace", action="store_true", help="include stacks")
        parser.add_argument("--all", action="store_true", help="include resolved rows")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--resolve", type=int, metavar="ID")
        parser.add_argument("--limit", type=int, default=40)

    def handle(self, *a, **o):
        Model = capture._model()
        if o.get("resolve"):
            row = Model.objects.filter(pk=o["resolve"]).first()
            if not row:
                self.stderr.write(f"no error #{o['resolve']}")
                return
            row.resolved_at = timezone.now()
            row.resolved_by = "cli"
            row.save(update_fields=["resolved_at", "resolved_by"])
            self.stdout.write(f"closed #{row.pk}. A recurrence will re-open it.")
            return

        qs = Model.objects.all()
        if o.get("id"):
            qs = qs.filter(pk=o["id"])
        else:
            if not o.get("all"):
                qs = qs.filter(resolved_at__isnull=True)
            if o.get("kind"):
                qs = qs.filter(kind=o["kind"])
            delta = _parse_since(o.get("since") or "")
            if delta:
                qs = qs.filter(last_seen__gte=timezone.now() - delta)
        rows = list(qs[: o["limit"]])
        fwd = capture.forwarding_status()

        if o.get("json"):
            self.stdout.write(json.dumps({"forwarding": fwd, "errors": [{
                "id": r.pk, "kind": r.kind, "severity": r.severity, "count": r.count,
                "message": r.message, "source": r.source, "page_url": r.page_url,
                "status": r.status, "first_seen": r.first_seen.isoformat(),
                "last_seen": r.last_seen.isoformat(), "open": r.is_open,
                "stack": r.stack if (o.get("trace") or o.get("id")) else "",
            } for r in rows]}, indent=2))
            return

        self.stdout.write("forwarding: %s" % (
            "armed as %s" % fwd["slug"] if fwd["armed"] else
            "NOT armed (%s missing) -- failures stay local" % ", ".join(
                n for n, ok in (("HUB_API_BASE", fwd["url"]), ("HUB_AGENT_TOKEN", fwd["token"]),
                                ("APP_SLUG", fwd["slug"])) if not ok)))
        if not rows:
            scope = [] if o.get("all") else ["open"]
            if o.get("kind"):
                scope.append(o["kind"])
            if o.get("since"):
                scope.append("in the last " + o["since"])
            total = Model.objects.count()
            self.stdout.write(
                f"No {' '.join(scope)} failures. ({total} row(s) recorded all-time -- the "
                f"table is wired and empty; run error_selftest to prove it is not dark.)")
            return

        total_open = Model.objects.filter(resolved_at__isnull=True).count()
        self.stdout.write(f"{len(rows)} shown of {total_open} open ({Model.objects.count()} all-time)\n")
        for r in rows:
            mins = int((timezone.now() - r.last_seen).total_seconds() // 60)
            when = f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago"
            flag = "" if r.is_open else "  [resolved]"
            self.stdout.write(f"#{r.pk:<4} {r.severity:<8} {r.kind:<10} x{r.count:<4} {when:<9}{flag}")
            self.stdout.write(f"      {r.message[:160]}")
            if r.source:
                self.stdout.write(f"      at {r.source}")
            if r.page_url:
                self.stdout.write(f"      on {r.page_url}")
            if o.get("trace") or o.get("id"):
                for line in (r.stack or "").splitlines():
                    self.stdout.write(f"        {line}")
            self.stdout.write("")
