"""Minimal, born-safe Django settings for the hub example site.

Security posture is fail-closed: SECRET_KEY is REQUIRED in prod (no committed literal — the hub
audit's AST gate enforces this), ephemeral only under DEBUG; ALLOWED_HOSTS never defaults to '*';
hub writes are token-gated via the HUB_WRITE_TOKEN environment variable. This file is also what
`manage.py hubaudit` AST-scans, so it doubles as the reference shape for a mounted project.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

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

INSTALLED_APPS = [
    "hub",  # the agent-operable /hub surface (event-sourced; renders from hub_core; token-gated writes)
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "hub.middleware.NoStoreHTMLMiddleware",
]

ROOT_URLCONF = "example_site.urls"
TEMPLATES = []
WSGI_APPLICATION = "example_site.wsgi.application"

# The hub itself has no relational models; this DB exists so `migrate` and any host apps work.
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(BASE_DIR / "db.sqlite3"),
                "OPTIONS": {"timeout": 20}},
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- hub configuration (see adapters/django/MOUNTING.md) ---
HUB_PROJECT_KEY = "example"        # entity-id prefix -> example:task:0001
HUB_BRAND = "Example"              # navbar reads "Example · Hub"
HUB_BUILD_STAMP = "build_sha.txt"  # BASE_DIR-relative build-identity stamp (written by the build)

# Token for hub writes (X-Write-Token). Public reads, token-gated writes. Fail-closed when unset.
HUB_WRITE_TOKEN = os.environ.get("HUB_WRITE_TOKEN", "")
