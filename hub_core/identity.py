"""Project identity — key, brand, and the per-instance worker URL scheme.

The standards-speaking edges (the MCP server's serverInfo, the A2A agent card's name, the
`<key>-worker://` launch scheme) all need to say WHICH instance they are. They read it from
here so the answer is derived in one place: two hubs running side by side on one workstation
must never present the same identity, or one board's launch click routes into the other's fleet.

Resolution order, most specific first:

  1. ``PROJECT/project.json``  — a committed file, when the instance wants identity in the repo.
  2. environment             — ``HUB_PROJECT_KEY`` / ``HUB_BRAND``.
  3. the packaged default    — key ``hub``.

There is deliberately no *required* config file: the scaffold must boot on a fresh clone with
nothing edited, because an adopter's first run is `init.sh` and then the example app. An instance
that wants a hard identity writes project.json and gets a loud error if it is malformed — but a
missing file is a default, not a crash.

``PROJECT_IDENTITY_FILE`` overrides the path (fixtures point it at a scratch identity).
"""
import json
import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_KEY = "hub"
# A scheme must survive being pasted into a URL and an OS protocol registration, so it is limited
# to what RFC 3986 allows in a scheme name. A key that cannot make one falls back rather than
# minting `my project://` and failing at the browser instead of here.
_SCHEME_SAFE = re.compile(r"^[a-z][a-z0-9+.-]*$")
_CACHE = {"key": None, "value": None}


def path() -> Path:
    return Path(os.environ.get("PROJECT_IDENTITY_FILE") or _ROOT / "PROJECT" / "project.json")


def _from_file(p):
    """The committed identity, or None when the instance keeps identity in settings/env.

    A malformed file RAISES: it was written on purpose, so silently falling back to the default
    would let a board serve somebody else's identity from a typo.
    """
    if not p.exists():
        return None
    ident = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(ident, dict):
        raise ValueError(f"{p} must contain a JSON object")
    return ident


def load() -> dict:
    """{key, brand, worker_scheme} for this instance. Cached on the identity file's mtime, so an
    edit is picked up without a restart and an unchanged file costs one stat."""
    p = path()
    try:
        stamp = p.stat().st_mtime_ns
    except OSError:
        stamp = None  # absorbs: no identity file — env/default path, nothing to invalidate on
    cache_key = (str(p), stamp, os.environ.get("HUB_PROJECT_KEY"), os.environ.get("HUB_BRAND"))
    if _CACHE["key"] == cache_key:
        return _CACHE["value"]

    ident = dict(_from_file(p) or {})
    key = str(ident.get("key") or os.environ.get("HUB_PROJECT_KEY") or _DEFAULT_KEY).strip()
    # A settings placeholder that init.sh never substituted is not an identity. Left alone it
    # would reach an MCP serverInfo and an agent card as the literal template token.
    if not key or key.startswith("{{"):
        key = _DEFAULT_KEY
    ident["key"] = key
    ident["brand"] = str(ident.get("brand") or os.environ.get("HUB_BRAND") or key.title()).strip()
    scheme = str(ident.get("worker_scheme") or f"{key}-worker").lower()
    ident["worker_scheme"] = scheme if _SCHEME_SAFE.match(scheme) else f"{_DEFAULT_KEY}-worker"

    _CACHE["key"], _CACHE["value"] = cache_key, ident
    return ident


def key() -> str:
    return load()["key"]


def brand() -> str:
    return load()["brand"]


def worker_scheme() -> str:
    """The per-instance protocol the Launch Worker control opens (`<key>-worker://`)."""
    return load()["worker_scheme"]
