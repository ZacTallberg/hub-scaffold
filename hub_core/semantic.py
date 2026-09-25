"""Semantic (dense) retrieval over the board's knowledge — meaning, not shared words.

Every lexical surface finds a record only when the asker already guessed its vocabulary: a
record that says the same thing in different words does not rank badly, it does not exist.
This module puts a vector beside every indexable record and fuses that ranking with BM25F.

THE EMBEDDER IS PLUGGABLE AND OPTIONAL. Vectors come from an OpenAI-compatible embeddings
endpoint named by the environment (read at CALL time, never frozen at import):

    HUB_EMBED_URL            base URL, e.g. http://127.0.0.1:8080/v1  (POST <base>/embeddings)
    HUB_EMBED_MODEL          model name sent in the request (optional)
    HUB_EMBED_TOKEN          bearer token (optional)
    HUB_EMBED_QUERY_PREFIX   prefix for QUERY text   (asymmetric models, e.g. "query: ")
    HUB_EMBED_DOC_PREFIX     prefix for DOCUMENT text (e.g. "passage: ")
    HUB_EMBED_BATCH          max texts per call (default 64)

With none configured, search answers lexically and SAYS so; nothing else changes.

FOUR PROPERTIES THIS MODULE HOLDS, each because its absence was a measured failure:

  1. A WRITE NEVER BLOCKS ON THE EMBEDDER. No write path makes an HTTP call. Work-to-do is
     DERIVED — indexable records minus stored vectors, keyed by a hash of the text — so there
     is no queue to maintain, an edited record re-embeds by itself, and a record written while
     the embedder is down simply has no vector yet.
  2. MISSING VECTORS DEGRADE LOUDLY. Every ranking returns coverage, and `partiality()` renders
     the one block a reader sees when something is missing. There is no zero-vector fallback:
     a zero vector reads downstream as "resembles nothing", a wrong answer in a right shape.
  3. IT DOES NOT CLAIM TO DETECT CONTRADICTION. Cosine says two records are about the same
     thing, not that they disagree. The overlap tagger uses it to put a pair in front of a
     reader; `adjudicate` is what reads both in full.
  4. MEASURED FUSION. Scores are combined convexly (`fuse_convex`, alpha 0.3 lexical), with a
     CSLS hub penalty computed offline. Reciprocal-rank fusion was measured WORSE than either
     channel alone at top-1 and is kept only as the baseline it lost to.

Stdlib only; the vector store is a sqlite sidecar beside the ledger (derived data, never in
the event log).
"""
from __future__ import annotations

import array
import hashlib
import heapq
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from operator import mul
from pathlib import Path

from . import knowledge


def embed_url() -> str:
    return (os.environ.get("HUB_EMBED_URL") or "").strip().rstrip("/")


def configured() -> bool:
    return bool(embed_url())


def _batch_max() -> int:
    try:
        return max(1, min(2048, int(os.environ.get("HUB_EMBED_BATCH") or 64)))
    except ValueError:
        return 64


#: A person is waiting on a query embed; nobody is waiting on a backfill.
QUERY_TIMEOUT_S = 8.0
BACKFILL_TIMEOUT_S = 180.0
#: What the prompt path waits for a focus vector before serving the standing order instead.
FOCUS_WARM_TIMEOUT_S = 2.0


class EmbedUnavailable(RuntimeError):
    """The embedder could not be reached, refused, or answered in a shape we cannot trust."""


