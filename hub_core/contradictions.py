"""Find doctrine lines and standing lessons that tell the reader to do OPPOSITE things.

The standing doctrine rides every session and the lessons ride every prompt. When one says
"always X" and the other says "never X in the same situation", an agent follows whichever it read
last -- and nobody sees the conflict, because nothing compared them. Lesson against lesson is
already compared at write time (the overlap tag) and settled by the adjudication pass
(``hub_core.adjudicate``), but a settled ``contradiction`` verdict sat on the record where nobody
was asked to decide it, and doctrine was never compared at all.

WHAT A PASS DOES
  1. Doctrine vs lessons. Every rule-bearing doctrine SENTENCE (one with a rule word: never,
     always, must, do not, only ...) is sent to the hub's related-records seam, which answers with
     the closest live records by weighted vocabulary AND by meaning. Each lesson among them is
     read by the judge model with the adjudicator's own verdict set: duplicate / correction /
     contradiction / unrelated. Each (line, lesson version) is judged ONCE; the verdict is cached
     until either text changes.
  2. Lesson vs lesson. Every adjudicated ``contradiction`` on a live lesson is collected.
  3. Each contradiction becomes ONE review -- the scaffold's delivered human gate (an ask tagged
     ``review``, never taken by an unattended responder) -- carrying both texts, both ids and the
     model's one-sentence reason, and a stable marker so a rerun never files it twice and never
     reopens one a person already answered.

WHAT IT REFUSES TO DO. It reports only: it never edits, supersedes or retires doctrine or a
lesson -- a person decides which rule stands. It never guesses: an unparseable reply is counted and
left unjudged. With judging to do and no model, the caller exits non-zero (a pass that quietly
judges nothing is how a board sits at zero).

Framework-free: the client verb (``python -m hub_core.client detect-contradictions``) supplies
the doctrine text, the lessons and the related-records lookup from the served read API, and files
reviews through the served ask API.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import re

from . import adjudicate

TOP_K = 3
MIN_LINE, MAX_LINE = 40, 700
_RULE_WORD = re.compile(r"\b(never|always|must|do not|don't|only|required|refuse|forbid|"
                        r"without exception|no exceptions?|every|stays?|belongs?|is never|"
                        r"are never|not allowed|may not|must not|cannot)\b", re.I)

DOCTRINE_PROMPT = """A team's standing doctrine and one of its shared engineering lessons may overlap. Read both and decide how they relate.

DOCTRINE LINE
{a_rule}

LESSON
{b_rule}
Why the lesson was written: {b_why}

Answer with exactly one of:
- duplicate: they are the same rule for the same situation.
- correction: one fixes, narrows or updates the other, so one should replace the other.
- contradiction: in the same situation they tell the reader to do opposite things.
- unrelated: they share words or a topic but are different rules; both can stand.

