"""The example app's settings: the kits run from ../kits (an adopter vendors them with
``tools/kits.py add`` instead), and one call applies the app-kit posture."""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
KITS = BASE_DIR.parent / "kits"
for kit in ("settings", "gate", "shell", "health", "assistant"):
    sys.path.insert(0, str(KITS / kit))

import os  # noqa: E402

import appkit_settings  # noqa: E402

os.environ.setdefault("APP_NAME", "Budget")
os.environ.setdefault("APP_SLUG", "budget-app")

appkit_settings.configure(
    globals(), BASE_DIR,
    app_module="budget",
    root_urlconf="config.urls",
    gate_table_prefix="budget_app",       # committed: an env key must never rename live tables
)

APP_SHELL = {
    "toasts": True,
    "palette": {"url_name": "budget:palette"},
    "live": {"url_name": "budget:events"},
    "drawer": True,
    "confirm": True,
    "transitions": True,
    "prefetch": True,
}

# The assistant's registry, and the canon families this app deliberately does not have.
ASSISTANT_TOOLS_MODULE = "budget.agent_tools"
from assistant import canon as _canon  # noqa: E402

_HAS = {"core_reads", "primitives"}
ASSISTANT_GAPS = {
    f.key: "the example app ships only the kit's core family; this one is left to a real app"
    for f in _canon.FAMILIES if f.key not in _HAS
}

# Errors reach the hub (patterns/error-visibility.md): ERROR+ from the request path and from
# background work is forwarded to POST <HUB_API_BASE>/api/app-error. A silent no-op without
# HUB_API_BASE, and fail-soft always.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
        "hub": {"()": "budget.errors.HubForwarder", "level": "ERROR"},
    },
    "loggers": {
        "django.request": {"handlers": ["console", "hub"], "level": "ERROR", "propagate": False},
        "budget": {"handlers": ["console", "hub"], "level": "INFO", "propagate": False},
    },
}