def embed(texts, *, is_query: bool = False, timeout: float | None = None) -> list:
    """Unit vectors for `texts`. Raises EmbedUnavailable; never returns zeros or a partial set.

    `is_query` is not decoration: asymmetric models are trained with distinct query and
    document prefixes, and embedding a question with the document prefix puts it in a
    different region than the corpus it searches. Nothing errors when that is wrong — recall
    silently drops — so both call sites pass it explicitly.
    """
    items = [str(t or "") for t in texts]
    if not items:
        return []
    if len(items) > _batch_max():
        raise ValueError("batch of %d exceeds HUB_EMBED_BATCH=%d" % (len(items), _batch_max()))
    if not configured():
        raise EmbedUnavailable("no embedder configured (set HUB_EMBED_URL)")
    prefix = os.environ.get("HUB_EMBED_QUERY_PREFIX" if is_query else "HUB_EMBED_DOC_PREFIX", "")
    body = {"input": [prefix + t for t in items]}
    model = os.environ.get("HUB_EMBED_MODEL", "").strip()
    if model:
        body["model"] = model
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("HUB_EMBED_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(embed_url() + "/embeddings", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    budget = timeout if timeout is not None else (QUERY_TIMEOUT_S if is_query else BACKFILL_TIMEOUT_S)
    try:
        with urllib.request.urlopen(req, timeout=budget) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise EmbedUnavailable("embed HTTP %s" % exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EmbedUnavailable("embed unreachable: %s: %s" % (type(exc).__name__, exc)) from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(items):
        # A short batch is refused whole: a record left silently unembedded while coverage
        # calls it done is exactly the failure property 2 exists to prevent.
        raise EmbedUnavailable("embed returned %s vectors for %d texts"
                               % (len(rows) if isinstance(rows, list) else "no", len(items)))
    rows = sorted(rows, key=lambda r: int(r.get("index", 0)) if isinstance(r, dict) else 0)
    vectors = []
    for r in rows:
        vec = r.get("embedding") if isinstance(r, dict) else None
        if not isinstance(vec, list) or not vec:
            raise EmbedUnavailable("embed returned a row with no vector")
        vectors.append(_unit(vec))
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        raise EmbedUnavailable("embed returned mixed dimensions %s" % sorted(dims))
    return vectors


def _unit(vec) -> list:
    """L2-normalize so a dot product IS cosine. Not trusted to the model: an un-normalized
    corpus would not fail, it would rank by magnitude — longer records first, dressed as
    relevance."""
    out = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in out))
    if norm <= 0:
        raise EmbedUnavailable("embed returned a zero vector")
    return [x / norm for x in out] if abs(norm - 1.0) > 1e-6 else out


_sumprod = getattr(math, "sumprod", None)


def cosine(a, b) -> float:
    """Unit operands, so a dot product. `math.sumprod` keeps the loop AND the multiply in C
    (measured ~3x over sum(map(mul)) and ~10x over a generator on the live corpus size)."""
    if _sumprod is not None:
        return _sumprod(a, b)
    return sum(map(mul, a, b))


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# ── the store: a sidecar beside the ledger, never inside it ──

def connect(hub_dir) -> sqlite3.Connection:
    path = Path(hub_dir) / "semantic.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS vectors (entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                 " text_sha TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,"
                 " model TEXT NOT NULL, at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS hub_terms (entity_id TEXT PRIMARY KEY, rk REAL NOT NULL,"
                 " k INTEGER NOT NULL, at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS focus_vectors (text_sha TEXT PRIMARY KEY,"
                 " dim INTEGER NOT NULL, vec BLOB NOT NULL, at REAL NOT NULL)")
    conn.commit()
    return conn


def _pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> list:
    out = array.array("f")
    out.frombytes(blob)
    # Handed back as a LIST: an array is compact on disk and slower to iterate (it boxes a
    # fresh float per element), measured.
    return list(out)


def ledger_records(state) -> dict:
    """`{entity_id: (kind, text)}` for every ledger record that should carry a vector."""
    superseded = knowledge.superseded_ids(state)
    out = {}
    for ent in (state.get("entities") or {}).values():
        if not knowledge.searchable(ent, superseded):
            continue
        text = knowledge.text_of(ent)
        if text:
            out[str(ent["id"])] = (str(ent.get("type")), text)
    return out


def catalog_records(items) -> dict:
    """`{id: ("cap", text)}` for published catalog capabilities — records served at READ time
    that never enter the ledger. Walking only the ledger left the largest body of "what can I
    already do here" findable by exact name and nothing else."""
    out = {}
    for it in items or []:
        if not isinstance(it, dict) or not it.get("id") or not it.get("name"):
            continue
        text = " ".join(str(it.get(k) or "") for k in ("name", "what", "when", "get")).strip()
        if text:
            out[str(it["id"])] = ("cap", text)
    return out


