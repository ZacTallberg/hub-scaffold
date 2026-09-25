"""manage.py retrieval_eval — measure search on THIS board's own answered questions.

A ranking that was reasoned about and never measured can be wrong for months while every
surface reports success: on the board this method was lifted from, the original term-frequency
scorer put the right record in the top five for 4.9% of real questions, and nobody knew.

THE GOLDEN SET IS NOT AUTHORED. Every answered ask on the board is a question somebody asked
when they were stuck, linked by the board itself (`directive.answers`) to the reply that
resolved it. Nobody wrote those pairs for an evaluation, so they cannot encode the evaluator's
belief about what search should find. A board with fewer than MIN_GOLDEN of them is too small
to measure, and the command says so instead of printing a percentage of nothing.

WHAT IS PRINTED, per configuration: top-1 / top-5 / top-10 accuracy.
  * lexical        BM25F over the corpus exactly as served (titles intact).
  * lexical-body   the same with each answer's title AND its restated question removed, so a
                   keyword method cannot win on the echo. Not comparable to the fused rows:
                   stored vectors were embedded from the full text.
  * dense          cosine (+CSLS when the hub term exists), when an embedder is configured.
  * fused@alpha    convex fusion; alpha is FITTED on one half and REPORTED on the other, over
                   SPLITS deterministic partitions, printed as mean (min-max), so the spread is
                   the measurement's own error bar. The shipped alpha is printed on all pairs.

  * delivered      the PER-PROMPT path (what `guidance.json` would put in front of a console
                   whose focus is the ask): the answer's rank in the ranked memory index, at
                   @1/@5/@10/@25. Its query is the ask's CONTEXT with the question removed,
                   because every answer restates its question verbatim ("Answer: <q>" and
                   "In answer to your question: <q>") and scoring a question against its own
                   echo inflates the figure. Context sentences sharing >= 60% of the question's
                   words are dropped too; an ask left with < 60 chars is excluded and COUNTED.
                   One row per ASK: two asks may share a context prefix.

`--post` records the run on the board's standing-eval trend (hub_core.evals, read back at
/hub/eval.json), suite `retrieval-answered-asks` unless `--suite` names another, and prints the
row as stored, so a run that did not land never reads as recorded.

TIES ARE PESSIMISTIC: a target's rank counts every record scoring greater than OR EQUAL to it.
BM25 gives most of a corpus exactly 0.0, and the optimistic convention would rank a target that
matched nothing first.

Read-only against the ledger. It reads the fold and the vector sidecar and embeds the questions
(never the corpus); the delivered path may warm the focus-embedding cache exactly as a prompt
would; `--post` appends one row to HUB_DIR/evals.jsonl and nothing else.
"""
import hashlib
import re

from django.core.management.base import BaseCommand

from hub import hub_app, knowledge_api
from hub_core import bm25, evals, semantic

MIN_GOLDEN = 10
SPLITS = 5
ALPHAS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
_TAIL = re.compile(r"\n+---\nIn answer to your question:.*\Z", re.S)
DELIVERED_KS = (1, 5, 10, 25)
MIN_CONTEXT_CHARS = 60
ECHO_SHARE = 0.6
_WORD = re.compile(r"[a-z0-9]{3,}")


def delivered_golden(state):
    """`([(query, answer_id, ask_id)], excluded)`: one row per answered ASK, its query the ask's
    context with every sentence that restates the question removed."""
    ents = state.get("entities") or {}
    rows, excluded, seen = [], 0, set()
    for ent in sorted((e for e in ents.values() if isinstance(e, dict)),
                      key=lambda e: str(e.get("id"))):
        if ent.get("type") != "directive" or not ent.get("answers"):
            continue
        if ent.get("status") in ("superseded", "dropped", "retired"):
            continue
        ask_id = str(ent["answers"])
        ask = ents.get(ask_id)
        if not ask or ask_id in seen:
            continue
        seen.add(ask_id)
        title = " ".join(str(ask.get("title") or "").split())
        context = " ".join(str(ask.get("body_md") or "").split()).replace(title, "")
        qwords = set(_WORD.findall(title.lower()))
        keep = [s for s in re.split(r"(?<=[.!?])\s+", context)
                if not qwords or len(qwords & set(_WORD.findall(s.lower()))) / len(qwords) < ECHO_SHARE]
        query = " ".join(keep).strip(" .:-")[:400]
        if len(query) < MIN_CONTEXT_CHARS:
            excluded += 1
            continue
        rows.append((query, str(ent["id"]), ask_id))
    return rows, excluded


