"""Knowledge READ API: ranked search, related records, the per-prompt memory index, the
mirror feed, and the published capability catalog.

Every surface here reads ONE vocabulary (hub_core.knowledge: what is knowledge, what is
retired, what text a record is found by) and ONE state fold (the memoized read model the board
itself serves), keyed on the ledger HEAD. A cache key that can be None was the defect this
layout fixes on the instance it came from: search was keyed on a field the snapshot did not
carry, so the corpus froze at the first query and a lesson written afterwards could not be
found until a restart.

Unauthenticated reads, like the rest of /hub. Nothing here writes to the ledger; the vector
store is derived data beside it.
"""
import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from hub_core import bm25, capability_catalog, errorlog, inbox as inbox_core, knowledge, lexical, semantic
from hub_core import staleness
from hub_core.canonical import content_hash
from hub_core.validate import validate

from . import hub_app


def _state_and_key():
    """(state, head_key) from the board's own memoized fold, and the head it was folded at."""
    from .hub_api import _projected
    s = hub_app.store()
    try:
        cur = s.latest_cursor() or {}
        _events, state = _projected(s, cur)
    finally:
        s.close()
    return state, (str(hub_app.HUB_DIR), int(cur.get("seq") or 0), str(cur.get("hash") or ""))


# ── the published capability catalog ──

def _validate_cap_row(row):
    return validate(row, "cap", hub_app.registry())


def catalog(wait=False):
    """(items_or_None, publication_meta). None means the catalog could not be read or is not
    configured — never an empty list standing in for an unknown."""
    try:
        publication, meta = capability_catalog.snapshot(
            hub_app.HUB_DIR, project_key=hub_app.PROJECT_KEY,
            validate_row=_validate_cap_row, wait=wait)
    except Exception as exc:                                   # noqa: BLE001 - never 500 a read
        return None, {"state": "none", "commit": "", "error": "%s: %s" % (type(exc).__name__, exc)}
    if not publication:
        return None, meta
    return capability_catalog.items(publication, hub_app.PROJECT_KEY), meta


def _catalog_for_index():
    """What the index should reach: catalog items when configured, [] when deliberately
    unconfigured (nothing is missing), None when configured and unreadable."""
    items, meta = catalog()
    if items is None and meta.get("state") == "none" and not capability_catalog.config()["repo"]:
        return [], ""
    return items, meta.get("error") or ""


@require_GET
def capabilities_json(request):
    """The capability catalog — what an agent can already do here, and when to reach for it.

    Ledger `cap` entities merged with the published catalog (one capability under two spellings
    counted once). Filters: ?kind=, ?q= (whole phrase first, then every word or its stem).
    Carries an ETag over the content INCLUDING the publication commit, so a republished catalog
    is never answered 304 from a reader's cache."""
    state, _key = _state_and_key()
    caps = [e for e in (state.get("entities") or {}).values()
            if isinstance(e, dict) and e.get("type") == "cap" and not knowledge.is_dead(e)]
    items, meta = catalog()
    publication = {"state": meta.get("state"), "commit": meta.get("commit"),
                   "checked_at": meta.get("checked_at"), "error": meta.get("error") or ""}
    rows = capability_catalog.merged(items or [], caps)
    total = len(rows)
    kinds = {}
    for r in rows:
        kinds[r.get("kind") or "capability"] = kinds.get(r.get("kind") or "capability", 0) + 1
    kind = (request.GET.get("kind") or "").strip().lower()
    if kind:
        rows = [r for r in rows if str(r.get("kind") or "").lower() == kind]
    q = (request.GET.get("q") or "").strip()[:200]
    if q:
        rows = capability_catalog.matches(rows, q)
    body = {"data": rows,
            "metadata": {"count": len(rows), "total": total, "kinds": kinds,
                         "publication": publication,
                         "sources": {"catalog": sum(1 for r in rows if r.get("source") == "catalog"),
                                     "ledger": sum(1 for r in rows if r.get("source") == "ledger")}}}
    tag = '"%s"' % content_hash(body)
    if request.headers.get("If-None-Match") == tag:
        resp = HttpResponse(status=304)
    else:
        resp = JsonResponse(body)
    resp["ETag"] = tag
    return resp