def indexable(state, catalog_items=None) -> dict:
    """Ledger first: a cap with a real ledger row keeps the row's text; the catalog fills only
    what the ledger never held."""
    out = ledger_records(state)
    for eid, rec in catalog_records(catalog_items).items():
        out.setdefault(eid, rec)
    return out


def stored(conn) -> dict:
    return {row[0]: row[1] for row in conn.execute("SELECT entity_id, text_sha FROM vectors")}


def pending(records: dict, conn) -> list:
    have = stored(conn)
    return [(eid, kind, text) for eid, (kind, text) in records.items() if have.get(eid) != sha_of(text)]


def backfill(records: dict, conn, *, limit: int = 0, model_label: str = "") -> dict:
    """Embed up to `limit` pending records (0 = one batch). Retires vectors of records that are
    no longer indexable FIRST and unconditionally: a cleanup that runs only when there is other
    work to do leaves a superseded rule's vector serving as current knowledge."""
    dead = [eid for eid in stored(conn) if eid not in records]
    if dead:
        conn.executemany("DELETE FROM vectors WHERE entity_id = ?", [(e,) for e in dead])
        conn.commit()
    todo = pending(records, conn)[:max(1, min(limit or _batch_max(), _batch_max()))]
    if not todo:
        return {"embedded": 0, "pending": 0, "retired": len(dead), "ok": True}
    try:
        vectors = embed([text for _e, _k, text in todo], is_query=False)
    except (EmbedUnavailable, ValueError) as exc:
        return {"embedded": 0, "pending": len(pending(records, conn)), "retired": len(dead),
                "ok": False, "reason": str(exc)}
    label = model_label or os.environ.get("HUB_EMBED_MODEL", "") or "embedder"
    now = time.time()
    conn.executemany(
        "INSERT INTO vectors (entity_id, kind, text_sha, dim, vec, model, at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, text_sha=excluded.text_sha,"
        " dim=excluded.dim, vec=excluded.vec, model=excluded.model, at=excluded.at",
        [(eid, kind, sha_of(text), len(vec), _pack(vec), label, now)
         for (eid, kind, text), vec in zip(todo, vectors)])
    conn.commit()
    return {"embedded": len(todo), "pending": len(pending(records, conn)), "retired": len(dead),
            "ok": True}


def silent_types(state) -> dict:
    """`{type: rows}` for indexed types where EVERY live record yielded empty text — a wrong
    field list, not an empty corpus. Surfaced so a schema change announces itself instead of
    quietly shrinking the index."""
    rows, texted = {}, {}
    superseded = knowledge.superseded_ids(state)
    for ent in (state.get("entities") or {}).values():
        if not knowledge.searchable(ent, superseded):
            continue
        kind = str(ent.get("type"))
        rows[kind] = rows.get(kind, 0) + 1
        if knowledge.text_of(ent):
            texted[kind] = texted.get(kind, 0) + 1
    return {k: n for k, n in rows.items() if n and not texted.get(k)}


def unreachable(catalog_items, records: dict, catalog_error: str = "") -> dict:
    """Catalog records the reader can see and the index cannot, or `caps: None` when the
    catalog could not be counted. "I could not read it" and "it is empty" must never arrive as
    the same number; a MISSING denominator is the 0/0-reads-as-all-clear failure in a mask."""
    if catalog_items is None:
        return {"caps": None, "reason": catalog_error or "the capability catalog could not be read"}
    missing, by_id = 0, {}
    for it in catalog_items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        eid = str(it.get("id") or "")
        by_id.setdefault(eid, []).append(str(it["name"]))
        if eid not in records:
            missing += 1
    out = {"caps": missing}
    # Two capabilities that map to one id are ONE record, and the second loses silently.
    collisions = {k: v for k, v in by_id.items() if len(v) > 1}
    if collisions:
        out["id_collisions"] = collisions
    return out