Reply with JSON only, no prose around it: {{"verdict": "<one of the four>", "reason": "<one sentence>"}}"""


def sha(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()


def _units(body_md: str) -> list:
    """Paragraphs and bullets, each reflowed to one line: doctrine is hard-wrapped, so a physical
    line is a sentence FRAGMENT and would be compared as one. Code blocks, headings, tables and
    quotes are not rules."""
    units, cur, in_code = [], [], False

    def flush():
        if cur:
            units.append(" ".join(cur))
            cur.clear()
    for raw in str(body_md or "").splitlines():
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code or s.startswith(("#", "|", ">")) or not s:
            flush()
            continue
        if re.match(r"^([-*+]|\d+\.)\s+", s):
            flush()
            cur.append(re.sub(r"^([-*+]|\d+\.)\s+", "", s))
        else:
            cur.append(s)
    flush()
    return units


def doctrine_lines(body_md: str, *, source: str = "doctrine") -> list:
    """[(line_id, text)] for every rule-bearing doctrine SENTENCE, markdown stripped. The id is
    derived from the text, so it is stable across passes and moves only when the line does."""
    out, seen = [], set()
    for unit in _units(body_md):
        unit = re.sub(r"[*_`]{1,3}", "", unit).strip()
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\"(])", unit):
            line = sent.strip()
            if not (MIN_LINE <= len(line) <= MAX_LINE) or not _RULE_WORD.search(line):
                continue
            key = " ".join(line.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(("%s:%s" % (source, sha(key)[:12]), line))
    return out


def judge_line(line_text: str, lesson: dict, *, chat=None):
    """(verdict, reason) or (None, why_not) from the judge model (HUB_JUDGE_URL)."""
    chat = chat or adjudicate.chat
    prompt = DOCTRINE_PROMPT.format(a_rule=adjudicate.norm(line_text),
                                    b_rule=adjudicate.norm(lesson.get("title")),
                                    b_why=adjudicate.norm(lesson.get("body_md"))[:1500] or "(none)")
    try:
        reply = chat([{"role": "user", "content": prompt}])
    except Exception as exc:                                 # noqa: BLE001
        return None, "model call failed: %s: %s" % (type(exc).__name__, exc)
    verdict, reason = adjudicate.parse_verdict(reply)
    if verdict is None:
        return None, "%s; reply was %r" % (reason, str(reply)[:160])
    return verdict, reason


def marker(a_id: str, b_id: str) -> str:
    """The stable marker a review carries: one per unordered pair, whichever side found it."""
    return "contradiction:" + sha(*sorted((a_id, b_id)))[:12]


def review_text(a_id, a_text, b_id, b_text, reason, origin) -> tuple:
    """(question, context) for the review ask."""
    mark = marker(a_id, b_id)
    what = ("doctrine vs %s" % b_id) if not a_id.count(":") >= 2 else ("%s vs %s" % (a_id, b_id))
    question = "Review gate: two served rules contradict each other (%s) [%s]" % (what, mark)
    context = "\n".join([
        "Two rules agents are served tell the reader to do opposite things in the same situation.",
        "Found by the contradiction pass (%s). Nothing was edited: decide which rule stands, then "
        "supersede or edit the other, and answer this review." % origin,
        "", "**%s**" % a_id, "> " + " ".join(str(a_text).split()),
        "", "**%s**" % b_id, "> " + " ".join(str(b_text).split()),
        "", "Why they conflict: " + (reason or "(no reason given)")])
    return question, context


def run_pass(*, lines, lessons, neighbours, filed, raise_review, cache, judge=judge_line,
             model_ok=adjudicate.model_reachable, limit: int = 120, dry_run: bool = False,
             out=print) -> dict:
    """One pass. ``lessons`` {id: note}; ``neighbours(text) -> ([lesson ids, closest first],
    why_blind)`` where ``why_blind`` is "" when at least one basis (wording or meaning) ran, else
    the reason neither could -- "no neighbours" and "could not look" are different facts;
    ``filed`` the markers already on the board (any status); ``raise_review(question, context,
    relates_to) -> (ok, detail)``; ``cache`` a dict of prior verdicts, updated in place."""
    t = {"doctrine_lines": len(lines), "lessons": len(lessons), "pairs_considered": 0,
         "judged_now": 0, "judged_cached": 0, "unjudged": 0, "needs_model": 0,
         "contradictions": 0, "from_adjudicator": 0, "reviews_new": 0, "reviews_existing": 0,
         "write_refused": 0, "verdicts": {}, "lookup_blind": 0}
    found = []                         # (a_id, a_text, b_id, b_text, reason, origin)
    pending = []
    for lid, text in lines:
        near, blind = neighbours(text)
        if blind:
            t["lookup_blind"] += 1
            t["lookup_reason"] = blind
            continue
        for eid in [e for e in near if e in lessons][:TOP_K]:
            t["pairs_considered"] += 1
            lesson = lessons[eid]
            key = sha(lid, eid, lesson.get("version"))
            hit = cache.get(key)
            if hit:
                t["judged_cached"] += 1
                if hit.get("verdict") == "contradiction":
                    found.append((lid, text, eid, lesson.get("title"), hit.get("reason"),
                                  "doctrine-vs-lesson"))
                continue
            pending.append((key, lid, text, eid, lesson))
    reachable = None
    for key, lid, text, eid, lesson in pending[:limit]:
        if reachable is None:
            reachable, detail = model_ok()
            out("MODEL %s" % detail)
        if not reachable:
            t["needs_model"] += 1
            continue
        verdict, reason = judge(text, lesson)
        if verdict is None:
            t["unjudged"] += 1
            out("UNJUDGED %s -> %s: %s" % (lid, eid, reason))
            continue
        t["judged_now"] += 1
        t["verdicts"][verdict] = t["verdicts"].get(verdict, 0) + 1
        cache[key] = {"verdict": verdict, "reason": reason, "at": adjudicate.now_iso(),
                      "line": lid, "lesson": eid}
        out("VERDICT %-13s %s -> %s  %s" % (verdict, lid, eid, reason))
        if verdict == "contradiction":
            found.append((lid, text, eid, lesson.get("title"), reason, "doctrine-vs-lesson"))

    for eid, ent in lessons.items():
        for r in ent.get("related") or []:
            if not (isinstance(r, dict) and r.get("adjudicated") and r.get("verdict") == "contradiction"):
                continue
            other = lessons.get(str(r.get("id")))
            if other is None:
                continue
            t["from_adjudicator"] += 1
            found.append((eid, ent.get("title"), other["id"], other.get("title"), r.get("reason"),
                          "adjudicator"))

    seen, unique = set(), []
    for item in found:           # the adjudicator tags BOTH lessons; one pair is one contradiction
        pair = tuple(sorted((item[0], item[2])))
        if pair not in seen:
            seen.add(pair)
            unique.append(item)
    t["contradictions"] = len(unique)
    for a_id, a_text, b_id, b_text, reason, origin in unique:
        mark = marker(a_id, b_id)
        if mark in filed:
            t["reviews_existing"] += 1
            continue
        question, context = review_text(a_id, a_text, b_id, b_text, reason, origin)
        out("REVIEW %s  %s" % (mark, question))
        if dry_run:
            continue
        ok, detail = raise_review(question, context, [x for x in (a_id, b_id) if x.count(":") >= 2])
        if ok:
            t["reviews_new"] += 1
            filed.add(mark)
        else:
            t["write_refused"] += 1
            out("WRITE REFUSED %s %s" % (mark, str(detail)[:300]))
    return t