def golden(state):
    """`[(question, answer_id)]` from the board's own ask -> answer links."""
    ents = state.get("entities") or {}
    out = []
    for ent in ents.values():
        if not isinstance(ent, dict) or ent.get("type") != "directive" or not ent.get("answers"):
            continue
        ask = ents.get(str(ent["answers"]))
        if not ask:
            continue
        question = " ".join(str(ask.get("title") or "").split())
        context = " ".join(str(ask.get("body_md") or "").split())
        text = (question + " " + context).strip()[:400]
        if text:
            out.append((text, str(ent["id"])))
    return sorted(out, key=lambda p: p[1])


def rank_of(target, scores):
    """Pessimistic rank of `target` in {id: score}; None when it scored nothing."""
    t = scores.get(target)
    if not t:
        return None
    return sum(1 for s in scores.values() if s >= t)


def accuracy(ranks):
    n = max(1, len(ranks))
    return tuple(round(100.0 * sum(1 for r in ranks if r and r <= k) / n, 1) for k in (1, 5, 10))


def half(question, salt):
    return int(hashlib.sha256(("%s|%s" % (salt, question)).encode("utf-8")).hexdigest(), 16) % 2


class Command(BaseCommand):
    help = "Measure retrieval accuracy on the board's own answered questions (read-only)."

    def add_arguments(self, p):
        p.add_argument("--json", action="store_true", help="print the table as JSON")
        p.add_argument("--post", action="store_true",
                       help="record this run on the standing-eval trend (/hub/eval.json)")
        p.add_argument("--suite", default="retrieval-answered-asks",
                       help="the trend suite --post records under")

    def handle(self, *a, json=False, post=False, suite="retrieval-answered-asks", **o):
        import json as _json
        state = hub_app.current_state()
        pairs = golden(state)
        if len(pairs) < MIN_GOLDEN:
            self.stderr.write("retrieval_eval: only %d answered questions on this board; at least %d "
                              "are needed before a percentage means anything" % (len(pairs), MIN_GOLDEN))
            raise SystemExit(3)
        key = ("eval", id(state))
        knowledge_api.catalog(wait=True)          # a fresh process has not read the catalog yet
        cat_items, _err = knowledge_api._catalog_for_index()
        rows, docs, _ck = knowledge_api._corpus(state, key, cat_items)
        answers = {a for _q, a in pairs}
        missing = sorted(a for a in answers if a not in rows)
        # BODY-ONLY: strip each answer's title and its restated question.
        body_docs = {}
        for eid, d in docs.items():
            if eid in answers:
                ent = state["entities"][eid]
                body_docs[eid] = {"title": "", "tags": d["tags"],
                                  "body": _TAIL.sub("", str(ent.get("body_md") or ""))}
            else:
                body_docs[eid] = d
        full_idx, body_idx = bm25.Bm25F(docs), bm25.Bm25F(body_docs)
        lex = [dict((i, s) for s, i in full_idx.score(q)) for q, _a in pairs]
        lexb = [dict((i, s) for s, i in body_idx.score(q)) for q, _a in pairs]
        table = {"golden": len(pairs), "corpus": len(rows), "answers_not_in_corpus": len(missing),
                 "configs": {}}
        table["configs"]["lexical"] = accuracy([rank_of(a, s) for (_q, a), s in zip(pairs, lex)])
        table["configs"]["lexical-body (not comparable)"] = accuracy(
            [rank_of(a, s) for (_q, a), s in zip(pairs, lexb)])

        dense, why = self._dense(pairs, list(rows))
        if dense is None:
            table["dense"] = "not measured: %s" % why
        else:
            table["configs"]["dense (%s)" % why] = accuracy(
                [rank_of(a, s) for (_q, a), s in zip(pairs, dense)])
            fused = {al: [semantic.fuse_convex(l, d, alpha=al) for l, d in zip(lex, dense)] for al in ALPHAS}
            ranks = {al: [rank_of(a, s) for (_q, a), s in zip(pairs, fused[al])] for al in ALPHAS}
            table["configs"]["fused@%s shipped" % semantic.CONVEX_ALPHA] = accuracy(
                ranks[min(ALPHAS, key=lambda x: abs(x - semantic.CONVEX_ALPHA))])
            held, chosen = [], []
            for split in range(SPLITS):
                fit = [i for i, (q, _a) in enumerate(pairs) if half(q, split) == 0]
                rep = [i for i, (q, _a) in enumerate(pairs) if half(q, split) == 1]
                best = max(ALPHAS, key=lambda al: (accuracy([ranks[al][i] for i in fit])[0], -abs(al - semantic.CONVEX_ALPHA)))
                chosen.append(best)
                held.append(accuracy([ranks[best][i] for i in rep]))
            mean = tuple(round(sum(h[k] for h in held) / len(held), 1) for k in range(3))
            spread = tuple("%s-%s" % (min(h[k] for h in held), max(h[k] for h in held)) for k in range(3))
            table["configs"]["fused, alpha fitted on held-out halves"] = mean
            table["held_out"] = {"splits": SPLITS, "alphas_chosen": chosen, "spread_top1_top5_top10": spread}
        table["delivered"] = self._delivered(state, key)
        if post:
            table["posted"] = self._post(table, suite)
        if json:
            self.stdout.write(_json.dumps(table, indent=2, sort_keys=True))
            return
        self.stdout.write("retrieval_eval: %d answered questions, %d searchable records%s"
                          % (table["golden"], table["corpus"],
                             ("; %d answers are not in the corpus" % len(missing)) if missing else ""))
        self.stdout.write("%-44s %7s %7s %7s" % ("configuration", "top-1", "top-5", "top-10"))
        for name, (t1, t5, t10) in table["configs"].items():
            self.stdout.write("%-44s %6.1f%% %6.1f%% %6.1f%%" % (name[:44], t1, t5, t10))
        if "dense" in table:
            self.stdout.write("dense: %s" % table["dense"])
        if "held_out" in table:
            self.stdout.write("held-out: alphas chosen per split %s; spread top-1/5/10 %s"
                              % (table["held_out"]["alphas_chosen"],
                                 ", ".join(table["held_out"]["spread_top1_top5_top10"])))
        d = table["delivered"]
        self.stdout.write("delivered (per-prompt block): %d asks scored, %d excluded (< %d chars of "
                          "context once the question is removed)%s"
                          % (d["n"], d["excluded"], MIN_CONTEXT_CHARS,
                             ("; ranked by %s" % d["ranked_by"]) if d.get("ranked_by") else ""))
        if d["n"]:
            self.stdout.write("   " + "  ".join("@%d %5.1f%%" % (k, d["@%d" % k]) for k in DELIVERED_KS))
        if "posted" in table:
            self.stdout.write("posted: %s" % _json.dumps(table["posted"], sort_keys=True))

    def _delivered(self, state, key):
        """Rank of each answer in the per-prompt memory index for its ask's context."""
        rows, excluded = delivered_golden(state)
        ranks, by = [], {}
        for query, answer_id, _ask in rows:
            ranked, _total, meta = knowledge_api.memory_index(
                state, key, cap=max(DELIVERED_KS), focus=query, full=0)
            ids = [r.get("id") for r in ranked]
            ranks.append(ids.index(answer_id) + 1 if answer_id in ids else None)
            way = meta.get("by") or ("meaning" if meta.get("ranked") else "standing order")
            by[way] = by.get(way, 0) + 1
        out = {"n": len(rows), "excluded": excluded,
               "ranked_by": ", ".join("%s %d" % kv for kv in sorted(by.items()))}
        n = max(1, len(ranks))
        for k in DELIVERED_KS:
            out["@%d" % k] = round(100.0 * sum(1 for r in ranks if r and r <= k) / n, 1)
        return out

    def _post(self, table, suite):
        paths = {}
        for name, (t1, t5, t10) in table["configs"].items():
            paths[name[:48]] = {"n": table["golden"], "@1": t1, "@5": t5, "@10": t10}
        d = table["delivered"]
        if d["n"]:
            paths["delivered"] = {k: v for k, v in d.items() if k != "ranked_by" and k != "excluded"}
        run = {"suite": suite, "pairs": table["golden"], "excluded": d["excluded"], "paths": paths,
               "notes": "configs over %d answered asks, %d searchable records; delivered ranked by %s"
                        % (table["golden"], table["corpus"], d.get("ranked_by") or "-")}
        try:
            return evals.append(hub_app.HUB_DIR, run, by="retrieval_eval")
        except evals.EvalRefused as refused:
            self.stderr.write("retrieval_eval: the trend refused the run: %s" % refused)
            raise SystemExit(4)

    def _dense(self, pairs, candidate_ids):
        if not semantic.configured():
            return None, "no embedder configured (HUB_EMBED_URL)"
        conn = semantic.connect(hub_app.HUB_DIR)
        try:
            vectors = semantic.vectors_for(conn, candidate_ids)
            if not vectors:
                return None, "no vectors stored (run manage.py semantic_index)"
            hubs = semantic.hub_terms(conn)
            out = []
            batch = 16
            questions = [q for q, _a in pairs]
            for i in range(0, len(questions), batch):
                try:
                    qvecs = semantic.embed(questions[i:i + batch], is_query=True)
                except semantic.EmbedUnavailable as exc:
                    return None, str(exc)
                for qv in qvecs:
                    scores = {}
                    for eid, vec in vectors.items():
                        if len(vec) != len(qv):
                            continue
                        c = semantic.cosine(qv, vec)
                        scores[eid] = (2.0 * c - hubs.get(eid, 0.0)) if hubs else c
                    out.append(scores)
            return out, "csls" if hubs else "cosine"
        finally:
            conn.close()