def coverage(state, conn, records: dict, catalog_items=None, catalog_error: str = "") -> dict:
    have = stored(conn)
    fresh = sum(1 for eid, (_k, text) in records.items() if have.get(eid) == sha_of(text))
    out = {"records": len(records), "embedded": fresh, "pending": len(records) - fresh,
           "configured": configured(),
           # Carried beside `records` on purpose: `pending: 0` is a TRUE statement about a
           # denominator that excluded these, and the two only mean anything together.
           "unreachable": unreachable(catalog_items, records, catalog_error)}
    silent = silent_types(state)
    if silent:
        out["silent_types"] = silent
    return out


_COVERAGE_CACHE: dict = {"key": None, "value": None}


def coverage_for(state, conn, records, ledger_key, catalog_items=None, catalog_error="") -> dict:
    """coverage(), reused until the ledger revision or the vector store moves."""
    store = _store_key(conn)
    key = (ledger_key, store, len(catalog_items or ()), catalog_error) \
        if ledger_key is not None and store is not None else None
    if key is not None and _COVERAGE_CACHE["key"] == key:
        return json.loads(json.dumps(_COVERAGE_CACHE["value"]))
    value = coverage(state, conn, records, catalog_items, catalog_error)
    if key is not None:
        _COVERAGE_CACHE.update(key=key, value=json.loads(json.dumps(value)))
    return value


#: At most this many limitation lines: an unbounded notice crowds out the answer it annotates.
PARTIAL_MAX_LINES = 6


def partiality(meta: dict) -> str:
    """The `<memory-partial>` block for a retrieval that could not see everything, or "".

    SILENT WHEN THE ANSWER IS WHOLE: a notice that appears every time is decoration, and a
    reader stops seeing it exactly when it finally matters. Limitations only — the counts stay
    in the metadata, where a program reads them.
    """
    lines = []
    if not meta.get("semantic"):
        reason = str(meta.get("reason") or "").strip()
        lines.append("the embedder did not run — this answer is KEYWORD-ONLY, so a record that "
                     "matches by meaning rather than by wording was not found"
                     + (" (%s)" % reason if reason else ""))
    pend, records = int(meta.get("pending") or 0), int(meta.get("records") or 0)
    if pend:
        lines.append("%d of %d records are stored but not yet embedded, so they are searchable "
                     "only by wording" % (pend, records))
    unreach = meta.get("unreachable") or {}
    caps = unreach.get("caps")
    if caps is None and unreach:
        lines.append("the capability catalog could not be counted, so it is unknown how many "
                     "capabilities are missing from this index"
                     + (" (%s)" % unreach["reason"] if unreach.get("reason") else ""))
    elif caps:
        lines.append("%d published capabilities carry no vector and CANNOT be found by meaning "
                     "here — ask the catalog directly (capabilities.json)" % caps)
    for eid, names in sorted((unreach.get("id_collisions") or {}).items()):
        lines.append("capabilities %s share the id %s, so only one of them can be found here"
                     % (", ".join(names), eid))
    if not lines:
        return ""
    body = "\n".join("- " + line for line in lines[:PARTIAL_MAX_LINES])
    return ('<memory-partial>\nThis recall is INCOMPLETE. What it could not see:\n%s\n'
            'Treat an absence here as unknown, not as evidence that nothing exists.\n'
            '</memory-partial>' % body)


# The whole store, unpacked, memoized on what the stored set IS. `PRAGMA data_version` is per
# connection and a serving path opens one per request, so it cannot key a cross-request memo;
# count + newest write stamp moves on every insert, re-embed and delete.
_VECTOR_CACHE: dict = {"key": None, "vectors": {}}
_HUB_CACHE: dict = {"key": None, "terms": {}}


def _store_key(conn):
    try:
        v = conn.execute("SELECT COUNT(*), COALESCE(MAX(at), 0) FROM vectors").fetchone()
        h = conn.execute("SELECT COUNT(*), COALESCE(MAX(at), 0) FROM hub_terms").fetchone()
        return (v[0], v[1], h[0], h[1])
    except sqlite3.Error:
        return None