# ── search: BM25F + dense, fused convexly, with the partiality notice ──

_SEARCH_STOP = {"the", "a", "an", "is", "of", "to", "and", "or", "in", "on", "for", "it",
                "with", "at", "this", "that", "was", "are"}

#: The searchable corpus, built once per (ledger head, catalog revision). The rows are
#: templates and never mutated: a per-query score rides a copy of only the rows returned, so
#: one request's ranking can never sit in the next request's corpus.
_CORPUS = {"key": None, "rows": None, "docs": None}
_CORPUS_LOCK = threading.Lock()


def _corpus(state, key, cat_items):
    ckey = (key, content_hash(cat_items or []) if cat_items else "")
    with _CORPUS_LOCK:
        if _CORPUS["key"] == ckey and _CORPUS["rows"] is not None:
            return _CORPUS["rows"], _CORPUS["docs"], ckey
    rows, docs = {}, {}
    superseded = knowledge.superseded_ids(state)
    # A ledger cap the published catalog already names (under any spelling) is the SAME
    # capability: the catalog row stands for it here exactly as it does on capabilities.json.
    replaced = capability_catalog.replaced_by_catalog(
        cat_items or [], [e for e in (state.get("entities") or {}).values()
                          if isinstance(e, dict) and e.get("type") == "cap"])
    for ent in (state.get("entities") or {}).values():
        if isinstance(ent, dict) and ent.get("id") in replaced:
            continue
        # THE SAME CORPUS ON BOTH SIDES: chatter and retired records are out of the lexical half
        # exactly as they are out of the dense half — two halves of one ranking must not
        # disagree about what exists.
        if not knowledge.searchable(ent, superseded):
            continue
        title = knowledge.title_of(ent)
        # Fielded: the title and tags have their own weighted fields, so the body field holds
        # only what the record carries BESIDE them — repeating the title there would weigh it
        # twice and make the declared field weights a fiction.
        body = knowledge.body_of(ent)
        kind = knowledge.knowledge_kind(ent)
        rows[ent["id"]] = {"id": ent["id"], "type": ent.get("type"), "title": title[:200],
                           "kind": kind or ent.get("type"),
                           "status": ent.get("status") or ent.get("maturity") or "",
                           "excerpt": body[:400]}
        if kind:
            rows[ent["id"]]["_label"] = knowledge.label_inputs(ent, kind)
        docs[ent["id"]] = {"title": title, "body": body,
                           "tags": " ".join(str(t) for t in (ent.get("tags") or []))}
    for it in cat_items or []:
        rows[it["id"]] = {"id": it["id"], "type": "cap", "title": it["name"][:200],
                          "kind": it.get("kind") or "capability", "status": "catalog",
                          "excerpt": str(it.get("what") or "")[:400], "source": "catalog"}
        docs[it["id"]] = {"title": it["name"], "body": " ".join(str(it.get(k) or "") for k in ("what", "when", "get")),
                          "tags": str(it.get("kind") or "")}
    with _CORPUS_LOCK:
        _CORPUS.update(key=ckey, rows=rows, docs=docs)
    return rows, docs, ckey


def _records_for_index(state, cat_items):
    return semantic.indexable(state, cat_items)


def _semantic_half(q, state, key, candidate_ids, cat_items, cat_error):
    """(ranked_ids, meta) from the vector store, or ([], why-not). Never raises: search must
    answer with the embedder down, the backfill never run, and the store absent — and must
    SAY which halves ran."""
    try:
        conn = semantic.connect(hub_app.HUB_DIR)
        try:
            mark = time.perf_counter()
            ids, meta = semantic.semantic_ranking(q, conn, candidate_ids=candidate_ids)
            ranked = time.perf_counter()
            meta.update(semantic.coverage_for(state, conn, _records_for_index(state, cat_items), key,
                                              cat_items, cat_error))
            meta["_timing"] = {"ranking": ranked - mark, "coverage": time.perf_counter() - ranked}
            return ids, meta
        finally:
            conn.close()
    except Exception as exc:                                   # noqa: BLE001
        return [], {"semantic": False, "reason": "%s: %s" % (type(exc).__name__, exc)}


