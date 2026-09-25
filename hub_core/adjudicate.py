"""Settle the overlap tags a lesson write records — rules first, a model only when reading helps.

WHY THIS EXISTS. A lesson write no longer refuses a rule that resembles a live one (a
correction is near-identical text by definition, so a similarity gate at the door rejects
exactly the writes that fix a wrong rule). It ADMITS the write and records the suspected
relationship as `related` entries with `adjudicated: false`. A suspicion nobody settles is
worse than the refusal it replaced — measured on the source instance: 30 tagged entries,
0 adjudicated, read by nothing. This closes the loop.

Per unadjudicated entry, in order:
  1. RULES decide what reading cannot improve: identical text is a duplicate; a match that is
     not a rule (a task, a finding) is `other-kind`; a matched record since retired is
     `target-retired`.
  2. A MODEL reads both rules and both stories and answers duplicate / correction /
     contradiction / unrelated with one sentence of reason.

IT NEVER GUESSES. A reply that does not parse to one of the four verdicts leaves the entry
open and is counted. A pass with judging to do and no model EXITS NON-ZERO (the caller maps
`needs_model` to exit 2): a job that quietly adjudicates nothing is how a board sits at 0 of 30.

The model is any OpenAI-compatible chat endpoint (read at call time):
    HUB_JUDGE_URL    base URL, e.g. http://127.0.0.1:8000/v1  (POST <base>/chat/completions)
    HUB_JUDGE_MODEL  model name
    HUB_JUDGE_TOKEN  bearer token (optional)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.request

VERDICTS = ("duplicate", "correction", "contradiction", "unrelated")
RULE_VERDICTS = ("duplicate", "other-kind", "target-retired")
TIMEOUT_S = 60.0

PROMPT = """Two rules from a team's shared engineering memory may overlap. Read both, including why each was written, and decide how they relate.

RULE A
{a_rule}
Why A was written: {a_why}

RULE B
{b_rule}
Why B was written: {b_why}

Answer with exactly one of:
- duplicate: A and B are the same rule for the same situation.
- correction: one fixes, narrows or updates the other, so one should replace the other.
- contradiction: in the same situation they tell the reader to do opposite things.
- unrelated: they share words or a topic but are different rules; both can stand.