def vectors_for(conn, ids=None) -> dict:
    key = _store_key(conn)
    if key is None or _VECTOR_CACHE["key"] != key:
        out = {row[0]: _unpack(row[1]) for row in conn.execute("SELECT entity_id, vec FROM vectors")}
        if key is not None:
            _VECTOR_CACHE.update(key=key, vectors=out)
    else:
        out = _VECTOR_CACHE["vectors"]
    if ids is None:
        return out
    return {eid: out[eid] for eid in ids if eid in out}


# ── ranking ──

#: How much of the combined score comes from the LEXICAL half. Measured on the source
#: instance over five deterministic held-out halves of a few hundred real answered asks: the
#: meeting point of the top-1 plateau (0.1-0.3) and the top-5 plateau (0.3-1.0). Re-measure on
#: YOUR board with tools/retrieval_eval.py before moving it, and move it only to a value that
#: run reported.
CONVEX_ALPHA = 0.3
#: Neighbourhood size for the CSLS hub term (the classic local-scaling default).
K_CSLS = 10
#: Above this many vectors the exact O(N^2) hub-term pass refuses rather than holding the
#: index job for an unbounded time; replace it with an approximate neighbour index, do not
#: raise the number to make the refusal go away.
HUB_TERM_MAX_VECTORS = 10000


def hub_terms(conn) -> dict:
    """`{entity_id: r_k}` — the CSLS local-scaling term, or `{}` if never computed."""
    try:
        key = _store_key(conn)
        if key is not None and _HUB_CACHE["key"] == key:
            return _HUB_CACHE["terms"]
        out = {row[0]: row[1] for row in conn.execute("SELECT entity_id, rk FROM hub_terms")}
        if key is not None:
            _HUB_CACHE.update(key=key, terms=out)
        return out
    except sqlite3.Error:
        return {}


def _neighbour_means(ids: list, vectors: dict, k: int) -> list:
    """For each vector, the mean cosine to its k nearest OTHER vectors. Exact; every pair is
    computed once and offered to both ends' bounded min-heaps."""
    vecs = [vectors[e] for e in ids]
    heaps: list = [[] for _ in vecs]
    push, replace = heapq.heappush, heapq.heapreplace
    for i, vi in enumerate(vecs):
        hi = heaps[i]
        for j in range(i + 1, len(vecs)):
            c = cosine(vi, vecs[j])
            for h in (hi, heaps[j]):
                if len(h) < k:
                    push(h, c)
                elif c > h[0]:
                    replace(h, c)
    return [sum(h) / len(h) if h else 0.0 for h in heaps]


def compute_hub_terms(conn, *, k: int = K_CSLS) -> dict:
    """Rebuild the CSLS hub term for every stored vector — OFF the request path.

    `r_k(d)` is the mean cosine of d to its k nearest other documents. A record in a dense part
    of the space is close to everything and surfaces for questions it has nothing to do with;
    ranking by `2*cos - r_k(d)` penalises that. Because r_k is per DOCUMENT it can change the
    order (a per-query z-score cannot). Measured +1.6 points of top-1 on the source instance.
    """
    vectors = vectors_for(conn)
    if len(vectors) > HUB_TERM_MAX_VECTORS:
        return {"ok": False, "computed": 0, "vectors": len(vectors), "ceiling": True,
                "reason": "%d vectors exceeds HUB_TERM_MAX_VECTORS (%d); the hub term is left as "
                          "it was" % (len(vectors), HUB_TERM_MAX_VECTORS)}
    ids = list(vectors)
    if len(ids) <= k:
        return {"ok": False, "computed": 0, "vectors": len(ids),
                "reason": "only %d vectors; fewer than k=%d neighbours to average" % (len(ids), k)}
    started = time.time()
    means = _neighbour_means(ids, vectors, k)
    conn.execute("DELETE FROM hub_terms")
    conn.executemany("INSERT INTO hub_terms (entity_id, rk, k, at) VALUES (?,?,?,?)",
                     [(eid, means[i], k, started) for i, eid in enumerate(ids)])
    conn.commit()
    return {"ok": True, "computed": len(ids), "vectors": len(ids), "k": k,
            "mean_rk": round(sum(means) / len(means), 4), "seconds": round(time.time() - started, 2)}


