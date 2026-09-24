"""The visibility VEIL — what a limited-permission reader (a contributor's agent) may know.

Some adopters give an outside or junior contributor most of the tools so they can be productive,
while certain facets of the project (a customer, a system of record, a credential store) must be
invisible to them: their agents must not even learn that the thing EXISTS, so they cannot
hallucinate the ability to touch it. Three rules, and every surface that hands bytes to a reader
runs through them:

1. OMISSION, NEVER MARKING. A hidden record is absent. No "restricted" placeholder, no count of
   hidden items, no 403 — a hidden route answers 404 exactly as an unknown route does.
2. FAIL CLOSED BY SHAPE. A reader whose tier is unknown is a contributor. A registry that cannot
   be read hides everything from contributors rather than nothing. A read route that has not
   declared its visibility does not serve contributors at all.
3. TERMS, NOT JUST TAGS. A record is hidden when it is tagged with a hidden facet AND when its
   text mentions one of that facet's terms. Free text is where existence leaks.

The boundary itself is DATA: ``PROJECT/facets.json`` (git-versioned, edited by the operator).
This module never decides what is sensitive; it decides how a sensitivity, once named, is
enforced everywhere at once. With no registry file the veil is OPEN for every tier — an adopter
that declares no facets pays nothing for this module existing.

Framework-free; tiers are a small sidecar (``tiers.json`` in the hub dir) keyed by agent name.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .process_lock import ProcessFileLock

TIERS = ("operator", "member", "contributor")
DEFAULT_TIER = "contributor"          # the narrowest view is the one an unknown reader gets

_CACHE: dict = {"stamp": None, "doc": None}

# Markdown fences: `<!-- facet: billing -->` ... `<!-- /facet -->`. A block may name several
# facets (visible only when ALL are visible) and may negate one (`!billing`: visible only to tiers
# that do NOT see that facet — the alternate text a contributor gets instead). The marker lines are
# removed for everyone.
_MD_OPEN = re.compile(r"^[ \t]*<!--\s*facet\s*:\s*([^>]*?)\s*-->[ \t]*\r?$", re.M)
_MD_CLOSE = re.compile(r"^[ \t]*<!--\s*/facet\s*-->[ \t]*\r?$", re.M)


def registry(path) -> dict:
    """The facet registry, normalised, cached on the file's mtime. Missing -> no facets (the veil
    is open); unreadable or malformed -> ``broken`` (the veil fails closed for contributors)."""
    path = Path(path)
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {"version": 0, "facets": [], "missing": True}
    if _CACHE["stamp"] == stamp and _CACHE["doc"] is not None:
        return _CACHE["doc"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"version": 0, "facets": [], "broken": "unreadable"}
    facets, broken = [], ""
    for f in (raw.get("facets") if isinstance(raw, dict) else None) or []:
        if not isinstance(f, dict) or not str(f.get("id") or "").strip():
            broken = "a facet without an id"
            continue
        tiers = [str(t).strip().lower() for t in (f.get("tiers") or ["operator", "member"])]
        if any(t not in TIERS for t in tiers):
            broken = "facet %s names an unknown tier" % f["id"]
        facets.append({
            "id": str(f["id"]).strip(),
            "label": str(f.get("label") or f["id"]),
            "tiers": tiers,
            "terms": [str(t).strip() for t in (f.get("terms") or []) if str(t).strip()],
            "tags": [str(t).strip().lower() for t in (f.get("tags") or []) if str(t).strip()],
            "routes": [str(t).strip() for t in (f.get("routes") or []) if str(t).strip()],
        })
    doc = {"version": raw.get("version") if isinstance(raw, dict) else None, "facets": facets}
    if broken:
        doc["broken"] = broken
    _CACHE.update(stamp=stamp, doc=doc)
    return doc


def _term_pattern(term: str) -> str:
    """A term matches as a WHOLE token: `ledger` hits "the ledger puller" and not "ledgers-v2";
    `billing*` hits every token that starts that way."""
    if term.endswith("*") and len(term) > 1:
        return r"(?<![A-Za-z0-9])" + re.escape(term[:-1])
    return r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"


class Veil:
    """One tier's view. `open` means nothing is hidden and every filter is a pass-through."""

    def __init__(self, tier: str, doc: dict | None = None):
        tier = str(tier or "").strip().lower()
        self.tier = tier if tier in TIERS else DEFAULT_TIER
        self.doc = doc if doc is not None else {"facets": []}
        self.broken = bool(self.doc.get("broken")) and self.tier == "contributor"
        self.hidden = [f for f in self.doc.get("facets") or [] if self.tier not in f["tiers"]]
        self.open = not self.hidden and not self.broken
        self._by_term = []
        for f in self.hidden:
            for term in f["terms"]:
                try:
                    self._by_term.append((re.compile(_term_pattern(term), re.I), f["id"]))
                except re.error:
                    continue
        self._tags = {t: f["id"] for f in self.hidden for t in f["tags"]}
        self.hidden_ids = {f["id"] for f in self.hidden}
        self.hidden_routes = {r for f in self.hidden for r in f["routes"]}

    def hit(self, text) -> str | None:
        """The id of the first hidden facet this text (or JSON-able value) mentions, or None. A
        broken registry makes every non-empty text a hit: the veil cannot know what is sensitive,
        so for a contributor everything is."""
        if self.open or not text:
            return None
        if self.broken:
            return "registry"
        s = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
        for rx, fid in self._by_term:
            if rx.search(s):
                return fid
        return None

    def visible(self, record) -> bool:
        """A board record, an inbox item, a presence row: visible unless tagged into or worded
        into a hidden facet."""
        if self.open:
            return True
        if self.broken:
            return False
        if isinstance(record, dict):
            for t in record.get("tags") or []:
                if str(t).lower() in self._tags:
                    return False
        return self.hit(record) is None

    def filter(self, records):
        return list(records or []) if self.open else [r for r in records or [] if self.visible(r)]

    def scrub(self, obj):
        """Omit hidden mentions from a nested payload: list items (records) and string values
        that hit are dropped, a dict key that hits is dropped with its value; structure is kept.
        The generic shape every veiled JSON surface is passed through."""
        if self.open:
            return obj
        if isinstance(obj, list):
            out = []
            for x in obj:
                if isinstance(x, (dict, list)):
                    if (not isinstance(x, dict) or self.visible(x)) and self.hit(x) is None:
                        out.append(self.scrub(x))
                elif isinstance(x, str):
                    if self.hit(x) is None:
                        out.append(x)
                else:
                    out.append(x)
            return out
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if self.hit(str(k)) is not None:
                    continue
                if isinstance(v, str):
                    if self.hit(v) is None:
                        out[k] = v
                else:
                    out[k] = self.scrub(v)
            return out
        return obj

    def facets_visible(self, spec: str) -> bool:
        """Evaluate a fence spec: `a, b` (all visible), `!a` (a is hidden)."""
        if self.broken:
            return False
        for part in [p.strip() for p in spec.split(",") if p.strip()]:
            neg = part.startswith("!")
            hidden = part.lstrip("!").strip() in self.hidden_ids
            if (neg and not hidden) or (not neg and hidden):
                return False
        return True

    def strip(self, markdown: str) -> str:
        """Markdown with every fenced block this tier may not see removed (markers removed for
        everyone). An unterminated fence drops to the end of the document: fail closed."""
        if self.broken:
            return ""
        out, keep, depth = [], True, 0
        for line in str(markdown or "").splitlines(keepends=True):
            bare = line.rstrip("\r\n")
            m = _MD_OPEN.match(bare)
            if m:
                if depth == 0:
                    keep = self.facets_visible(m.group(1))
                depth += 1
                continue
            if _MD_CLOSE.match(bare):
                depth = max(0, depth - 1)
                if depth == 0:
                    keep = True
                continue
            if keep:
                out.append(line)
        return "".join(out)

    def route_visible(self, route_name: str) -> bool:
        return self.open or (not self.broken and route_name not in self.hidden_routes)

    def terms(self) -> list:
        """(term, facet id) for every term hidden from this tier — what a leak audit scans for."""
        return [(t, f["id"]) for f in self.hidden for t in f["terms"]]


# ── tiers: who is which ──────────────────────────────────────────────────────────────────────

def _tiers_path(hub_dir) -> Path:
    return Path(hub_dir) / "tiers.json"


def read_tiers(hub_dir) -> dict:
    try:
        raw = json.loads(_tiers_path(hub_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k).lower(): str(v).lower() for k, v in (raw or {}).items()
            if str(v).lower() in TIERS} if isinstance(raw, dict) else {}


def set_tier(hub_dir, agent: str, tier: str) -> dict:
    """Set (or, with tier "", clear) one agent's tier. Returns the full mapping."""
    agent = str(agent or "").strip().lower()
    tier = str(tier or "").strip().lower()
    if not agent or (tier and tier not in TIERS):
        raise ValueError("agent and a tier in %s are required" % (TIERS,))
    path = _tiers_path(hub_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(path.parent, name=".tiers.lock", timeout=5):
        tiers = read_tiers(hub_dir)
        if tier:
            tiers[agent] = tier
        else:
            tiers.pop(agent, None)
        tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
        tmp.write_text(json.dumps(tiers, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return tiers
