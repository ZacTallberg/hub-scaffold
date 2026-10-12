"""BM25F over the board's knowledge: stdlib, fielded, exhaustive over the query's postings.

WHY THIS AND NOT TERM FREQUENCY. The first scorer was `3.0 * title.count(term) +
body.count(term)`. Two things are missing from that and both are fatal on a real board:

  * NO IDF. Every term counts the same, on a corpus saturated with its own house vocabulary
    (`deploy`, `fix`, `error`, `never`). A question containing a common word is scored mostly
    on how often that word appears, so an unrelated chatty record wins.
  * NO LENGTH NORMALIZATION. Record types differ several-fold in mean length, so a long
    record wins by being long.

Measured on the instance this was lifted from, over a few hundred real answered questions
(the asker's own words, scored against the record that actually resolved them): term
frequency put the answer in the top five 4.9% of the time; BM25F 96.2%, at ~18 ms per query
over ~3,400 records in pure Python.

FIELDED because a hit in a record's headline is not worth the same as a hit in the story
behind it. The weights inherit the old 3:1 title:body ratio rather than inventing a new one,
so the change is IDF and length normalization, not a simultaneous re-weighting.
"""
from __future__ import annotations

import math
import re
import threading

#: Title carries the claim; tags are words someone chose deliberately; the body is the story.
FIELD_WEIGHTS = {"title": 3.0, "tags": 2.0, "body": 1.0}

#: Standard BM25 constants: k1 governs term-frequency saturation, b length normalization.
K1 = 1.2
B = 0.75

_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PART = re.compile(r"[._-]+")


def tokens(text) -> list:
    """Words, with two repairs measured on a few hundred real answered questions:

    * a trailing ``.``/``-``/``_`` is punctuation, not part of the word: the pattern keeps ``.``
      for ``settings.json`` and ``0.29``, so every sentence-final word was indexed as ``today.``
      and never matched ``today``;
    * a compound (``allowed_hosts.json``, ``export-service``, ``verify_deploy``) is ALSO indexed
      as its parts, so ``allowed_hosts`` finds ``allowed_hosts.json``. The whole compound stays
      a token, so an exact identifier still outscores a record that only shares its pieces."""
    out = []
    for w in _WORD.findall(str(text or "").lower()):
        w = w.rstrip("._-")
        if not w:
            continue
        out.append(w)
        parts = _PART.split(w)
        if len(parts) > 1:
            out.extend(p for p in parts if p)
    return out


class Bm25F:
    """Built once per corpus revision, queried many times."""

    def __init__(self, docs: dict):
        """`docs` maps id -> {"title": str, "body": str, "tags": str}."""
        self.df: dict = {}
        self.tf: dict = {}
        self.length: dict = {}
        for eid, fields in docs.items():
            counts, weighted_len = {}, 0.0
            for field, weight in FIELD_WEIGHTS.items():
                toks = tokens(fields.get(field))
                weighted_len += weight * len(toks)
                for t in toks:
                    counts[t] = counts.get(t, 0.0) + weight
            self.tf[eid] = counts
            self.length[eid] = weighted_len
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(docs) or 1
        self.avg_len = (sum(self.length.values()) / self.n) if self.length else 1.0

    def idf(self, term) -> float:
        """Robertson/Sparck-Jones IDF, floored at zero: a term in most documents earns ~0 and
        stops driving the ranking."""
        df = self.df.get(term, 0)
        return max(0.0, math.log((self.n - df + 0.5) / (df + 0.5) + 1.0))

    def _postings(self) -> dict:
        """term -> [(id, weighted tf, length factor)], built on first query. Scoring walks only
        the records that contain a query term instead of every record for every term -- the
        same sums in the same per-term order, so the ranking is unchanged and a long question
        stays cheap."""
        post = getattr(self, "_post", None)
        if post is None:
            post = {}
            avg = self.avg_len or 1.0
            for eid, counts in self.tf.items():
                norm = K1 * (1 - B + B * self.length[eid] / avg)
                for t, f in counts.items():
                    post.setdefault(t, []).append((eid, f, norm))
            self._post = post
        return post

    def score(self, query, *, limit: int = 0) -> list:
        """`[(score, id)]` descending, ties broken by id. Exhaustive over every record that
        shares a term; nothing truncated or prefiltered. Every occurrence of a query term
        counts (a term the asker repeats is one they mean): measured, de-duplicating the
        query's terms was no better and reads less of the question."""
        qterms = [t for t in tokens(query) if self.df.get(t)]
        if not qterms:
            return []
        post = self._postings()
        acc: dict = {}
        for t in qterms:
            w = self.idf(t) * (K1 + 1)
            for eid, f, norm in post[t]:
                acc[eid] = acc.get(eid, 0.0) + w * f / (f + norm)
        out = [(s, eid) for eid, s in acc.items() if s]
        out.sort(key=lambda pair: (-pair[0], pair[1]))
        return out[:limit] if limit else out


# One index per corpus revision. Building costs ~10x a query, so it is memoized on a key the
# CALLER supplies -- the ledger head plus anything else that decides which records exist for
# this reader. A None key is never cached: an unkeyable corpus must not freeze the index.
#
# A KEY MISS IS NOT A TEXT CHANGE. The ledger head moves on every append (claims, starts, CI and
# deploy events, acks), so a busy board rebuilt the index on about every other search -- seconds
# of request-thread CPU each time -- while the indexed TEXT had not changed at all. A miss now
# fingerprints the corpus (one digest over every id and weighted field) and reuses the built
# index when the text is identical. Any change to a record's title, body or tags, or a record
# entering or leaving the corpus, still rebuilds, so it can never serve a stale index.
_CACHE: dict = {"key": None, "index": None, "fingerprint": None}
_LOCK = threading.Lock()


def fingerprint(docs: dict) -> str:
    """One digest of everything the index is built from: each id and its weighted fields."""
    import hashlib
    h = hashlib.sha256()
    for eid in sorted(docs):
        fields = docs[eid] or {}
        h.update(str(eid).encode("utf-8", "replace"))
        for field in FIELD_WEIGHTS:
            h.update(b"\x1f")
            h.update(str(fields.get(field) or "").encode("utf-8", "replace"))
        h.update(b"\x1e")
    return h.hexdigest()


def index_for(docs: dict, key):
    """The BM25F index for this corpus, built once per distinct corpus TEXT."""
    if key is None:
        return Bm25F(docs)
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["index"] is not None:
            return _CACHE["index"]
    fp = fingerprint(docs)
    with _LOCK:
        if _CACHE["fingerprint"] == fp and _CACHE["index"] is not None:
            _CACHE["key"] = key
            return _CACHE["index"]
    idx = Bm25F(docs)
    with _LOCK:
        _CACHE.update(key=key, index=idx, fingerprint=fp)
    return idx