def semantic_ranking(query: str, conn, *, candidate_ids=None) -> tuple:
    """`(ranked_ids, meta)` against the stored vectors. Never raises.

    EVERY candidate is scored (no top-N cut): the fusion is a weighted SCORE combination, and a
    record outside a cut would contribute 0.0 to the larger term for a verdict never rendered.
    `meta["scores"]` carries the per-id score for `fuse_convex`; `meta["scaling"]` says whether
    the CSLS term ran, so an unscaled ranking can never be mistaken for a scaled one.
    """
    if not (query or "").strip():
        return [], {"semantic": False, "reason": "empty query"}
    try:
        qvec = embed([query], is_query=True)[0]
    except (EmbedUnavailable, ValueError) as exc:
        return [], {"semantic": False, "reason": str(exc)}
    vectors = vectors_for(conn, candidate_ids)
    if not vectors:
        return [], {"semantic": False, "reason": "no vectors stored yet (run manage.py semantic_index)"}
    dims = {len(v) for v in vectors.values()}
    if dims != {len(qvec)}:
        return [], {"semantic": False,
                    "reason": "the stored vectors (dim %s) were made by a different model than the "
                              "query (dim %d); re-run semantic_index" % (sorted(dims), len(qvec))}
    hubs = hub_terms(conn)
    cosines = {eid: cosine(qvec, vec) for eid, vec in vectors.items()}
    if hubs:
        scored = sorted(((2.0 * c - hubs.get(eid, 0.0), eid) for eid, c in cosines.items()),
                        key=lambda p: (-p[0], p[1]))
    else:
        scored = sorted(((c, eid) for eid, c in cosines.items()), key=lambda p: (-p[0], p[1]))
    return [eid for _s, eid in scored], {
        "semantic": True, "compared": len(vectors), "scaling": "csls" if hubs else "cosine",
        "scores": {eid: s for s, eid in scored},
        # Stays a COSINE under CSLS: 2*cos - r_k is not a similarity and is not bounded by 1.
        "top_similarity": round(max(cosines.values()), 4) if cosines else None}


def fuse_convex(lexical_scores: dict, semantic_scores: dict, *, alpha: float = CONVEX_ALPHA) -> dict:
    """`alpha * lexical + (1 - alpha) * dense`, each normalized WITHIN THE QUERY — for a record
    BOTH channels could read.

    A BM25 score and a cosine are different units; the only thing that makes them addable is
    rescaling both against the range this query produced (lexical by its max, dense min-max).
    A record with a vector that shares no word with the query scores 0 lexically and keeps its
    dense share.

    A record WITHOUT a vector (written since the last `semantic_index` run) is scored by the
    channel that could read it, at full weight: its score is its normalized lexical score. A
    missing vector is an UNKNOWN, not a zero — scoring it as a zero capped every such record at
    `alpha` (0.3), below any embedded record with a middling cosine, so the lesson written a
    minute ago ranked 33rd for its own exact words while records that contained neither word
    ranked above it. Renormalizing alpha to 1 for that record keeps the scale identical (both
    forms live in 0..1), so an unvectored exact match ties the best record both channels agree
    on, and an unvectored weak match stays below it.

    `semantic_scores` holds a score for EVERY stored vector among the candidates (no top-N cut
    upstream), so absence from it means no vector — never "ranked too low to return".

    Measured on the source instance (266 real answered asks, held-out halves, fully indexed):
    BM25F alone 69.5% top-1 / 94.5% top-5; dense alone 71.9 / 89.8; max-reciprocal-rank fusion
    61.7 / 96.1 — worse at top-1 than either of its own inputs, because a max of ranks is blind
    to the two channels AGREEING, which is the signal that separates a top-1 from a top-5; this
    convex form 75.6 / 97.4, and 77.2 / 97.7 with the CSLS term.
    """
    if not semantic_scores:
        return dict(lexical_scores)
    if not lexical_scores:
        return dict(semantic_scores)
    top = max(lexical_scores.values()) or 1.0
    lo, hi = min(semantic_scores.values()), max(semantic_scores.values())
    span = (hi - lo) or 1.0
    out = {}
    for eid in set(lexical_scores) | set(semantic_scores):
        lex = lexical_scores.get(eid, 0.0) / top
        dense = semantic_scores.get(eid)
        if dense is None:
            out[eid] = lex                      # no vector: the lexical channel at full weight
        else:
            out[eid] = alpha * lex + (1.0 - alpha) * (dense - lo) / span
    return out


