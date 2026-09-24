"""manage.py hubbackground -- the HUB_ROLE=background process.

Runs the projections a request process should not pay for on its own clock: folds the ledger,
materializes the delivery (git ancestry) projection into a sidecar the web processes read, wakes
the live streams when that projection changes, re-arms the lease truth timers, and writes its own
clock (``background.json``) after every tick so web processes can tell a live backgrounder from a
dead one. Pair it with web processes started with ``HUB_ROLE=web``; see ``hub/roles.py``.

    HUB_ROLE=background python manage.py hubbackground            # every 20 s, forever
    python manage.py hubbackground --once                         # one tick, then exit
"""
import os
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the hub's background projections on their own clock (HUB_ROLE=background)."

    def add_arguments(self, p):
        p.add_argument("--interval", type=float,
                       default=float(os.environ.get("HUB_BACKGROUND_INTERVAL_S") or 20))
        p.add_argument("--once", action="store_true")

    def handle(self, *a, interval=20.0, once=False, **o):
        os.environ.setdefault("HUB_ROLE", "background")
        from hub import background
        while True:
            report = background.tick()
            self.stdout.write("hubbackground tick %s" % " ".join(
                "%s=%s" % (k, report[k]) for k in sorted(report)))
            self.stdout.flush()
            if once:
                return
            time.sleep(max(1.0, interval))
