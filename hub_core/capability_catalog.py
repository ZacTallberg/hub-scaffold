"""A capability catalog PUBLISHED from another repository, pinned to one commit.

The board's own `cap` entities describe this project's capability graph. Many teams also keep
a larger, diffable catalog of "what an agent can already do here" — tools, recipes, contracts,
reusable components — in a separate repository, where it is reviewed like code and publishing
it is a push. This module serves that catalog read-only, without ever letting a half-written or
mismatched publication reach a reader.

Configuration (all optional; with none set the catalog is simply absent and says so):

    HUB_CAPABILITY_REPO       path to a local git checkout of the publishing repository
    HUB_CAPABILITY_REF        ref to publish from (default: HEAD)
    HUB_CAPABILITY_EXPORT     path of the export inside that repo (default: capabilities.export.json)
    HUB_CAPABILITY_MANIFEST   optional second file that must be read at the SAME commit
    HUB_CAPABILITY_FETCH      "1" to `git fetch` before resolving the ref (a mirror)

THE PROPERTIES, each earned by a failure on the instance this was lifted from:

  * ONE COMMIT. The export and its manifest are read with `git show <sha>:<path>` at the SAME
    resolved sha. Reading each at "the latest" lets a publication land between the two reads
    and pair an old export with a new manifest.
  * REFUSE AN INCOMPLETE PUBLICATION. Every item needs name/kind/what/when/get, names are
    unique, every item must map onto a row the board's own `cap` schema accepts (an importer
    that drifted from a schema with `additionalProperties: false` produced rows nothing could
    store), and an item naming a manifest key must find it. A refused publication never
    replaces the last good one.
  * NEVER BLOCK A READER. A refresh runs on a background thread at most once per POLL_S;
    readers get the last complete publication (cached atomically on disk, so it survives a
    restart and a source outage) and a `publication` block saying `current`, `cached` (with
    the error), or `none`.

Stdlib only; the schema check is injected by the adapter (`validate_row`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

POLL_S = 60
REQUIRED = ("name", "kind", "what", "when", "get")

_LOCK = threading.Lock()
_STATE = {"cache": None, "publication": None, "error": "", "running": False, "next": 0.0,
          "verified": False, "config": None}


def config() -> dict:
    repo = (os.environ.get("HUB_CAPABILITY_REPO") or "").strip()
    return {"repo": repo,
            "ref": (os.environ.get("HUB_CAPABILITY_REF") or "HEAD").strip(),
            "export": (os.environ.get("HUB_CAPABILITY_EXPORT") or "capabilities.export.json").strip(),
            "manifest": (os.environ.get("HUB_CAPABILITY_MANIFEST") or "").strip(),
            "fetch": (os.environ.get("HUB_CAPABILITY_FETCH") or "") == "1"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(name or "").lower()).strip("-.")[:80] or "cap"


def alias(text: str) -> str:
    """A name reduced to letters and digits, so `components/auth-gate`, `auth-gate` and
    `Auth Gate` are ONE key. The id match alone let 49 superseded ledger caps survive beside
    their catalog replacements, and the tab counted 423 for 374 capabilities."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def cap_row(item: dict, project_key: str) -> dict:
    """The `cap` entity a catalog item maps onto. Validated at publication time."""
    return {"id": "%s:cap:%s" % (project_key, slug(item.get("name"))), "type": "cap",
            "name": str(item.get("name") or ""), "maturity": "proven",
            "needs": str(item.get("what") or ""), "iface": str(item.get("get") or ""),
            "pivot_notes": str(item.get("when") or ""), "version": 1}


def validate_export(export, manifest, *, project_key: str, validate_row=None) -> None:
    """Raise ValueError naming the first reason this publication is not whole."""
    if not isinstance(export, dict) or not isinstance(export.get("items"), list):
        raise ValueError("export has no item list")
    if not export["items"]:
        raise ValueError("export is empty")
    names, slugs = set(), set()
    kits = (manifest or {}).get("kits") if isinstance(manifest, dict) else None
    for i, item in enumerate(export["items"]):
        if not isinstance(item, dict):
            raise ValueError("item %d is not an object" % i)
        missing = [k for k in REQUIRED if not isinstance(item.get(k), str) or not item[k].strip()]
        if missing:
            raise ValueError("item %d (%s) is incomplete: missing %s"
                             % (i, item.get("name") or "?", ", ".join(missing)))
        if item["name"] in names or slug(item["name"]) in slugs:
            raise ValueError("duplicate capability name %r" % item["name"])
        names.add(item["name"])
        slugs.add(slug(item["name"]))
        if validate_row is not None:
            errors = validate_row(cap_row(item, project_key))
            if errors:
                raise ValueError("item %r does not fit the cap schema: %s" % (item["name"], errors[0]))
        if item.get("kit") is not None:
            if not isinstance(kits, dict) or item["kit"] not in kits:
                raise ValueError("item %r names kit %r, which the manifest at this commit does "
                                 "not carry" % (item["name"], item["kit"]))