def fuse_rrf_max(lexical_ids, semantic_ids, *, k: int = 60) -> dict:
    """Max reciprocal-rank fusion — the MEASURED LOSER, kept only so the evaluation harness has
    the baseline to re-prove the winner against. Never call it from a request path."""
    out = {}
    for ids_ in (lexical_ids, semantic_ids):
        for rank, eid in enumerate(ids_):
            out[eid] = max(out.get(eid, 0.0), 1.0 / (k + rank + 1))
    return out


# ── the focus vector: what a console is doing, embedded once per distinct gist ──

def focus_key(text: str) -> str:
    return sha_of(" ".join(str(text or "").split()).lower())


def cached_focus(conn, text: str):
    if not (text or "").strip():
        return None
    try:
        row = conn.execute("SELECT vec FROM focus_vectors WHERE text_sha = ?",
                           (focus_key(text),)).fetchone()
    except sqlite3.Error:
        return None
    return _unpack(row[0]) if row else None


#: The embedder circuit: after a TRANSPORT failure (unreachable, timed out, refused) the
#: prompt-path and overlap-path embeds are skipped for this long. A down embedder used to be
#: asked on every uncached focus and every overlap lookup, each paying its full timeout before
#: falling back — seconds per prompt against a hook's budget, and minutes per adjudication pass
#: spent waiting on the same outage once per record. A cached focus still ranks meaningfully,
#: because that needs no embed.
EMBED_RETRY_S = 60
_EMBED_DOWN: dict = {"until": 0.0, "why": ""}


def embed_circuit() -> str:
    """"" when the embedder may be asked; otherwise why not, and for how much longer."""
    left = _EMBED_DOWN["until"] - time.time()
    if left <= 0:
        return ""
    return "embed unreachable (not retried for %d s; last: %s)" % (int(left) + 1, _EMBED_DOWN["why"])


def _trip_if_down(reason: str) -> None:
    """Open the circuit on a transport failure only: an HTTP error or a shape complaint is
    answered per call, and the next text may well succeed."""
    if str(reason or "").startswith("embed unreachable"):
        _EMBED_DOWN.update(until=time.time() + EMBED_RETRY_S, why=str(reason)[:160])


