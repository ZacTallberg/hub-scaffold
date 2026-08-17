"""Portable project identity for every standards-speaking edge.

The board, MCP discovery, public agent discovery, receipt predicates, and local worker scheme all
need to say WHICH instance they belong to. They read the same five fields here so two hubs running
side by side never present the same identity or route a launch click into the wrong fleet.

Resolution order, most specific first:

  1. ``PROJECT/project.json`` — the committed identity emitted by ``init.sh``.
  2. environment — the matching ``HUB_*`` values described in ``load()``.
  3. deterministic packaged defaults — key ``hub`` and a ``urn:hub:hub`` host.

A raw scaffold checkout still boots without a file so development tools remain usable. A file
that exists but is malformed raises rather than silently presenting another identity.
``PROJECT_IDENTITY_FILE`` overrides the exact file. ``HUB_PROJECT_DIR`` points monorepo or nested
adapters at their shared Project Plane while keeping this module independent of Django settings.
"""
import json
import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_KEY = "hub"
_KEY_SAFE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_NAME_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A scheme must survive being pasted into a URL and an OS protocol registration, so it is limited
# to what RFC 3986 allows in a scheme name.
_SCHEME_SAFE = re.compile(r"^[a-z][a-z0-9+.-]*$")
_CACHE = {"key": None, "value": None}


def path() -> Path:
    exact = os.environ.get("PROJECT_IDENTITY_FILE")
    if exact:
        return Path(exact)
    project_dir = os.environ.get("HUB_PROJECT_DIR")
    return (Path(project_dir) if project_dir else _ROOT / "PROJECT") / "project.json"


def _from_file(p):
    """Return the committed identity, or None when no identity file exists."""
    if not p.exists():
        return None
    ident = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(ident, dict):
        raise ValueError(f"{p} must contain a JSON object")
    return ident


def load() -> dict:
    """Return ``key/brand/app_name/app_host/worker_scheme`` for this instance.

    The result is cached on the identity file's mtime and relevant environment values, so an edit
    is picked up without a restart while an unchanged file costs only one stat.
    """
    p = path()
    try:
        stamp = p.stat().st_mtime_ns
    except OSError:
        stamp = None
    cache_key = (
        str(p), stamp,
        os.environ.get("HUB_PROJECT_KEY"), os.environ.get("HUB_BRAND"),
        os.environ.get("HUB_APP_NAME"), os.environ.get("HUB_PUBLIC_URL"),
        os.environ.get("HUB_WORKER_PROTOCOL"),
    )
    if _CACHE["key"] == cache_key:
        return _CACHE["value"]

    ident = dict(_from_file(p) or {})
    key = str(ident.get("key") or ident.get("project_key")
              or os.environ.get("HUB_PROJECT_KEY") or _DEFAULT_KEY).strip().lower()
    # An unsubstituted scaffold template is not an identity.
    if not key or key.startswith("{{"):
        key = _DEFAULT_KEY
    if not _KEY_SAFE.fullmatch(key):
        raise ValueError(f"{p}: key must match {_KEY_SAFE.pattern!r}")
    ident["key"] = key

    # Keep adoption lossless for projects that already carried the same meaning under an older
    # field name.  The canonical five are still what every consumer receives and what the next
    # committed edit should use.
    brand_value = str(ident.get("brand") or ident.get("display_name")
                      or os.environ.get("HUB_BRAND") or key.title()).strip()
    ident["brand"] = key.title() if not brand_value or brand_value.startswith("{{") else brand_value

    app_name = str(ident.get("app_name") or os.environ.get("HUB_APP_NAME") or key).strip()
    if not app_name or app_name.startswith("{{") or not _NAME_SAFE.fullmatch(app_name):
        app_name = key
    ident["app_name"] = app_name

    app_host = str(ident.get("app_host") or ident.get("live_url") or ident.get("public_url")
                   or os.environ.get("HUB_PUBLIC_URL")
                   or f"urn:hub:{key}").strip().rstrip("/")
    if not app_host or app_host.startswith("{{"):
        app_host = f"urn:hub:{key}"
    ident["app_host"] = app_host

    fallback_scheme = f"hub-{key}"
    scheme = str(ident.get("worker_scheme") or os.environ.get("HUB_WORKER_PROTOCOL")
                 or fallback_scheme).strip().lower()
    ident["worker_scheme"] = scheme if _SCHEME_SAFE.fullmatch(scheme) else fallback_scheme

    # Preserve extension fields but guarantee the portable five are always present.
    _CACHE["key"], _CACHE["value"] = cache_key, ident
    return ident


def key() -> str:
    return load()["key"]


def brand() -> str:
    return load()["brand"]


def worker_scheme() -> str:
    """The per-instance protocol the Launch Worker control opens (``hub-<key>://``)."""
    return load()["worker_scheme"]


def app_name() -> str:
    return load()["app_name"]


def app_host() -> str:
    return load()["app_host"]