@require_GET
def search_json(request):
    """Ranked search over the board's knowledge — the PULL half of "push pointers, pull content".

    BM25F (title > tags > body, IDF + length-normalized) over every live, non-chatter record,
    fused convexly with dense retrieval when an embedder is configured. `metadata` says which
    halves ran (`fusion`), what the dense half could not see (`partial`, present ONLY when
    something is missing), and where the time went (`timing_ms`, per stage).

    ?q= (<=200 chars) · ?limit= (1-50) · ?mode=fused|lexical|semantic (measurement) ·
    ?alpha=0..1 (measurement; the served default is the measured CONVEX_ALPHA)."""
    started = time.perf_counter()
    q = (request.GET.get("q") or "").strip().lower()[:200]
    try:
        limit = max(1, min(50, int(request.GET.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    mode = (request.GET.get("mode") or "fused").strip().lower()
    if mode not in ("fused", "lexical", "semantic"):
        mode = "fused"
    try:
        alpha = float(request.GET["alpha"]) if request.GET.get("alpha") else semantic.CONVEX_ALPHA
        alpha = min(1.0, max(0.0, alpha))
    except ValueError:
        alpha = semantic.CONVEX_ALPHA
    if not q:
        return JsonResponse({"data": [], "metadata": {"q": "", "msg": "pass ?q="}})
    terms = [t for t in re.split(r"[^a-z0-9._-]+", q) if t and t not in _SEARCH_STOP][:24]
    timing = {}
    state, key = _state_and_key()
    timing["state"] = time.perf_counter() - started
    mark = time.perf_counter()
    cat_items, cat_error = _catalog_for_index()
    rows, docs, ckey = _corpus(state, key, cat_items)
    timing["corpus"] = time.perf_counter() - mark

    mark = time.perf_counter()
    lexical_scores = {}
    if mode != "semantic":
        for score, eid in bm25.index_for(docs, ckey).score(q):
            lexical_scores[eid] = round(score, 4)
    timing["lexical"] = time.perf_counter() - mark

    mark = time.perf_counter()
    if mode == "lexical":
        dense_ids, retrieval = [], {"semantic": False, "reason": "mode=lexical requested"}
    else:
        dense_ids, retrieval = _semantic_half(q, state, key, list(rows), cat_items, cat_error)
    timing["semantic"] = time.perf_counter() - mark
    for part, secs in (retrieval.pop("_timing", None) or {}).items():
        timing["semantic." + part] = secs

    dense_scores = retrieval.pop("scores", None) or {}
    if mode == "semantic":
        fused, fusion = dense_scores, "semantic only" if dense_ids else "semantic unavailable"
    elif dense_ids:
        fused, fusion = semantic.fuse_convex(lexical_scores, dense_scores, alpha=alpha), "convex alpha=%s" % alpha
    else:
        fused, fusion = lexical_scores, "lexical only"
    # TIES BROKEN BY ID, never by which channel inserted first: rank 1 is then a fact about
    # the scores, reproducible across runs.
    ordered = sorted(fused, key=lambda eid: (-fused[eid], eid))
    # `matched` = records that share a WORD with the query; `scored` = every record either half
    # ranked (the denominator of what is shown). With a dense half they differ, and a count that
    # used `matched` as the denominator printed "20 shown of 2".
    metadata = {"q": q, "terms": terms, "matched": len(lexical_scores), "scored": len(fused),
                "candidates": len(rows),
                "mode": mode, "fusion": fusion, "retrieval": retrieval}
    partial = semantic.partiality(retrieval) if mode != "lexical" else ""
    if partial:
        metadata["partial"] = partial
    timing["total"] = time.perf_counter() - started
    metadata["timing_ms"] = {k: round(v * 1000, 1) for k, v in timing.items()}
    now = datetime.now(timezone.utc)

    def _hit(eid):
        # A knowledge hit carries its rendered label, never the raw inputs.
        row = {k: v for k, v in rows[eid].items() if k != "_label"}
        label = staleness.render_inputs(rows[eid].get("_label"), now=now)
        if label:
            row["label"] = label
        row.update(score=round(fused[eid], 4), lexical=lexical_scores.get(eid, 0.0))
        return row
    data = [_hit(eid) for eid in ordered[:limit] if eid in rows]
    if not data:
        # "NOTHING MATCHES" IS THE MOST DANGEROUS LINE A SEARCH PRINTS. From an index that could
        # only read wording it is a fact about the asker's VOCABULARY, not about what the board
        # knows — and it reads as permission to go and solve something already solved.
        metadata["hint"] = ("no record matched these words" +
                            (" and meaning-based retrieval did not run" if fusion == "lexical only" else "") +
                            " — ask again in different words (the symptom, the system, the error text) "
                            "before concluding the board has not met this")
    return JsonResponse({"data": data, "metadata": metadata})


# ── related: what a record may duplicate, correct or contradict ──

@require_GET
def related_json(request):
    """Records close to ?id= (or ?text=), by weighted vocabulary AND by meaning, each basis
    declared when it could not run. The adjudication pass fills a lesson's deferred semantic
    half from here, and `recall` shows it — computed on the hub, which holds the vectors."""
    eid = (request.GET.get("id") or "").strip()
    text = (request.GET.get("text") or "").strip()[:4000]
    state, _key = _state_and_key()
    ent = (state.get("entities") or {}).get(eid) if eid else None
    if eid and not ent:
        return JsonResponse({"errors": [{"code": "not_found", "msg": eid}]}, status=404)
    if ent:
        text = " ".join(p for p in (knowledge.title_of(ent), str(ent.get("body_md") or "")) if p)
    if not text:
        return JsonResponse({"errors": [{"code": "need_id_or_text"}]}, status=400)
    rule = knowledge.title_of(ent) if ent else text
    lex_hits, lex_meta = lexical.neighbours(rule, state, ignore_id=eid)
    try:
        conn = semantic.connect(hub_app.HUB_DIR)
        try:
            sem_hits, sem_meta = semantic.related(text, conn, z_min=RELATED_Z, cos_min=RELATED_COS_MIN,
                                                  exclude=(eid,) if eid else ())
        finally:
            conn.close()
    except Exception as exc:                                   # noqa: BLE001
        sem_hits, sem_meta = [], {"semantic": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
    ents = state.get("entities") or {}
    for h in sem_hits:
        other = ents.get(h["id"]) or {}
        h["kind"] = knowledge.knowledge_kind(other) or other.get("type") or ""
        h["title"] = knowledge.title_of(other)[:160] if other else ""
    return JsonResponse({"data": {"lexical": lex_hits, "semantic": sem_hits},
                         "metadata": {"id": eid or None, "lexical": lex_meta, "semantic": sem_meta}})


#: The semantic basis's net: "unusually close for this corpus" (z) above a cosine floor. It
#: TAGS, it never refuses, so a too-wide net costs a reader one glance.
RELATED_Z = 3.0
RELATED_COS_MIN = 0.55


# ── the per-prompt memory index ──

MEMORY_CAP_MAX = 200
MEMORY_FULL_MAX = 40
MEMORY_RULE_CHARS = 900
MEMORY_WHY_CHARS = 600

#: The standing corpus is the same for every prompt at one ledger head; only the RANK depends
#: on the console. Its own name and its own lock: sharing a cache dict with search let one
#: memo's clear() delete the other's slot and every search raised KeyError until a restart.
_PROMPT_CORPUS: dict = {}
_PROMPT_CORPUS_LOCK = threading.Lock()


_PROMPT_LEX: dict = {}


#: Identifiers a console's focus carries that are never shared vocabulary: board ids
#: (<project>:<type>:<local>), problem ids, shas and uuids, run counts, long numbers. Left in,
#: each is a query term no record contains, and a record answering a board TEMPLATE's words
#: ("holds p-...: <service>: <job> failing on main (N runs)") outranked the relevant ones.
_FOCUS_IDS = re.compile(
    r"\b[a-z0-9][a-z0-9_-]*:[a-z]+:[a-z0-9][a-z0-9._-]*\b"
    r"|\bp-[0-9a-f]{8,}\b"
    r"|\b[0-9a-f]{8,}(?:-[0-9a-f]{4,})*\b"
    r"|\(\s*\d+\s+runs?\s*\)"
    r"|\b\d{3,}\b", re.I)


def lexical_query(focus) -> str:
    """The focus with identifiers removed, for WORD ranking only (meaning-based ranking reads
    the whole focus and is not fooled by an id)."""
    return " ".join(_FOCUS_IDS.sub(" ", str(focus or "")).split())


def _rank_by_wording(rows, focus, key, why_not):
    """The focus ranked by its WORDS when its meaning could not be read.

    Without an embedder (the scaffold's default) a focus used to change nothing: every prompt
    got the same standing order, so "ranked for what this console is doing" was unreachable
    out of the box. BM25F over each row's rule and story puts the rows that share the focus's
    vocabulary first — then the rest in standing order, so nothing drops out — and says it
    ranked by wording, so a keyword ranking is never mistaken for a semantic one. Its own
    memo, never the search index's single slot: a prompt must not evict a search corpus.

    The index is keyed on WHICH records it holds, not on the ledger head (which moves on every
    write and rebuilt the index on almost every prompt). Each matching row carries
    `lexical_score` in 0..1: its BM25F against THIS query's own ceiling — the score a record
    matching every query term fully would reach, counting terms no record contains (they are
    exactly the part of the question the corpus does not answer). Scores relative to the best
    hit came back flat (1.0 / 0.99 / 0.94 ...) and a cut on them cut nothing."""
    ids_key = hashlib.sha256(chr(31).join(sorted(r["id"] for r in rows)).encode("utf-8")).hexdigest()
    with _PROMPT_CORPUS_LOCK:
        idx = _PROMPT_LEX.get(ids_key)
    if idx is None:
        idx = bm25.Bm25F({r["id"]: {"title": r.get("title") or "",
                                    "body": "%s %s" % (r.get("rule") or "", r.get("why") or "")}
                          for r in rows})
        with _PROMPT_CORPUS_LOCK:
            if len(_PROMPT_LEX) >= 16:
                _PROMPT_LEX.clear()
            _PROMPT_LEX[ids_key] = idx
    query = lexical_query(focus)
    scored = idx.score(query)
    reason = why_not.get("reason") or "meaning-based ranking unavailable"
    if not scored:
        return rows, {"ranked": False, "reason": "%s; and no record shares a word with the focus" % reason}
    qterms = list(dict.fromkeys(bm25.tokens(query)))
    ceiling = sum(idx.idf(t) * (bm25.K1 + 1) for t in qterms) or 1.0
    lex = {eid: round(min(1.0, sc / ceiling), 4) for sc, eid in scored}
    at = {r["id"]: r for r in rows}
    ordered = ([dict(at[eid], _lex=lex[eid]) for _sc, eid in scored if eid in at]
               + [r for r in rows if r["id"] not in lex])
    return ordered, {"ranked": True, "by": "wording", "matched": len(scored),
                     "top_lexical_score": lex[scored[0][1]], "semantic_reason": reason,
                     "reason": "ranked by the focus's WORDS, not its meaning (%s)" % reason}


#: A restated record ranks above an equally close one learned once: +REINFORCED_BOOST per
#: restatement, capped at REINFORCED_CAP. `score` stays the raw cosine a reader cuts on.
REINFORCED_BOOST = 0.01
REINFORCED_CAP = 5


def memory_index(state, key, *, cap=40, focus="", full=0, peer=""):
    """(rows, total, rank_meta): the knowledge index, ranked by the console's focus when it can
    be, in standing order with the stated reason when it cannot."""
    with _PROMPT_CORPUS_LOCK:
        rows = _PROMPT_CORPUS.get(key)
    if rows is None:
        rows = knowledge.memory_rows(state)
        with _PROMPT_CORPUS_LOCK:
            if len(_PROMPT_CORPUS) >= 16:
                _PROMPT_CORPUS.clear()
            _PROMPT_CORPUS[key] = rows
    rows = list(rows)
    total = len(rows)
    meta = {"ranked": False, "reason": "no focus supplied"}
    if focus:
        try:
            conn = semantic.connect(hub_app.HUB_DIR)
            try:
                ok, warm = semantic.ensure_focus(conn, focus)
                ordered, meta = semantic.rank_by_focus([r["id"] for r in rows], conn, focus)
            finally:
                conn.close()
            meta = dict(meta)
            scores = meta.pop("scores", None) or {}
            if meta.get("ranked"):
                at = {r["id"]: r for r in rows}
                rows = [dict(at[i], _score=scores.get(i)) for i in ordered if i in at]
                boosted = [(r["_score"] if r.get("_score") is not None else -1.0)
                           + REINFORCED_BOOST * min(int(r.get("reinforced") or 0), REINFORCED_CAP)
                           for r in rows]
                rows = [r for _b, _n, r in sorted(zip(boosted, range(len(rows)), rows),
                                                   key=lambda t: (-t[0], t[1]))]
                # Whether THIS request paid for the embed or read a warm cache.
                meta["focus_cache"] = "hit" if warm.get("hit") else "embedded-now"
            elif not ok and warm.get("reason"):
                meta["reason"] = "could not embed this console's focus: %s" % warm["reason"]
        except Exception as exc:                               # noqa: BLE001 - the ORDER may degrade, never the block
            meta = {"ranked": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        if not meta.get("ranked"):
            rows, meta = _rank_by_wording(rows, focus, key, meta)
    # PEER SIMILARITY: how close a peer's message is to what THIS console is doing, so a
    # client re-ranks only when the two diverge. Same embedder, same bounded wait; any failure
    # is None with its reason, never a guess.
    peer = " ".join(str(peer or "").split())[:400]
    if peer:
        if not focus:
            meta["peer_similarity"], meta["peer_reason"] = None, "no focus to compare against"
        else:
            try:
                conn = semantic.connect(hub_app.HUB_DIR)
                try:
                    sim, why = semantic.pair_similarity(conn, focus, peer)
                finally:
                    conn.close()
            except Exception as exc:                           # noqa: BLE001
                sim, why = None, "%s: %s" % (type(exc).__name__, exc)
            meta["peer_similarity"] = sim
            if why:
                meta["peer_reason"] = why
    out = []
    now = datetime.now(timezone.utc)
    for i, r in enumerate(rows[:cap]):
        row = {k: r[k] for k in ("id", "type", "title", "tier", "verified_as_of", "verify",
                                 "reinforced") if r.get(k)}
        # The record's ONE label (STATE / UNVERIFIED / CHECK FAILED), identical on search and the
        # feed; the prompt block prints it instead of a bare date.
        label = staleness.render_inputs(r.get("_label"), now=now)
        if label:
            row["label"] = label
        if r.get("_score") is not None:
            row["score"] = r["_score"]
        if r.get("_lex") is not None:
            row["lexical_score"] = r["_lex"]
        if i < full:
            rule = knowledge.clip(r.get("rule"), MEMORY_RULE_CHARS)
            why = knowledge.clip(r.get("why"), MEMORY_WHY_CHARS)
            if rule:
                row["rule"] = rule
            if why:
                row["why"] = why
        out.append(row)
    return out, total, meta


#: How long one agent's live block may be reused: several consoles and a refresh loop ask the
#: same question seconds apart, and nothing it reads is hidden longer than this.
LIVE_REUSE_S = 10.0
_LIVE_MEMO: dict = {}
_LIVE_LOCK = threading.Lock()


def _live(state, key, agent):
    try:
        memo_key = (agent, key, repr(errorlog.stamp(hub_app.HUB_DIR)))
    except Exception:                                          # noqa: BLE001
        memo_key = None
    now = time.monotonic()
    if memo_key is not None:
        with _LIVE_LOCK:
            hit = _LIVE_MEMO.get(memo_key)
        if hit is not None and now - hit[0] < LIVE_REUSE_S:
            return copy.deepcopy(hit[1])
    out = {}
    try:
        from .hub_api import _errors_block, _operator_agent
        _rows, emeta, _unclaimed = _errors_block()
        out["errors_unclaimed"] = int(emeta.get("unclaimed") or 0)
        if agent:
            inbox = inbox_core.snapshot(state, agent, _operator_agent())
            out["inbox"] = {"count": len(inbox.get("items") or []),
                            "fingerprint": inbox.get("fingerprint"),
                            "items": [{"id": i.get("id"), "kind": i.get("kind"), "title": i.get("title")}
                                      for i in (inbox.get("items") or [])[:5]]}
    except Exception as exc:                                   # noqa: BLE001 - never lose the knowledge block
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    if memo_key is not None:
        with _LIVE_LOCK:
            if len(_LIVE_MEMO) >= 256:
                _LIVE_MEMO.clear()
            _LIVE_MEMO[memo_key] = (now, copy.deepcopy(out))
    return out


@require_GET
def guidance_json(request):
    """What a session should have in front of it on THIS prompt, in one fetch: the knowledge
    index ranked by the console's live focus, and the small live block (what is addressed to
    the agent, how many operational errors are unclaimed).

    ?focus=<what this console is doing> · ?agent= · ?memory_cap= (<=200 ranked titles) ·
    ?memory_full= (<=40 of them carry their RULE and WHY). `memory_rank` says HOW the index was
    ordered, so a client can say so: an index that silently reverts from relevance to recency
    looks exactly like one that ranked well."""
    agent = (request.GET.get("agent") or "").strip().lower()[:64]
    focus = (request.GET.get("focus") or "").strip()[:400]
    try:
        cap = max(1, min(int(request.GET.get("memory_cap") or 40), MEMORY_CAP_MAX))
        full = max(0, min(int(request.GET.get("memory_full") or 0), MEMORY_FULL_MAX))
    except ValueError:
        cap, full = 40, 0
    state, key = _state_and_key()
    # ?peer=<a peer message's text> answers memory_rank.peer_similarity (cosine against focus).
    peer = (request.GET.get("peer") or "").strip()[:400]
    memory, total, rank = memory_index(state, key, cap=cap, focus=focus, full=full, peer=peer)
    return JsonResponse({"memory": memory, "memory_total": total, "memory_rank": rank,
                         "live": _live(state, key, agent),
                         "metadata": {"agent": agent or None, "head": {"seq": key[1], "hash": key[2]}}})


# ── knowledge attached to an EVENT ──
#
# The per-prompt index ranks the corpus by what a console is doing. An EVENT -- claiming a
# problem, starting a task, a CI failure landing, asking doctor why a service is not healthy --
# has a sharper subject than any prompt, and it is exactly when a record the board already paid
# for saves the next hour. So those responses carry the top records for THAT subject, by the
# same cosine (`score`) the index uses, and only above a cut-off: when nothing clears it,
# nothing is attached (a weak neighbour is noise; on the instance this was lifted from, nearest
# neighbours of a failed command's text were relevant about 4 times in 30).

def _cut_setting(name, default):
    try:
        import os
        return float(hub_app._dj_setting(name, None) or os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


EVENT_KNOWLEDGE_K = 3
_EVK_CACHE: dict = {}
_EVK_LOCK = threading.Lock()
_EVK_TTL_S = 3600


def event_knowledge(subject: str, *, k: int = EVENT_KNOWLEDGE_K, cut: float | None = None) -> list:
    """``[{id, type, title, score, rule?}]`` -- at most ``k`` records whose cosine to
    ``subject`` is at least the cut (HUB_EVENT_KNOWLEDGE_CUT, default 0.48); [] when none clears
    it, when there is no embedder, or on any failure. Cached per subject for an hour, so a frame
    re-delivered every wait cycle does not re-rank. Never raises."""
    subject = " ".join(str(subject or "").split())[:400]
    if not subject:
        return []
    cut = _cut_setting("HUB_EVENT_KNOWLEDGE_CUT", 0.48) if cut is None else cut
    now = time.time()
    with _EVK_LOCK:
        hit = _EVK_CACHE.get(subject)
    if hit and now - hit[0] < _EVK_TTL_S:
        return copy.deepcopy(hit[1])
    out = []
    try:
        state, key = _state_and_key()
        rows, _total, meta = memory_index(state, key, cap=max(k * 4, 12), focus=subject, full=k * 4)
        if meta.get("ranked") and meta.get("by") != "wording":
            for r in rows:
                sc = r.get("score")
                if sc is None or sc < cut:
                    continue
                item = {"id": r.get("id"), "type": r.get("type"), "title": r.get("title"),
                        "score": sc}
                if r.get("rule") and r["rule"] != r.get("title"):
                    item["rule"] = knowledge.clip(r["rule"], 300)
                out.append(item)
                if len(out) >= k:
                    break
    except Exception:                                          # noqa: BLE001 - a hint, never a failure
        return []
    with _EVK_LOCK:
        if len(_EVK_CACHE) > 500:
            _EVK_CACHE.clear()
        _EVK_CACHE[subject] = (now, out)
    return copy.deepcopy(out)


# ── /hub/knowledge/since: the append-only feed a machine mirrors and ranks locally ──

_CAT_MEMO = {"at": 0.0, "sha": "", "items": None}
_CAT_LOCK = threading.Lock()
CAT_REUSE_S = 30


def _catalog_sha():
    now = time.time()
    with _CAT_LOCK:
        if _CAT_MEMO["items"] is not None and now - _CAT_MEMO["at"] < CAT_REUSE_S:
            return _CAT_MEMO["sha"], _CAT_MEMO["items"]
    items, _meta = catalog()
    if items is None:
        return "", None                    # unavailable: the source does not move
    sha = hashlib.sha1(json.dumps(items, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    with _CAT_LOCK:
        _CAT_MEMO.update(at=now, sha=sha, items=items)
    return sha, items


@require_GET
def knowledge_since(request):
    """Everything the board knows, as an append-only feed a machine mirrors locally.

        GET /hub/knowledge/since?cursor=<opaque>&limit=<=500
        -> {cursor, more, items: [put|revoke|reset ...], head}

    An empty or 0 cursor is the bootstrap: page with `more` until it is false. A caught-up
    caller gets an empty page and, with its ETag sent back (If-None-Match, X-Hub-ETag or
    ?etag=), a 304: the same three carriers every conditional route reads, because a proxy on
    the path can drop If-None-Match and then a caught-up mirror is re-sent a full 200 forever."""
    cur = knowledge.parse_cursor(request.GET.get("cursor"))
    try:
        limit = max(1, min(int(request.GET.get("limit") or knowledge.FEED_MAX), knowledge.FEED_MAX))
    except ValueError:
        limit = knowledge.FEED_MAX
    state, key = _state_and_key()
    head_seq = key[1]
    cat_sha, cat_items = _catalog_sha()
    tag = '"k-%s"' % knowledge.cursor_str(cur)
    # A narrowed reader is sent no revoke for a record it could never see (knowledge.ledger_ops).
    from . import veil as _veil
    reader = _veil.veil_for(request)
    ops, last, more = knowledge.ledger_ops(state, cur["L"], limit,
                                           visible=None if reader.open else reader.visible)
    nxt = dict(cur)
    nxt["L"] = last if more else max(cur["L"], head_seq)
    items = list(ops)
    if not more and cat_items is not None and cat_sha and cat_sha != cur["C"]:
        # Sent whole, even past `limit`: a reset split across pages would leave a mirror holding
        # half a catalog between polls.
        items.extend(knowledge.catalog_ops(cat_items))
        nxt["C"] = cat_sha
    sent = (request.headers.get("If-None-Match") or request.headers.get("X-Hub-ETag")
            or request.GET.get("etag") or "")
    if not items and sent.strip().replace("W/", "").strip('"') == tag.strip('"'):
        resp = HttpResponse(status=304)
        resp["ETag"] = tag
        resp["X-Hub-ETag"] = tag
        return resp
    body = {"cursor": knowledge.cursor_str(nxt), "more": bool(more), "items": items,
            "head": {"seq": head_seq, "hash": key[2], "catalog": cat_sha}}
    if not items:
        body["etag"] = tag            # in the body too, for a transport that returns bodies only
    resp = JsonResponse(body)
    if not items:
        resp["ETag"] = tag
        resp["X-Hub-ETag"] = tag
    return resp
