"""Weighted lexical overlap between rules: the same words, judged by how common they are HERE.

The first overlap check counted ABSOLUTE shared "distinctive" terms (four letters or more,
not in a hand-kept stoplist) and refused a new rule that shared three with a live one. Three
arithmetic defects followed, all measured on the instance this was lifted from:

  1. COMMON WORDS COUNTED AS MUCH AS RARE ONES. A team that writes rules as "never do X" makes
     `never` its most frequent distinctive term (17% of rules, 29% of every collision).
  2. A HAND-KEPT STOPLIST FALLS BEHIND: `test` filtered, `tests` not; every new topic arrives
     with vocabulary nobody added.
  3. AN ABSOLUTE COUNT PUNISHES CAREFUL RULES: 6% of 3-5-term rules collided, 93% of rules
     with 25+ terms. The check was hardest on its best input.

TF-IDF cosine fixes all three with stdlib arithmetic: IDF weighs a term by how rare it is in
THIS corpus (no list to maintain) and the cosine normalizes by length.

THE THRESHOLD IS AN ABSOLUTE COSINE. A relative criterion (an outlier against the corpus)
was tried first and collapsed: TF-IDF similarities are overwhelmingly zero, so every rule's
nearest neighbour is many deviations above a near-zero mean. On the source corpus the real
subject collisions scored >= 0.38 and the recorded false refusals <= 0.23; 0.31 sits in the
gap. That is a measurement of ONE corpus — re-measure yours before trusting it.

WHAT THIS IS NOT: it is still vocabulary. It cannot see two rules saying the same thing in
different words (that is `semantic.related`). And its output is a TAG for a reader, never a
refusal: a correction is near-identical text by definition, so any similarity gate at the door
is optimally shaped to reject exactly the writes that fix a wrong rule.
"""
from __future__ import annotations

import math
import re

from . import knowledge

#: Calibrated on the source corpus (see module docstring). A caller passes it explicitly.
CONFLICT_COSINE = 0.31

_WORD = re.compile(r"[a-z0-9][a-z0-9._/-]{3,}")
_STOP = frozenset("""
this that with from have will when then than they them their there were been being into
only also must should would could does done make made just more most much many some such
what which while where your yours about after again against because before below between
both during each further here itself other over same through under until very
""".split())


def terms(text) -> set:
    return {w.strip("._/-") for w in _WORD.findall(str(text or "").lower())
            if w.strip("._/-") and w.strip("._/-") not in _STOP}


def is_rule(ent) -> bool:
    """A live rule the overlap tagger compares against: a lesson, or a crystallized memory note."""
    return knowledge.knowledge_kind(ent) in ("lesson", "note") and not knowledge.is_dead(ent)


def corpus_df(state) -> tuple:
    """`({term: document_frequency}, n)` over live knowledge written in the same voice."""
    df, n = {}, 0
    for ent in (state.get("entities") or {}).values():
        if not isinstance(ent, dict) or knowledge.knowledge_kind(ent) is None or knowledge.is_dead(ent):
            continue
        text = knowledge.title_of(ent)
        if not text:
            continue
        n += 1
        for t in terms(text):
            df[t] = df.get(t, 0) + 1
    return df, n


def weights(text, df, n) -> dict:
    """An L2-normalized TF-IDF vector. Smoothed IDF so a term in every document still weighs a
    little and a term the corpus has never seen (normal for a NEW rule) divides by nothing."""
    ts = terms(text)
    if not ts:
        return {}
    raw = {t: math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0 for t in ts}
    norm = math.sqrt(sum(v * v for v in raw.values()))
    return {t: v / norm for t, v in raw.items()} if norm else {}


def similarity(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b[t] for t, w in a.items() if t in b)


def neighbours(text, state, *, cos_min: float = CONFLICT_COSINE, ignore_id: str = "",
               limit: int = 4) -> tuple:
    """`(hits, meta)` — live rules whose weighted vocabulary is close to `text`.

    SELF IS ESTABLISHED BY IDENTITY ONLY (`ignore_id`). Skipping on equal TEXT made a
    character-for-character duplicate of a live rule the one thing this could never report;
    exact copies are flagged `exact` and ordered first so a `limit` never hides one.
    """
    df, n = corpus_df(state)
    if n < 8:
        return [], {"weighted": False, "reason": "only %d knowledge records; too few to weight terms" % n}
    query = weights(text, df, n)
    if not query:
        return [], {"weighted": False, "reason": "no distinctive terms in the rule"}
    norm_text = " ".join(str(text).split())
    hits, best, compared = [], 0.0, 0
    for ent in (state.get("entities") or {}).values():
        if not isinstance(ent, dict) or not is_rule(ent):
            continue
        eid = str(ent.get("id") or "")
        if not eid or eid == ignore_id:
            continue
        rule = knowledge.title_of(ent)
        if not rule:
            continue
        compared += 1
        sim = similarity(query, weights(rule, df, n))
        best = max(best, sim)
        if sim >= cos_min:
            shared = sorted(set(query) & terms(rule), key=lambda t: (-query.get(t, 0.0), t))
            hits.append({"id": eid, "rule": rule[:160], "similarity": round(sim, 4),
                         # Ordered by WEIGHT, so the term that drove the match reads first.
                         "shared": shared[:5],
                         # 1.0 does NOT mean identical: the same words in any order also score
                         # 1.0. Only this flag says the stored rule and the incoming one are the
                         # same characters.
                         "exact": " ".join(rule.split()) == norm_text,
                         "status": ent.get("status") or "",
                         "foundational": str(ent.get("tier") or "") == "foundational"})
    if compared < 8:
        return [], {"weighted": False, "reason": "only %d live rules to compare" % compared}
    hits.sort(key=lambda h: (not h["exact"], -h["similarity"], h["id"]))
    return hits[:limit], {"weighted": True, "cos_min": cos_min, "compared": compared,
                          "closest": round(best, 4), "exact": sum(1 for h in hits if h["exact"])}