Reply with JSON only, no prose around it: {{"verdict": "<one of the four>", "reason": "<one sentence>"}}"""


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(text) -> str:
    return " ".join(str(text or "").split())


def judge_config() -> dict:
    return {"url": (os.environ.get("HUB_JUDGE_URL") or "").strip().rstrip("/"),
            "model": (os.environ.get("HUB_JUDGE_MODEL") or "").strip(),
            "token": (os.environ.get("HUB_JUDGE_TOKEN") or "").strip()}


def _headers(cfg) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg["token"]:
        h["Authorization"] = "Bearer " + cfg["token"]
    return h


def chat(messages, *, max_tokens: int = 200, cfg=None) -> str:
    cfg = cfg or judge_config()
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.0}
    if cfg["model"]:
        body["model"] = cfg["model"]
    req = urllib.request.Request(cfg["url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers=_headers(cfg), method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        payload = json.load(resp)
    return str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def model_reachable(cfg=None) -> tuple:
    """`(ok, detail)`. Completes once: an open port is not a serving model."""
    cfg = cfg or judge_config()
    if not cfg["url"]:
        return False, "no judge model configured (set HUB_JUDGE_URL and HUB_JUDGE_MODEL)"
    try:
        reply = chat([{"role": "user", "content": "Reply with the single word: ready"}],
                     max_tokens=5, cfg=cfg)
    except Exception as exc:                                  # noqa: BLE001
        return False, "completion failed at %s: %s: %s" % (cfg["url"], type(exc).__name__, exc)
    return True, "%s at %s answered %r" % (cfg["model"] or "(default model)", cfg["url"], reply[:20])


def parse_verdict(text) -> tuple:
    """`(verdict, reason)` or `(None, why_not)`. Shape-tolerant (fences, a sentence before the
    JSON, capitalised keys), and never coerces an unknown verdict to the nearest one: a
    confidently wrong verdict on a rule is worse than an open question."""
    raw = str(text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None, "no JSON object in the reply"
    try:
        obj = json.loads(m.group(0))
    except ValueError as exc:
        return None, "unparseable JSON: %s" % exc
    if not isinstance(obj, dict):
        return None, "JSON was not an object"
    low = {str(k).strip().lower(): v for k, v in obj.items()}
    verdict = str(low.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return None, "verdict %r is not one of %s" % (verdict, "/".join(VERDICTS))
    return verdict, norm(low.get("reason"))[:400] or "(no reason given)"


def rule_verdict(entry: dict, lesson: dict, target, is_dead) -> tuple:
    """`(verdict, reason)` decided without reading, or `(None, None)` when a reader is needed."""
    if entry.get("kind") and entry.get("kind") not in ("lesson", "note"):
        return "other-kind", "the match is a %s, not a rule" % entry.get("kind")
    if not target:
        return "target-retired", "the matched record is no longer on the board"
    if is_dead(target):
        return "target-retired", "the matched record is %s" % (target.get("status") or "retired")
    if entry.get("exact") or norm(lesson.get("title")) == norm(target.get("title")):
        return "duplicate", "identical rule text"
    return None, None


def judge_pair(lesson: dict, target: dict, cfg=None) -> tuple:
    """`(verdict, reason, by)` from the model, or `(None, why_not, None)`."""
    cfg = cfg or judge_config()
    prompt = PROMPT.format(a_rule=norm(lesson.get("title")), a_why=norm(lesson.get("body_md"))[:1500] or "(none)",
                           b_rule=norm(target.get("title")), b_why=norm(target.get("body_md"))[:1500] or "(none)")
    try:
        reply = chat([{"role": "user", "content": prompt}], cfg=cfg)
    except Exception as exc:                                  # noqa: BLE001
        return None, "model call failed: %s: %s" % (type(exc).__name__, exc), None
    verdict, reason = parse_verdict(reply)
    if verdict is None:
        return None, "%s; reply was %r" % (reason, reply[:160]), None
    return verdict, reason, cfg["model"] or "model"


# ── the pass ──
#
# The pass runs WHERE THE MODEL IS REACHABLE (a worker, an operator's machine, a scheduled job)
# and writes back through the served API like every other client: the ledger is never touched
# from a side process. It is therefore written against three callables rather than a store:
#
#   get_entity(id) -> dict | None        the record as the board serves it now
#   lookup_related(id) -> (data, meta)   related.json for a record: lexical AND semantic neighbours
#   write(id, fields, expected_version)  -> (ok: bool, detail: str)

def _is_lesson(ent) -> bool:
    return isinstance(ent, dict) and "lesson" in {str(t).lower() for t in (ent.get("tags") or [])}


def _dead(ent) -> bool:
    from .knowledge import DEAD_STATUS
    return str((ent or {}).get("status") or "") in DEAD_STATUS


def fill_deferred(lesson: dict, lookup_related) -> tuple:
    """Run the overlap bases a write could NOT run. `(related, related_partial, filled)`.

    A write never waits on the embedder, and the lexical basis declines on a board too small
    to weigh terms, so a lesson can land with either basis in `related_partial.missing`. This
    is where they run later: new neighbours join as unadjudicated suspicions, and a basis
    leaves `missing` only when it actually ran.

    `lookup_related(id)` returns `({"lexical": [...], "semantic": [...]}, metadata)` — the
    shape of `related.json`. A basis that still cannot run leaves the stored marker exactly as
    it was: rewording its reason would re-version every waiting lesson on every pass and send
    each one to every mirror again, for no new fact."""
    related = [dict(r) for r in (lesson.get("related") or []) if isinstance(r, dict)]
    stored = lesson.get("related_partial") or None
    missing = [m for m in ((stored or {}).get("missing") or []) if m]
    if not missing:
        return related, stored, 0
    try:
        data, meta = lookup_related(lesson["id"])
    except Exception:                                         # noqa: BLE001
        return related, stored, 0
    ran = {"weighted-lexical": bool((meta.get("lexical") or {}).get("weighted")),
           "semantic": bool((meta.get("semantic") or {}).get("semantic"))}
    source = {"weighted-lexical": "lexical", "semantic": "semantic"}
    have = {r.get("id") for r in related}
    added, still = 0, []
    for basis in missing:
        if not ran.get(basis):
            still.append(basis)
            continue
        for h in (data.get(source[basis]) or []):
            if not isinstance(h, dict) or not h.get("id") or h["id"] in have or h["id"] == lesson["id"]:
                continue
            entry = {"id": h["id"], "similarity": h.get("similarity"), "basis": basis,
                     "kind": h.get("kind") or "", "adjudicated": False}
            if basis == "semantic" and h.get("z") is not None:
                entry["z"] = h["z"]
            if basis == "weighted-lexical":
                entry["shared"] = list(h.get("shared") or [])[:5]
                entry["exact"] = bool(h.get("exact"))
            related.append(entry)
            have.add(h["id"])
            added += 1
    if still == missing:
        return related, stored, 0
    partial = {"missing": still, "reason": (stored or {}).get("reason") or ""} if still else None
    return related[:12], partial, added


def embedder_is_down(reason) -> bool:
    """True when a failed semantic lookup says the ENDPOINT is gone, not that this text found
    nothing: a refused or timed-out connection, an unresolvable name, the hub's open embed
    circuit, or no embedder configured. An HTTP error or a shape complaint is answered per call
    — the next text may well succeed."""
    low = str(reason or "").lower()
    return ("embed unreachable" in low or "no embedder configured" in low or "timed out" in low
            or "connection refused" in low)


def run_pass(lessons, *, get_entity, lookup_related, write, cfg=None, limit: int = 0,
             dry_run: bool = False) -> dict:
    """Settle every unadjudicated overlap on `lessons`; returns the counts a caller prints.

    `needs_model` counts entries that only a reader can settle while no model is configured:
    a caller maps a non-zero value to a non-zero exit, so an unattended schedule that settles
    nothing is loud instead of green."""
    cfg = cfg or judge_config()
    have_model = bool(cfg.get("url"))
    out = {"lessons": 0, "open_entries": 0, "by_rule": 0, "by_model": 0, "unparsed": 0,
           "needs_model": 0, "deferred_filled": 0, "semantic_deferred": 0, "written": 0,
           "write_failed": 0, "dry_run": dry_run, "model": cfg.get("model") or None,
           "embedder": "not needed", "failures": []}
    judged = 0
    # ONE EMBEDDER VERDICT PER PASS, not one wait per lesson. With the embedder down, each
    # lesson's deferred semantic lookup paid the full timeout and a pass of a few hundred
    # lessons spent nearly all its time waiting on one outage (while holding whatever lock or
    # queue slot the schedule gave it). The first "unreachable" settles it for the rest of the
    # pass: a lesson whose only missing basis is the semantic one is counted still-deferred
    # without a call, and the summary names the reason.
    embed_down = {"why": ""}

    def lookup(eid):
        data, meta = lookup_related(eid)
        sem = (meta or {}).get("semantic") or {}
        if sem.get("semantic"):
            out["embedder"] = "reachable"
        elif embedder_is_down(sem.get("reason")) and not embed_down["why"]:
            embed_down["why"] = str(sem.get("reason"))[:200]
        return data, meta
    for lesson in lessons:
        if not _is_lesson(lesson) or _dead(lesson):
            continue
        missing = [m for m in ((lesson.get("related_partial") or {}).get("missing") or []) if m]
        if embed_down["why"] and missing == ["semantic"]:
            out["semantic_deferred"] += 1
            related, partial, filled = ([dict(r) for r in (lesson.get("related") or []) if isinstance(r, dict)],
                                        lesson.get("related_partial") or None, 0)
        else:
            related, partial, filled = fill_deferred(lesson, lookup)
        out["deferred_filled"] += filled
        if partial and "semantic" in (partial.get("missing") or []) and not (
                embed_down["why"] and missing == ["semantic"]):
            out["semantic_deferred"] += 1
        changed = filled > 0 or partial != (lesson.get("related_partial") or None)
        open_here = [r for r in related if not r.get("adjudicated")]
        if not open_here and not changed:
            continue
        out["lessons"] += 1
        out["open_entries"] += len(open_here)
        for entry in open_here:
            if limit and judged >= limit:
                break
            target = get_entity(entry.get("id"))
            if target and not entry.get("kind"):
                entry["kind"] = ("lesson" if _is_lesson(target) else
                                 "note" if target.get("type") == "note" else str(target.get("type") or ""))
            verdict, reason = rule_verdict(entry, lesson, target, _dead)
            by = "rule"
            if verdict is None:
                if not have_model:
                    out["needs_model"] += 1
                    continue
                judged += 1
                verdict, reason, by = judge_pair(lesson, target, cfg=cfg)
                if verdict is None:
                    # NEVER GUESS: the entry stays open and the reason is counted, not coerced.
                    out["unparsed"] += 1
                    out["failures"].append({"lesson": lesson["id"], "target": entry.get("id"),
                                            "reason": reason})
                    continue
                out["by_model"] += 1
            else:
                out["by_rule"] += 1
            entry.update({"adjudicated": True, "verdict": verdict, "reason": reason,
                          "adjudicated_by": by, "adjudicated_at": now_iso()})
            changed = True
        if not changed or dry_run:
            continue
        # The note writer MERGES, so a cleared marker is written as an empty `missing` list
        # rather than omitted — an omitted key would leave the stale "semantic missing" standing.
        fields = {"related": related,
                  "related_partial": partial or {"missing": [], "reason": "every overlap basis ran"}}
        ok, detail = write(lesson["id"], fields, lesson.get("version"))
        if ok:
            out["written"] += 1
        else:
            out["write_failed"] += 1
            out["failures"].append({"lesson": lesson["id"], "reason": detail})
    if embed_down["why"]:
        out["embedder"] = embed_down["why"]
    if not out["failures"]:
        out.pop("failures")
    return out