def _git(repo, *args, timeout=20) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise ValueError("git %s failed: %s" % (args[0], out.stderr.decode("utf-8", "replace").strip()[:200]))
    return out.stdout.decode("utf-8", "replace")


def fetch_publication(cfg: dict, previous, *, project_key: str, validate_row=None) -> dict:
    """Resolve the ref ONCE and read every file at that sha. Raises ValueError when not whole."""
    repo = cfg["repo"]
    if cfg.get("fetch"):
        _git(repo, "fetch", "--quiet", timeout=60)
    sha = _git(repo, "rev-parse", "--verify", cfg["ref"] + "^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("the ref resolved to no commit")
    if previous and previous.get("commit") == sha and previous.get("config") == _cfg_key(cfg):
        return {**previous, "checked_at": time.time()}
    try:
        export = json.loads(_git(repo, "show", "%s:%s" % (sha, cfg["export"])))
    except json.JSONDecodeError as exc:
        raise ValueError("export is not JSON: %s" % exc) from exc
    manifest = None
    if cfg.get("manifest"):
        try:
            manifest = json.loads(_git(repo, "show", "%s:%s" % (sha, cfg["manifest"])))
        except json.JSONDecodeError as exc:
            raise ValueError("manifest is not JSON: %s" % exc) from exc
    validate_export(export, manifest, project_key=project_key, validate_row=validate_row)
    updated = _git(repo, "show", "-s", "--format=%cI", sha).strip()
    return {"commit": sha, "updated": updated, "checked_at": time.time(), "config": _cfg_key(cfg),
            "export": export, "manifest": manifest}


def _cfg_key(cfg) -> str:
    return "|".join(str(cfg.get(k) or "") for k in ("repo", "ref", "export", "manifest"))


def _read_cached(path: Path, cfg, *, project_key, validate_row):
    """The last complete publication on disk, or None when absent, foreign, or not whole."""
    try:
        publication = json.loads(path.read_text(encoding="utf-8"))
        if publication.get("config") != _cfg_key(cfg):
            return None
        validate_export(publication["export"], publication.get("manifest"),
                        project_key=project_key, validate_row=validate_row)
        if not re.fullmatch(r"[0-9a-f]{40}", str(publication.get("commit") or "")):
            return None
        return publication
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _refresh(path: Path, cfg, previous, project_key, validate_row):
    try:
        publication = fetch_publication(cfg, previous, project_key=project_key, validate_row=validate_row)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(publication, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        with _LOCK:
            _STATE.update(publication=publication, error="", verified=True)
    except Exception as exc:                                  # noqa: BLE001 - keep the last good one
        with _LOCK:
            _STATE["error"] = "catalog refresh failed: %s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        with _LOCK:
            _STATE.update(running=False, next=time.time() + POLL_S)


def snapshot(hub_dir, *, project_key: str, validate_row=None, wait: bool = False):
    """`(publication_or_None, meta)`. Starts at most one background refresh per POLL_S and
    returns immediately with the last complete publication. `wait=True` runs the refresh
    inline (a CLI or an index job that can afford the git calls)."""
    cfg = config()
    if not cfg["repo"]:
        return None, {"state": "none", "commit": "", "checked_at": None,
                      "error": "no catalog configured (set HUB_CAPABILITY_REPO)"}
    path = Path(hub_dir) / "capability-publication.json"
    with _LOCK:
        if _STATE["config"] != _cfg_key(cfg) or _STATE["cache"] != str(path):
            _STATE.update(config=_cfg_key(cfg), cache=str(path), error="", running=False, next=0.0,
                          verified=False,
                          publication=_read_cached(path, cfg, project_key=project_key,
                                                   validate_row=validate_row))
        start = not _STATE["running"] and time.time() >= _STATE["next"]
        if start:
            _STATE["running"] = True
        previous = _STATE["publication"]
    if start:
        if wait:
            _refresh(path, cfg, previous, project_key, validate_row)
        else:
            threading.Thread(target=_refresh, args=(path, cfg, previous, project_key, validate_row),
                             name="hub-capability-publication", daemon=True).start()
    with _LOCK:
        publication = _STATE["publication"]
        cached = bool(_STATE["error"] or not _STATE["verified"])
        return publication, {
            "state": ("cached" if cached else "current") if publication else "none",
            "commit": publication["commit"] if publication else "",
            "checked_at": publication.get("checked_at") if publication else None,
            "error": _STATE["error"] or ("" if publication else
                                         "the first publication is being read; ask again shortly"
                                         if _STATE["running"] else
                                         "no complete publication has been read yet")}


# ── serving ──

def items(publication, project_key: str) -> list:
    """Catalog items in serving shape: the author's fields plus a stable board id."""
    out = []
    for item in ((publication or {}).get("export") or {}).get("items") or []:
        if isinstance(item, dict):
            row = dict(item)
            row["id"] = "%s:cap:%s" % (project_key, slug(item["name"]))
            row["source"] = "catalog"
            out.append(row)
    return out


def ledger_items(caps) -> list:
    """Ledger `cap` entities in the same serving shape."""
    out = []
    for c in caps or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append({"id": c.get("id"), "name": c["name"], "kind": c.get("kind") or "capability",
                    "what": c.get("needs") or "", "when": c.get("pivot_notes") or "",
                    "get": c.get("iface") or "", "maturity": c.get("maturity") or "",
                    "source": "ledger"})
    return out


def replaced_by_catalog(cat: list, caps) -> set:
    """Ids of ledger `cap`s the catalog already names — by id AND by letters-and-digits alias,
    with and without a family prefix. ONE rule for every surface: the catalog tab and search
    once disagreed, one counting a capability once and the other listing it twice."""
    cat = list(cat or [])
    ids = {i["id"] for i in cat}
    aliases = set()
    for i in cat:
        aliases.update(a for a in (alias(i["name"]), alias(str(i["name"]).rsplit("/", 1)[-1])) if a)
    out = set()
    for c in caps or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        keys = [k for k in (alias(str(c["id"]).rsplit(":", 1)[-1]), alias(c.get("name"))) if k]
        if c["id"] in ids or any(k in aliases for k in keys):
            out.add(c["id"])
    return out


def merged(cat: list, caps) -> list:
    """Catalog items (serving shape, from `items()`) first, then every ledger cap the catalog
    does not already name, so one capability under two spellings is counted once."""
    cat = list(cat or [])
    gone = replaced_by_catalog(cat, caps)
    return cat + [row for row in ledger_items(caps) if row["id"] not in gone]


_SUFFIXES = ("ically", "ation", "ical", "ing", "ic", "ed", "es", "s")


def _variants(term: str) -> tuple:
    out = [term]
    if len(term) >= 5:
        for suffix in _SUFFIXES:
            if term.endswith(suffix) and len(term) - len(suffix) >= 4:
                out.append(term[:-len(suffix)])
                break
    return tuple(out)


_STOP = frozenset({"the", "a", "an", "is", "of", "to", "and", "or", "in", "on", "for", "it", "with", "at"})


def matches(rows: list, q: str) -> list:
    """Whole-phrase hits first, then rows containing EVERY word (as written or by its stem:
    "agentic" finds "agent"). A whole-query substring test only ever matched the catalog's
    exact spelling; measured, a two-word query answered 0 of 374 while both words existed."""
    q = str(q or "").strip().lower()
    if not q:
        return list(rows)
    texts = [(r, json.dumps(r, ensure_ascii=False).lower()) for r in rows]
    exact = [r for r, text in texts if q in text]
    words = [t for t in re.split(r"[^a-z0-9._-]+", q) if t and t not in _STOP]
    if not words:
        return exact
    seen = {id(r) for r in exact}
    return exact + [r for r, text in texts if id(r) not in seen
                    and all(any(v in text for v in _variants(t)) for t in words)]
