"""BM25F over the board's knowledge: stdlib, fielded, full scan.

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


def tokens(text) -> list:
    return _WORD.findall(str(text or "").lower())


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

    def score(self, query, *, limit: int = 0) -> list:
        """`[(score, id)]` descending, ties broken by id. Full scan; nothing prefiltered."""
        qterms = [t for t in dict.fromkeys(tokens(query)) if self.df.get(t)]
        if not qterms:
            return []
        avg = self.avg_len or 1.0
        out = []
        for eid, counts in self.tf.items():
            s = 0.0
            for t in qterms:
                f = counts.get(t)
                if not f:
                    continue
                denom = f + K1 * (1 - B + B * self.length[eid] / avg)
                s += self.idf(t) * (f * (K1 + 1)) / denom
            if s:
                out.append((s, eid))
        out.sort(key=lambda pair: (-pair[0], pair[1]))
        return out[:limit] if limit else out


# One index per corpus revision. Building costs ~10x a query, so it is memoized on a key the
# CALLER supplies -- the ledger head plus anything else that decides which records exist for
# this reader. A None key is never cached: an unkeyable corpus must not freeze the index.
_CACHE: dict = {"key": None, "index": None}
_LOCK = threading.Lock()


def index_for(docs: dict, key):
    """The BM25F index for this corpus revision, built once per key."""
    if key is not None:
        with _LOCK:
            if _CACHE["key"] == key and _CACHE["index"] is not None:
                return _CACHE["index"]
    idx = Bm25F(docs)
    if key is not None:
        with _LOCK:
            _CACHE.update(key=key, index=idx)
    return idx
