"""Minimal, born-safe Django settings for the hub example site.

Security posture is fail-closed: SECRET_KEY is REQUIRED in prod (no committed literal — the hub
audit's AST gate enforces this), ephemeral only under DEBUG; ALLOWED_HOSTS never defaults to '*';
general Hub mutations are token-gated via the HUB_WRITE_TOKEN environment variable. Reads are
authenticated by default; only the DEBUG preview declares them public, and the optional launch mint
uses its narrow CSRF gate. This file is also what
`manage.py hubaudit` AST-scans, so it doubles as the reference shape for a mounted project.
"""
import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# The example imports hub_core from the scaffold parent, so point portable identity at this
# example's own PROJECT plane rather than the template file in the parent checkout.
os.environ.setdefault("PROJECT_IDENTITY_FILE", str(BASE_DIR / "PROJECT" / "project.json"))

DEBUG = os.environ.get("DEBUG", "") == "1"

# SECRET_KEY: required in prod (NO literal fallback). In DEBUG, mint an ephemeral per-process key.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-ephemeral-" + secrets.token_urlsafe(32)
    else:
        raise RuntimeError("SECRET_KEY must be set in production (no insecure default).")

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
_extra = os.environ.get("ALLOWED_HOSTS", "")
if _extra:
    ALLOWED_HOSTS += [h.strip() for h in _extra.split(",") if h.strip()]

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Opt-in HTTPS redirect for a site whose TLS terminates at a proxy. The LIVENESS path is
    # exempt: a host-local probe (a rolling restart's health wait, an ops check) calls
    # http://127.0.0.1:<port>/... directly, with no proxy and no X-Forwarded-Proto, and a
    # redirect sends it to an https URL the app server cannot serve — every restart then
    # reads as unhealthy and rolls back. Matched with .search against the path without its
    # leading slash, so a mount prefix in front of /hub/ does not defeat it.
    if os.environ.get("HUB_SSL_REDIRECT", "") == "1":
        SECURE_SSL_REDIRECT = True
        SECURE_REDIRECT_EXEMPT = [r"(^|/)hub/cursor\.json$"]

INSTALLED_APPS = [
    "hub",  # the agent-operable /hub surface (event-sourced; renders from hub_core; token-gated writes)
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # A busy ledger answers 503 + Retry-After on every hub path, reads included — never a 500.
    # Per-route latency (worst process window, last hour) for /hub/perf.json and the rail.
    "hub.middleware.RouteTimingMiddleware",
    "hub.middleware.LedgerBusyMiddleware",
    "hub.middleware.NoStoreHTMLMiddleware",
    # The visibility veil: a no-op until PROJECT/facets.json declares hidden facets.
    "hub.veil.VeilMiddleware",
]

ROOT_URLCONF = "example_site.urls"
TEMPLATES = []
ASGI_APPLICATION = "example_site.asgi.application"
WSGI_APPLICATION = "example_site.wsgi.application"

# The hub itself has no relational models; this DB exists so `migrate` and any host apps work.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("HUB_TEST_DB", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 20},
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- hub configuration (see adapters/django/MOUNTING.md) ---
HUB_PROJECT_KEY = "example"        # entity-id prefix -> example:task:0001
HUB_BRAND = "Example"              # navbar reads "Example · Hub"
HUB_BUILD_STAMP = "build_sha.txt"  # BASE_DIR-relative build-identity stamp (written by the build)
HUB_DONE_STRICTNESS = os.environ.get("HUB_DONE_STRICTNESS", "tracked")  # evidence dial

# Commits from OTHER projects count as evidence and lineage for tasks about them: map each project
# slug to a local checkout (read with plain git). A project with no checkout can instead be asked
# through HUB_COMMIT_RESOLVER = "package.module:function" (function(project, sha) -> True/False/None).
HUB_PROJECT_REPOS = json.loads(os.environ.get("HUB_PROJECT_REPOS", "") or "{}")

# How long a console the Hub can PROVE is gone keeps what it held (task leases and item claims)
# before they are released, measured from its last-seen stamp. Default 30 minutes.
HUB_GONE_GRACE_S = int(os.environ.get("HUB_GONE_GRACE_S", "1800") or 1800)

# The interpreter line every seat must run ("major.minor"), graded on /hub/distribution.json.
# Empty = not graded.
HUB_REQUIRED_PYTHON = os.environ.get("HUB_REQUIRED_PYTHON", "")

# Reads are authenticated by default (a Django user, a scoped agent credential, or the shared
# root token). This example mounts no sign-in, so its local preview declares the board public
# — only under DEBUG. Outside DEBUG the example refuses anonymous reads, and a real adopter
# should add a sign-in rather than copy the public setting (hubaudit flags it in production).
HUB_READ_AUTH = "public" if DEBUG else "required"

# Optional local-worker bridge. Adopters enable it only after wiring their own launch protocol.
HUB_WORKER_LAUNCH_ENABLED = False
HUB_WORKER_PROTOCOL = "hub-example"
HUB_WORKER_LAUNCH_ISSUER_URL = "https://example.invalid/hub/api/launch-grant/consume"
HUB_WORKER_GRANT_TTL_S = 120

# Shared-root migration credential. Normal workers use short-lived scoped X-Agent-Token
# credentials issued through /hub/api/agent-credential. Disable this compatibility path after the
# fleet has migrated; reads follow HUB_READ_AUTH above and the narrow launch mint remains CSRF-gated.
HUB_WRITE_TOKEN = os.environ.get("HUB_WRITE_TOKEN", "")
HUB_SHARED_TOKEN_COMPAT = os.environ.get("HUB_SHARED_TOKEN_COMPAT", "true").lower() == "true"

# The host app's own 5xx reach the hub's operational stream (the "Host app 5xx" channel on
# the board's coverage strip). The handler writes to the same runtime directory the hub uses.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"hub_errors": {"()": "hub_core.errorlog.HubErrorHandler",
                                "hub_dir": os.environ.get("HUB_DIR") or str(BASE_DIR / "PROJECT" / ".hub"),
                                "level": "ERROR"}},
    "loggers": {"django.request": {"handlers": ["hub_errors"], "level": "ERROR",
                                   "propagate": True}},
}

# Console chat histories are OFF by default: turn them on only after the people whose consoles
# upload have been told the operator can read them. The browser viewer check is an adopter
# callable; "hub.viewers.debug_loopback" answers yes only under DEBUG from 127.0.0.1.
HUB_HISTORIES_ENABLED = os.environ.get("HUB_HISTORIES_ENABLED", "").lower() == "true"
HUB_HISTORY_VIEWER = os.environ.get("HUB_HISTORY_VIEWER") or None

# The shared app banner (hub_core/components/banner) and the person's preferences it keeps at
# /hub/api/profile. HUB_PERSON names the adopter's "who is this browser" callable; the example
# has no sign-in, so it uses the developer-only resolver (DEBUG + loopback, as HUB_DEBUG_PERSON).
HUB_PERSON = os.environ.get("HUB_PERSON") or ("hub.viewers.debug_person" if DEBUG else None)
HUB_DEBUG_PERSON = os.environ.get("HUB_DEBUG_PERSON", "alice")
# The app directory the banner's app drawer resolves slugs against, and the grants seam.
HUB_APPS = [
    {"slug": "budget-app", "name": "Budget", "url": "/demo/budget-app/"},
    {"slug": "reporting", "name": "Reporting", "url": "/demo/reporting/"},
    {"slug": "timesheets", "name": "Timesheets", "url": ""},
]
HUB_REACH = "example_site.demo.reach"
