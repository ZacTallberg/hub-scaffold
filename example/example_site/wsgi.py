"""WSGI entrypoint for the hub example site. Mirrors manage.py's sys.path bootstrap so the
scaffold's `hub_core` and the `hub` adapter import in place."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCAFFOLD = BASE_DIR.parent
for _p in (str(SCAFFOLD), str(SCAFFOLD / "adapters" / "django")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example_site.settings")

application = get_wsgi_application()

# Warm the ledger fold and the board snapshot once, on a daemon thread, in the SERVED process only
# (never in tests or management commands). Requests are not blocked; HUB_PREWARM=0 disables it.
from hub import prewarm  # noqa: E402

prewarm.start()
