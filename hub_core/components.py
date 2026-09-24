"""Shared UI components the hub HOSTS for the apps around it, and each app's settings for them.
Framework-free; the Django adapter serves it at /hub/components/.

WHY THE HUB HOSTS THEM. A component copied into every app is a component with N versions: a
fix lands in one copy and the rest drift until nobody can say which app runs which. So the
component's bytes live once, in `hub_core/components/<name>/`, and an app LINKS them:

    <link rel="stylesheet" href="/hub/components/agent/agent.css">
    <script src="/hub/components/agent/agent.js" defer></script>

When the apps are served from the same origin as the hub (the usual reverse-proxy layout),
that is a same-origin link with no bundle and no build step, and a change to the master
reaches every adopting app on its next page load with no app deploy.

WHAT IS SERVED, AND WHAT IS NOT. Presentation only -- CSS, JS, a manifest, images. No board
record, no identity. A file is served only when its name matches a file actually on disk
under a component directory, with no separators and an allowed suffix, so there is no path
the caller controls and no traversal to get wrong.

A VERSION IS MEASURED, NEVER DECLARED. A declared version somebody forgets to bump tells every
adopter they are current when they are not; the version here is a hash of the files being
served, so it cannot disagree with them.

PER-APP PROPERTIES. A component may declare, in its manifest, the presentation settings an
operator can set PER APP ("props": [fields]). Each field is a closed set (`choice`, `multi`)
or length-capped text (`text`, `list`). The schema travels with the component, so a new
component brings its own settings and the hub needs no change. Values are stored per app
slug in HUB_DIR/component-props.json; only values that differ from the defaults are stored,
so a later change to a default reaches every app that never chose otherwise. A value outside
its field is REFUSED and reported, never clamped to a neighbour: a caller sending an unknown
option has a bug, and quietly honouring the nearest value hides it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import re
import threading
import time
from pathlib import Path

from . import atomic

COMPONENTS = Path(__file__).resolve().parent / "components"

#: Chrome, not a file drop.
ALLOWED_SUFFIXES = {".css", ".js", ".json", ".svg", ".png", ".map"}
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
CACHE_SECONDS = 300
PROPS_STORE = "component-props.json"
HISTORY_KEEP = 25
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_LOCK = threading.Lock()


# ---------------------------------------------------------------- serving the files

def _component_dir(name: str) -> Path | None:
    if not name or not _NAME_RE.fullmatch(name):
        return None
    path = COMPONENTS / name
    return path if path.is_dir() else None


def _files(directory: Path) -> list[Path]:
    return sorted(f for f in directory.iterdir()
                  if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES)


def version(directory: Path) -> str:
    digest = hashlib.sha256()
    for f in _files(directory):
        digest.update(f.name.encode("utf-8"))
        digest.update(f.read_bytes())
    return digest.hexdigest()[:12]


def manifest(name: str) -> dict:
    directory = _component_dir(name)
    if directory is None:
        return {}
    path = directory / "manifest.json"
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"error": "manifest.json is not valid JSON"}
    return body if isinstance(body, dict) else {}


def read_file(name: str, filename: str):
    """(bytes, content_type, etag, version) for one served file, or None when it is not one."""
    directory = _component_dir(name)
    if directory is None or not filename or not _NAME_RE.fullmatch(filename.lower()):
        return None
    path = directory / filename
    if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    body = path.read_bytes()
    content_type = (CONTENT_TYPES.get(path.suffix.lower())
                    or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
    return body, content_type, etag, version(directory)


def index(adopters: dict | None = None) -> list[dict]:
    """Every hosted component: name, measured version, files, title, what, how to link it,
    whether it takes per-app properties, and the apps observed loading it."""
    out = []
    if not COMPONENTS.is_dir():
        return out
    for directory in sorted(p for p in COMPONENTS.iterdir() if p.is_dir()):
        files = [f.name for f in _files(directory)]
        if not files:
            continue
        man = manifest(directory.name)
        out.append({
            "name": directory.name,
            "version": version(directory),
            "files": files,
            "title": man.get("title") or directory.name,
            "what": man.get("what") or "",
            "link": man.get("link") or [],
            "mount": man.get("mount") or "",
            "props": bool(man.get("props")),
            # Observed, not declared: the apps whose pages fetched this component's
            # properties, with when each was last seen (note_adopter below).
            "used_by": dict(sorted(((adopters or {}).get(directory.name) or {}).items())),
        })
    return out


# ---------------------------------------------------------------- per-app properties

def schema() -> list[dict]:
    """[{component, title, fields}] for every hosted component that declares properties."""
    out = []
    if not COMPONENTS.is_dir():
        return out
    for directory in sorted(p for p in COMPONENTS.iterdir() if p.is_dir()):
        man = manifest(directory.name)
        fields = [f for f in (man.get("props") or []) if isinstance(f, dict) and f.get("key")]
        if fields:
            out.append({"component": directory.name,
                        "title": man.get("title") or directory.name, "fields": fields})
    return out


def _fields() -> dict:
    return {sec["component"]: {f["key"]: f for f in sec["fields"]} for sec in schema()}


def defaults(fields: dict | None = None) -> dict:
    fields = _fields() if fields is None else fields
    return {comp: {k: copy.deepcopy(f.get("default", "")) for k, f in entries.items()}
            for comp, entries in fields.items()}


def _clean_value(field: dict, value):
    """(ok, cleaned). ok=False means the value is refused and must be reported."""
    kind = field.get("type")
    options = [o.get("value") for o in (field.get("options") or []) if isinstance(o, dict)]
    if kind == "choice":
        return (value in options, value)
    if kind == "multi":
        if not isinstance(value, list) or any(v not in options for v in value):
            return (False, None)
        return (True, [v for v in options if v in value])        # schema order, no duplicates
    if kind == "text":
        if not isinstance(value, str):
            return (False, None)
        value = value.strip() if int(field.get("lines", 1)) > 1 else " ".join(value.split())
        return (len(value) <= int(field.get("max", 200)), value)
    if kind == "list":
        if not isinstance(value, list) or len(value) > int(field.get("max_items", 8)):
            return (False, None)
        out = []
        for item in value:
            if not isinstance(item, str):
                return (False, None)
            item = " ".join(item.split())
            if len(item) > int(field.get("max", 200)):
                return (False, None)
            if item:
                out.append(item)
        return (True, out)
    return (False, None)


def clean(raw, fields: dict | None = None) -> tuple[dict, list]:
    """Only what the schemas allow, as {component: {key: value}}, plus every refused
    `component.key` (an unknown component or key is refused too)."""
    fields = _fields() if fields is None else fields
    out, refused = {}, []
    if not isinstance(raw, dict):
        return out, ["(props is not an object)"]
    for comp, values in raw.items():
        entries = fields.get(comp)
        if entries is None or not isinstance(values, dict):
            refused.append(str(comp))
            continue
        for key, value in values.items():
            field = entries.get(key)
            ok, cleaned = _clean_value(field, value) if field else (False, None)
            if ok:
                out.setdefault(comp, {})[key] = cleaned
            else:
                refused.append("%s.%s" % (comp, key))
    return out, refused


def _store_path(hub_dir) -> Path:
    return Path(hub_dir) / PROPS_STORE


def _load(hub_dir) -> dict:
    try:
        body = json.loads(_store_path(hub_dir).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A corrupt store must not take every app's components down at once: it reads as
        # "nothing set", which is what a new hub looks like, and the next save repairs it.
        return {}
    return body if isinstance(body, dict) else {}


def get_record(hub_dir, slug: str) -> dict:
    fields = _fields()
    rec = _load(hub_dir).get(slug) or {}
    props = defaults(fields)
    stored, _ = clean(rec.get("props") or {}, fields)
    for comp, values in stored.items():
        props[comp].update(values)
    return {"props": props, "updated_at": rec.get("updated_at") or "",
            "updated_by": rec.get("updated_by") or "",
            "history": list(rec.get("history") or [])}


def set_props(hub_dir, slug: str, raw, actor: str) -> tuple[dict, list]:
    """Replace the app's properties with the cleaned `raw` -- a FULL set: a key left out goes
    back to its default -- and append who changed what, and when, to its short history."""
    fields = _fields()
    incoming, refused = clean(raw, fields)
    with _LOCK:
        store = _load(hub_dir)
        before = get_record(hub_dir, slug)["props"]
        props = defaults(fields)
        for comp, values in incoming.items():
            props[comp].update(values)
        changed = sorted("%s.%s" % (c, k) for c in props for k in props[c]
                         if props[c][k] != before.get(c, {}).get(k))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = store.get(slug) if isinstance(store.get(slug), dict) else {}
        history = list(rec.get("history") or [])
        if changed:
            history.append({"at": now, "by": actor, "changed": changed})
        stored = {c: {k: v for k, v in props[c].items() if v != fields[c][k].get("default", "")}
                  for c in props}
        store[slug] = {"props": {c: v for c, v in stored.items() if v},
                       "updated_at": now if changed else rec.get("updated_at", ""),
                       "updated_by": actor if changed else rec.get("updated_by", ""),
                       "history": history[-HISTORY_KEEP:]}
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        atomic.write_json(_store_path(hub_dir), store)
    return get_record(hub_dir, slug), refused


def stamp(hub_dir) -> tuple:
    """Change fingerprint of the properties store, for cache keys."""
    try:
        st = _store_path(hub_dir).stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (0, 0)


# ---------------------------------------------------------------- observed adoption

ADOPTERS_STORE = "component-adopters.json"
_ADOPT_SEEN: dict = {}
ADOPT_WRITE_EVERY_S = 300


def note_adopter(hub_dir, component: str, slug: str, now: float | None = None) -> None:
    """Record that `slug` loaded `component` (the component's own properties fetch names both).
    Adoption is OBSERVED from the page that actually linked the component, never declared
    beside it. Throttled per pair; never raises -- bookkeeping must not break a page."""
    if not _component_dir(component) or not SLUG_RE.fullmatch(slug or ""):
        return
    now = time.time() if now is None else now
    key = (str(hub_dir), component, slug)
    if now - _ADOPT_SEEN.get(key, 0.0) < ADOPT_WRITE_EVERY_S:
        return
    _ADOPT_SEEN[key] = now
    path = Path(hub_dir) / ADOPTERS_STORE
    try:
        with _LOCK:
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            body.setdefault(component, {})[slug] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            Path(hub_dir).mkdir(parents=True, exist_ok=True)
            atomic.write_json(path, body)
    except (OSError, TimeoutError):
        pass


def adopters(hub_dir) -> dict:
    """{component: {slug: last_seen_iso}} as observed."""
    try:
        body = json.loads((Path(hub_dir) / ADOPTERS_STORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}
