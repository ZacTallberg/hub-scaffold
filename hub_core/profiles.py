"""ONE PERSON'S PRESENTATION PREFERENCES, shared by every app around this hub. Framework-free;
the Django adapter serves it at /hub/api/profile (token-gated, server-to-server).

Someone who picks a larger UI, a dark theme, or where the agent button sits in one app has
picked it in all of them, and the mark they choose (their initials or a small picture) is the
mark every app draws. That is not an app's setting and not one browser's setting, so it lives
here, once per person.

THE PATH. A browser never reaches this. An app's SERVER, holding its own hub credential, asks
on behalf of a person it has ALREADY signed in, and serves the answer from its own gated
endpoint. The hub authenticates the app; the app vouches for the person.

NOT IN THE LEDGER, deliberately. The ledger is the append-only record of what the project
did; a person trying four text sizes until one feels right is not project history. This is
mutable per-person state, stored beside the other sidecars in HUB_DIR/profiles.json.

EVERY KEY IS A CLOSED SET, and a value outside it is DROPPED and reported, never clamped to
a neighbour: a browser sending ui=400 has a bug, and honouring it as "the biggest we allow"
hides that bug behind a page nobody can read.

THE MARK HAS TWO KINDS. {"kind": "initials"} or {"kind": "image", "image": "data:image/...;
base64,..."}. The picture never arrives as a file: the browser crops it square, scales it
small and exports a data URL, so there is no upload endpoint, no multipart parsing and no
file store. PNG, JPEG and WebP only -- SVG is refused because it can carry script, and this
string is handed to an <img> on every app.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from . import atomic

STORE = "profiles.json"
_LOCK = threading.Lock()

#: A person as the app names them: its own username for someone it signed in.
PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")

#: Every key a caller may set, with what it must be.
ALLOWED = {
    "kind": {"initials", "image"},
    "theme": {"", "light", "dark"},          # "" = not chosen; the app's own default stands
    "motion": {"full", "reduce"},
    "text": {90, 100, 110, 125},             # reading size, percent
    "ui": {92, 100, 110},                    # control density/scale, percent
    "agent": {"float", "sidebar", "header"},  # where the agent button sits
}
DEFAULTS = {"kind": "initials", "image": "", "theme": "", "motion": "full",
            "text": 100, "ui": 100, "agent": "float"}

#: The encoded picture cap: comfortably above a small square, far below a store-bloating blob.
MARK_MAX_CHARS = 96_000
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


def _path(hub_dir) -> Path:
    return Path(hub_dir) / STORE


def _load(hub_dir) -> dict:
    try:
        body = json.loads(_path(hub_dir).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A corrupt store must not take every app's banner down at once: it reads as
        # "nobody has set anything", and the next write repairs it.
        return {}
    return body if isinstance(body, dict) else {}


def clean(raw) -> tuple[dict, list]:
    """(accepted subset, refused keys). Unknown keys and out-of-range values are refused."""
    out, refused = {}, []
    if not isinstance(raw, dict):
        return out, ["(body is not an object)"]
    for key, value in raw.items():
        if key == "image":
            if value == "":
                out["image"] = ""
            elif (isinstance(value, str) and len(value) <= MARK_MAX_CHARS
                  and _DATA_URL_RE.match(value)):
                out["image"] = value
            else:
                refused.append("image")
            continue
        allowed = ALLOWED.get(key)
        if allowed is None:
            refused.append(str(key))
            continue
        if key in ("text", "ui"):
            # A STRING or an int, but never a bool (True == 1 would slip through).
            if isinstance(value, bool):
                refused.append(key)
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                refused.append(key)
                continue
        if value in allowed:
            out[key] = value
        else:
            refused.append(key)
    return out, refused


def valid_person(person: str) -> bool:
    return bool(PERSON_RE.fullmatch(person or ""))


def get(hub_dir, person: str) -> dict:
    prefs = dict(DEFAULTS)
    stored, _ = clean(_load(hub_dir).get(person) or {})
    prefs.update(stored)
    return prefs


def update(hub_dir, person: str, raw) -> tuple[dict, list]:
    """Merge the accepted subset of `raw` onto what the person had. A mark that says "draw my
    picture" with no picture is put back to initials rather than stored: it would render as a
    broken image on every app."""
    incoming, refused = clean(raw)
    with _LOCK:
        store = _load(hub_dir)
        merged = dict(DEFAULTS)
        stored, _ = clean(store.get(person) or {})
        merged.update(stored)
        merged.update(incoming)
        if merged.get("kind") == "image" and not merged.get("image"):
            merged["kind"] = "initials"
        store[person] = merged
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        atomic.write_json(_path(hub_dir), store)
    return merged, refused