def ensure_focus(conn, text: str, *, timeout: float = FOCUS_WARM_TIMEOUT_S) -> tuple:
    """Embed and cache this focus if needed — ON the prompt path, bounded.

    An off-path warm was tried first and could never work: a console's gist is recaptured
    every prompt, so the cache key changes every prompt and any asynchronous warm loses the
    race. The bounded timeout plus the stated fallback keep that honest: a slow embedder costs
    one bounded wait and then the standing order, with the reason attached.
    """
    if not (text or "").strip():
        return False, {"cached": False, "reason": "no focus"}
    if cached_focus(conn, text) is not None:
        return True, {"cached": True, "hit": True}
    down = embed_circuit()
    if down:
        return False, {"cached": False, "reason": down, "circuit": True}
    try:
        vec = embed([text], is_query=True, timeout=timeout)[0]
    except (EmbedUnavailable, ValueError) as exc:
        _trip_if_down(str(exc))
        return False, {"cached": False, "reason": str(exc)}
    try:
        conn.execute("INSERT INTO focus_vectors (text_sha, dim, vec, at) VALUES (?,?,?,?)"
                     " ON CONFLICT(text_sha) DO UPDATE SET vec=excluded.vec, at=excluded.at",
                     (focus_key(text), len(vec), _pack(vec), time.time()))
        conn.commit()
    except sqlite3.Error as exc:
        return False, {"cached": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
    return True, {"cached": True, "hit": False}


_RANK_MEMO: dict = {}
_RANK_LOCK = threading.Lock()


def rank_by_focus(ids, conn, focus_text) -> tuple:
    """Reorder `ids` by relevance to a focus. `(ordered, meta)`. Never embeds, never raises.

    Memoized on (focus, stored set, candidates): several consoles of one person share a gist,
    and each paid a full scan of dot products under the GIL. A record with no vector keeps its
    standing position AFTER the ranked ones — not-yet-embedded must never mean invisible.
    """
    ids = list(ids)
    if not ids:
        return ids, {"ranked": False, "reason": "nothing to rank"}
    store = _store_key(conn)
    memo = (focus_key(focus_text), store,
            hashlib.sha256("\x1f".join(ids).encode("utf-8")).hexdigest()) if store else None
    if memo is not None:
        with _RANK_LOCK:
            hit = _RANK_MEMO.get(memo)
        if hit is not None:
            return list(hit[0]), dict(hit[1])
    fvec = cached_focus(conn, focus_text)
    if fvec is None:
        return ids, {"ranked": False, "reason": "no cached vector for this focus"}
    vectors = vectors_for(conn, ids)
    vectors = {e: v for e, v in vectors.items() if len(v) == len(fvec)}
    if not vectors:
        return ids, {"ranked": False, "reason": "no vectors stored for these records"}
    scored = sorted(((cosine(fvec, vectors[e]), e) for e in ids if e in vectors),
                    key=lambda p: (-p[0], p[1]))
    ordered = [e for _s, e in scored] + [e for e in ids if e not in vectors]
    meta = {"ranked": True, "compared": len(vectors), "unvectored": len(ids) - len(vectors),
            "top_similarity": round(scored[0][0], 4) if scored else None}
    if memo is not None:
        with _RANK_LOCK:
            if len(_RANK_MEMO) >= 256:
                _RANK_MEMO.clear()
            _RANK_MEMO[memo] = (list(ordered), dict(meta))
    return ordered, meta


#: What a caller waits for the semantic overlap half when it DOES run inline (the adjudication
#: pass, never a write): the tag is advisory and its absence is declared.
TAG_TIMEOUT_S = 4.0


def related(text: str, conn, *, z_min: float, cos_min: float = 0.0, exclude=(), limit: int = 4,
            timeout: float | None = TAG_TIMEOUT_S) -> tuple:
    """Records whose MEANING stands out as close to `text`, as `(hits, meta)`. Never raises.

    The criterion is RELATIVE: a candidate qualifies by being an outlier against this text's
    similarity to the whole corpus, `z = (sim - mean) / stdev`. "Unusually close for this
    corpus" survives a model swap and a corpus that doubles; an absolute cosine does not.
    `cos_min` is only a floor against a corpus of near-identical records.
    """
    down = embed_circuit()
    if down:
        return [], {"semantic": False, "reason": down, "circuit": True}
    try:
        qvec = embed([text], is_query=False, timeout=timeout)[0]
    except (EmbedUnavailable, ValueError) as exc:
        _trip_if_down(str(exc))
        return [], {"semantic": False, "reason": str(exc)}
    skip = set(exclude)
    sims = [(cosine(qvec, vec), eid) for eid, vec in vectors_for(conn).items()
            if eid not in skip and len(vec) == len(qvec)]
    if len(sims) < 8:
        return [], {"semantic": False,
                    "reason": "only %d embedded records; too few to judge an outlier" % len(sims)}
    values = [s for s, _ in sims]
    mean = sum(values) / len(values)
    stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    if stdev <= 1e-9:
        return [], {"semantic": False, "reason": "no spread in similarities"}
    hits = [{"id": eid, "similarity": round(sim, 4), "z": round((sim - mean) / stdev, 2)}
            for sim, eid in sims if (sim - mean) / stdev >= z_min and sim >= cos_min]
    hits.sort(key=lambda h: (-h["z"], h["id"]))
    return hits[:limit], {"semantic": True, "z_min": z_min, "cos_min": cos_min,
                          "compared": len(sims), "mean": round(mean, 4), "stdev": round(stdev, 4)}
