"""ASGI entrypoint for the hub example site's long-lived realtime delivery.

Mirrors ``manage.py`` and ``wsgi.py`` so the scaffold's ``hub_core`` and Django
adapter import in place. Point an ASGI server at ``example_site.asgi:application``.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCAFFOLD = BASE_DIR.parent
for _p in (str(SCAFFOLD), str(SCAFFOLD / "adapters" / "django")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example_site.settings")

application = get_asgi_application()

# Warm the ledger fold and the board snapshot once, on a daemon thread, in the SERVED process only
# (never in tests or management commands). Requests are not blocked; HUB_PREWARM=0 disables it.
from hub import prewarm  # noqa: E402

prewarm.start()
