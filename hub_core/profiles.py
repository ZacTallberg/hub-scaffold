"""ONE PERSON'S PRESENTATION PREFERENCES, shared by every app around this hub. Framework-free;
the Django adapter serves it at /hub/api/profile.

Someone who picks a larger UI, a dark theme, a reading face or where the agent button sits in
one app has picked it in all of them, and the mark they choose (their initials or a small
picture) is the mark every app draws. That is not an app's setting and not one browser's
setting, so it lives here, once per person.

TWO PATHS, ONE ROUTE. An app's SERVER, holding its own hub credential, asks on behalf of a
person it has ALREADY signed in (?person=, profile:read / profile:write); the hub authenticates
the app, the app vouches for the person. And when the hub is mounted inside the site that signs
people in, the person's own same-origin browser may reach the same route, named by the
adopter's HUB_PERSON resolver and never by the request (adapters/django/hub/app_services.py).

NOT IN THE LEDGER, deliberately. The ledger is the append-only record of what the project
did; a person trying four text sizes until one feels right is not project history. This is
mutable per-person state, stored beside the other sidecars in HUB_DIR/profiles.json.

EVERY KEY IS A CLOSED SET, and a value outside it is DROPPED and reported, never clamped to
a neighbour: a browser sending ui=400 has a bug, and honouring it as "the biggest we allow"
hides that bug behind a page nobody can read.

PER-APP OVERRIDES. `apps` holds {slug: {key: value}} validated through the SAME table and
restricted to APP_SCOPED (how a page reads, and where its agent button sits). The mark and the
starred apps are the person, not the page, and cannot differ per app. An EMPTY override for an
app is a removal ("put this app back on my everywhere-value"), read from what was SENT rather
than from the merged result -- merging {} changes nothing, so testing the merge could never
fire. A single key sent as null puts just that key back. resolve() is the one definition of
"what this app should look like for this person"; the banner component carries the same rule
for a record it already holds, and APP_SCOPED is the pair to change together.

STARRED is a list that REPLACES (unstarring is the absence of a slug, so a merge could never
remove one): strings only, de-duplicated, the person's order kept, capped.

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
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Every key a caller may set, with what it must be.
ALLOWED = {
    "kind": {"initials", "image"},
    "theme": {"", "light", "dark"},          # "" = not chosen; the app's own default stands
    "motion": {"full", "reduce"},
    "text": {90, 100, 110, 125},             # reading size, percent
    "ui": {92, 100, 110, 125},               # control density/scale (zoom), percent
    "agent": {"float", "sidebar", "header"},  # where the agent button sits
    "font": {"", "system", "sans", "serif", "mono", "readable"},   # "" = the app's own face
    "nav_open": {"", "on", "off"},           # the banner sidebar's open/rail state
    "tips": {"on", "off"},                   # hover info bubbles
    "help": {"on", "off"},                   # help buttons: the tour, the read-me
}
#: The keys a person may set differently for ONE app. The mark and the stars are the person.
APP_SCOPED = frozenset({"theme", "motion", "text", "ui", "agent", "font", "nav_open",
                        "tips", "help"})
DEFAULTS = {"kind": "initials", "image": "", "theme": "", "motion": "full",
            "text": 100, "ui": 100, "agent": "float", "font": "", "nav_open": "",
            "tips": "on", "help": "on", "starred": [], "apps": {}}

MAX_STARRED = 12
MAX_APP_OVERRIDES = 64

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


def _clean_value(key, value):
    """(ok, value) for one enumerated key."""
    allowed = ALLOWED.get(key)
    if allowed is None:
        return False, None
    if key in ("text", "ui"):
        # A STRING or an int, but never a bool (True == 1 would slip through).
        if isinstance(value, bool):
            return False, None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False, None
    return (value in allowed), value


def _clean_starred(value, refused) -> list:
    keep: list = []
    if not isinstance(value, list):
        refused.append("starred")
        return keep
    for item in value:
        # Strings only: str(7) would pass the slug shape and store a browser's bug.
        slug = item.strip().lower() if isinstance(item, str) else ""
        if not SLUG_RE.fullmatch(slug):
            refused.append("starred[%r]" % (item,))
            continue
        if slug not in keep:
            keep.append(slug)
        if len(keep) >= MAX_STARRED:
            break
    return keep


def _clean_apps(value, refused) -> dict:
    kept: dict = {}
    if not isinstance(value, dict):
        refused.append("apps")
        return kept
    for slug, inner in value.items():
        slug_n = slug.strip().lower() if isinstance(slug, str) else ""
        if not SLUG_RE.fullmatch(slug_n) or not isinstance(inner, dict):
            refused.append("apps.%s" % (slug,))
            continue
        if len(kept) >= MAX_APP_OVERRIDES:
            refused.append("apps.%s" % slug_n)
            continue
        # An empty inner dict is KEPT: it is the removal signal update() acts on. A key sent as
        # null is kept as None: "this one key back to my everywhere-value".
        out: dict = {}
        for key, v in inner.items():
            if key not in APP_SCOPED:
                refused.append("apps.%s.%s" % (slug_n, key))
                continue
            if v is None:
                out[key] = None
                continue
            ok, v = _clean_value(key, v)
            if ok:
                out[key] = v
            else:
                refused.append("apps.%s.%s" % (slug_n, key))
        kept[slug_n] = out
    return kept


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
        if key == "starred":
            out["starred"] = _clean_starred(value, refused)
            continue
        if key == "apps":
            out["apps"] = _clean_apps(value, refused)
            continue
        ok, value = _clean_value(key, value)
        if ok:
            out[key] = value
        else:
            refused.append(str(key))
    return out, refused


def valid_person(person: str) -> bool:
    return bool(PERSON_RE.fullmatch(person or ""))


def _stored(store: dict, person: str) -> dict:
    prefs = dict(DEFAULTS)
    stored, _ = clean(store.get(person) or {})
    # A stored override never holds a null and is never empty (update() prunes both).
    apps = {k: {kk: vv for kk, vv in v.items() if vv is not None}
            for k, v in (stored.get("apps") or {}).items()}
    stored["apps"] = {k: v for k, v in apps.items() if v}
    prefs.update(stored)
    return prefs


def get(hub_dir, person: str) -> dict:
    return _stored(_load(hub_dir), person)


def resolve(prefs: dict, slug: str) -> dict:
    """The person's everywhere-choices with `slug`'s override laid on top."""
    override = (prefs.get("apps") or {}).get((slug or "").strip().lower()) or {}
    out = {k: v for k, v in prefs.items() if k != "apps"}
    out.update({k: v for k, v in override.items() if k in APP_SCOPED and v is not None})
    return out


def update(hub_dir, person: str, raw) -> tuple[dict, list]:
    """Merge the accepted subset of `raw` onto what the person had: per key and, for `apps`,
    per app and per key -- saving THIS app's override never drops another app's, and no key
    this table knows (the agent placement included) is lost on the way through. A mark that
    says "draw my picture" with no picture is put back to initials rather than stored: it
    would render as a broken image on every app."""
    incoming, refused = clean(raw)
    with _LOCK:
        store = _load(hub_dir)
        merged = _stored(store, person)
        incoming_apps = incoming.pop("apps", None)
        merged.update(incoming)
        if isinstance(incoming_apps, dict):
            apps = {k: dict(v) for k, v in (merged.get("apps") or {}).items()}
            for slug, inner in incoming_apps.items():
                if not inner:
                    apps.pop(slug, None)
                    continue
                nxt = apps.get(slug) or {}
                for key, value in inner.items():
                    if value is None:
                        nxt.pop(key, None)
                    else:
                        nxt[key] = value
                if nxt:
                    apps[slug] = nxt
                else:
                    apps.pop(slug, None)       # nothing left: no trace of the app in the store
            merged["apps"] = apps
        if merged.get("kind") == "image" and not merged.get("image"):
            merged["kind"] = "initials"
        store[person] = merged
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        atomic.write_json(_path(hub_dir), store)
    return merged, refused
