"""Knowledge WRITE API: lessons, and the finding / method / review record verbs.

All of them are `note` entities — the scaffold keeps optional record types out of its base
schema set — distinguished by what they carry (a kind tag, a category). Every write goes
through the ordinary token-gated seam (`hub_write._append`): schema validation, OCC, the
secret-shape refusal and realtime publication apply unchanged.

A LESSON IS ADMITTED, TAGGED, AND ADJUDICATED LATER — NEVER REFUSED FOR RESEMBLANCE.
A similarity gate at the door is the wrong shape, not a threshold to tune: a CORRECTION is
near-identical text, so the gate is optimally shaped to reject exactly the writes that fix a
wrong rule. On the instance this was lifted from, one evening's refusals were all corrections
or unrelated rules sharing ordinary words, and the gate's own escape hatch trained everyone to
bypass it. So the write lands, and the suspected relationship rides it as `related` (ids,
similarity, the shared terms that drove the match, an `exact` flag, `adjudicated: false`) —
with `related_partial` declaring any basis that did not run, because "nothing similar" and
"the half that reads meaning did not run" are different facts.

EXACT TEXT IS THE ONE CASE THAT NEEDS NO LATER READER: a rule identical (whitespace collapsed)
to a live one returns that record as `duplicate_of` and MERGES the new filing into it -- its
author, time and story appended to `reinforced_by` -- instead of minting a second record or
dropping the story. A finding re-filed under the same title (or a live finding's normalised
title) merges the same way, evidence kept. A board re-learns what it already knows (about one
new lesson in eight restated an existing record on the instance this was lifted from); a
restatement filed as a new record splits the evidence and the ranking, a merged one becomes the
rule's weight. Near-duplicates are never merged here -- a correction is near-identical text --
they are the consolidation pass's (hub_core.consolidate).
"""
import datetime as _dt
import hashlib
import re

from django.http import JsonResponse

from hub_core import ids, knowledge, lexical, semantic

from . import hub_app
from .hub_write import _append, _slug, writer

_LESSON_TAGS = ("lesson", "memory")


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _norm_title(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _resolve(ref) -> str:
    """A full id, or a bare note local, as a full note id ('' when unusable)."""
    ref = str(ref or "").strip()
    if not ref:
        return ""
    if ref.count(":") >= 2:
        return ref
    try:
        return ids.make_id(hub_app.PROJECT_KEY, "note", ref.rsplit(":", 1)[-1])
    except ValueError:
        return ""


def _lesson_id(rule, local=None) -> str:
    """Identity DERIVED from the rule: a retried POST lands on the same record, and a rule
    containing `C:\\ops`, `https://` or `Note:` can never be misread as an id fragment."""
    local = local or ("l-" + hashlib.sha256(_norm(rule).encode("utf-8")).hexdigest()[:12])
    return ids.make_id(hub_app.PROJECT_KEY, "note", local)


def _identical_live_rule(state, rule, this_id=""):
    """The live knowledge record whose rule IS this rule (whitespace collapsed), or None.
    A direct scan, never the similarity machinery: equality must not depend on a threshold or
    on there being enough records to weight terms. Fails OPEN."""
    text = _norm(rule)
    try:
        superseded = knowledge.superseded_ids(state)
        for ent in (state.get("entities") or {}).values():
            if not isinstance(ent, dict) or knowledge.knowledge_kind(ent) not in ("lesson", "note"):
                continue
            if knowledge.is_dead(ent, superseded):
                continue
            if _norm(ent.get("title")) == text:
                return ent
    except Exception:                                          # noqa: BLE001 - never block a write
        return None
    return None


#: How many restatements one record keeps (oldest dropped past it).
REINFORCED_MAX = 200


def _reinforce(ent, agent, why, as_kind):
    """Append one restatement to a record's `reinforced_by` (never a second record).
    Returns (resp, status) of the append."""
    rows = [dict(r) for r in (ent.get("reinforced_by") or []) if isinstance(r, dict)]
    rows.append({"agent": str(agent or "agent")[:80], "as": as_kind,
                 "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "why": str(why or "")[:600]})
    kept = {k: v for k, v in ent.items() if k not in ("id", "version", "provenance")}
    kept["reinforced_by"] = rows[-REINFORCED_MAX:]
    return _append("note", ent["id"], kept, expected_version=ent.get("version"), agent=agent,
                   idem=None, etype="note.created")


def _report(where, exc):
    """A failure the write deliberately survives still reaches the operational stream, with
    its type — a silent degradation in the tagger surfaces months later as "retrieval got
    worse" with nobody able to say when."""
    try:
        hub_app.record_error("hub.knowledge", "%s failed: %s" % (where, type(exc).__name__),
                             severity="warning", code="knowledge_write_path_failed",
                             context={"component": "knowledge", "operation": where})
    except Exception:                                          # noqa: BLE001
        pass


def overlap_tag(state, rule, eid) -> dict:
    """What this rule may duplicate, correct or contradict — RECORDED, never enforced."""
    try:
        hits, meta = lexical.neighbours(rule, state, ignore_id=eid)
    except Exception as exc:                                   # noqa: BLE001
        _report("lesson-overlap-tag", exc)
        hits, meta = [], {"weighted": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
    related = []
    for h in hits:
        related.append({"id": h["id"], "similarity": h["similarity"], "shared": h["shared"],
                        "basis": "weighted-lexical", "kind": "lesson" if "lesson" in
                        {str(t) for t in ((state.get("entities") or {}).get(h["id"], {}).get("tags") or [])}
                        else "note",
                        "exact": bool(h.get("exact")), "adjudicated": False})
    missing, reasons = [], []
    if not meta.get("weighted"):
        missing.append("weighted-lexical")
        reasons.append(str(meta.get("reason") or ""))
    # THE SEMANTIC HALF IS DEFERRED, never run here: it embeds over HTTP, and a write never
    # waits on the embedder. The adjudication pass fills it and clears the marker.
    missing.append("semantic")
    reasons.append("deferred to the adjudication pass; a write never waits on the embedder"
                   if semantic.configured() else "no embedder configured")
    return {"related": related[:6],
            "related_partial": {"missing": missing, "reason": "; ".join(r for r in reasons if r)}}


_RETIRE = {"note": "superseded", "directive": "superseded"}


def _retire(state, old_id, new_id, agent):
    """Flip the superseded record so every reader stops serving it. Types without a status to
    write are retired on the read side by the `supersedes` pointer alone. Best-effort — the
    correction is the valuable half — but a failure is REPORTED, never swallowed."""
    ent = (state.get("entities") or {}).get(old_id)
    if not ent:
        return "not-found"
    to = _RETIRE.get(ent.get("type"))
    if to is None:
        return "pointer-only"
    if ent.get("status") == to:
        return "already"
    payload = {k: v for k, v in ent.items() if k not in ("id", "version", "provenance")}
    payload["status"] = to
    if ent.get("type") == "note":
        payload["superseded_by"] = new_id
    try:
        _resp, status = _append(ent["type"], old_id, payload, expected_version=ent.get("version"),
                                agent=agent, idem="supersede:%s:%s" % (old_id, new_id),
                                etype="%s.%s" % (ent["type"], "created" if ent["type"] == "note" else "issued"))
    except Exception as exc:                                   # noqa: BLE001
        _report("lesson-retire", exc)
        return "failed"
    if status not in (200, 201):
        _report("lesson-retire", RuntimeError("HTTP %s" % status))
        return "failed"
    return "retired"


@writer(scope="note:write")
def lesson(request, b):
    """Record a RULE earned from a mistake, so the next agent does not re-derive it.

    Body: rule (required), why, tier (foundational|normal), verify (the command or URL that
    answers a state claim NOW), verified_as_of, tags, supersedes (an id this corrects).
    Superseding your own lesson's id (same rule, new story) corrects it IN PLACE."""
    agent = b.get("agent") or "agent"
    rule = _norm(b.get("rule") or b.get("statement") or b.get("title"))
    if not rule:
        return JsonResponse({"errors": [{"code": "need_rule", "msg": "a lesson needs its rule"}]}, status=400)
    try:
        eid = _lesson_id(rule, b.get("local"))
    except ValueError:
        return JsonResponse({"errors": [{"code": "invalid_local"}]}, status=400)
    old = _resolve(b.get("supersedes"))
    in_place = bool(old) and old == eid
    if in_place:
        old = ""
    state = hub_app.current_state()
    existing = (state.get("entities") or {}).get(eid)
    if not old and not in_place:
        dup = _identical_live_rule(state, rule)
        if dup is not None:
            # MERGED, not dropped: the restatement's story joins the canonical rule.
            rresp, rstatus = _reinforce(dup, agent, b.get("why") or "", "lesson")
            merged = rstatus in (200, 201)
            return JsonResponse({"data": {
                "id": dup.get("id"), "duplicate_of": dup.get("id"), "written": False,
                "version": ((rresp.get("data") or {}).get("version") if merged else None)
                or dup.get("version"),
                "reinforced": merged,
                "reinforced_count": len(dup.get("reinforced_by") or []) + (1 if merged else 0),
                "msg": "this exact rule is already on the board as %s; your story was added to "
                       "it (reinforced_by) instead of a second copy. To change the rule, write it "
                       "with supersedes=%s." % (dup["id"], dup["id"])}},
                status=200)
    if old and old not in (state.get("entities") or {}):
        return JsonResponse({"errors": [{"code": "no_such_record", "msg": old}]}, status=404)
    tier = str(b.get("tier") or "normal").lower()
    if tier not in ("normal", "foundational"):
        return JsonResponse({"errors": [{"code": "bad_tier", "msg": "normal | foundational"}]}, status=400)
    extra = [str(t).strip().lower() for t in (b.get("tags") or []) if str(t).strip()]
    payload = {"type": "note", "category": "gotcha", "title": rule,
               "body_md": str(b.get("why") or ""), "status": "standing", "tier": tier,
               "tags": list(dict.fromkeys(list(_LESSON_TAGS) + extra))}
    for key in ("verify", "verified_as_of"):
        if str(b.get(key) or "").strip():
            payload[key] = str(b[key]).strip()
    if old:
        payload["supersedes"] = old
    payload.update(overlap_tag(state, rule, eid))
    expected = existing.get("version") if existing else None
    if existing and b.get("expected_version") is not None:
        expected = b.get("expected_version")
    resp, status = _append("note", eid, payload, expected_version=expected, agent=agent,
                           idem=b.get("idem_key"), etype="note.created")
    if status not in (200, 201):
        return JsonResponse(resp, status=status)
    data = resp.setdefault("data", {})
    data.update({"written": True, "related": payload["related"],
                 "related_partial": payload["related_partial"]})
    if in_place:
        data["corrected_in_place"] = True
    if old:
        data["retired"] = {"id": old, "outcome": _retire(state, old, eid, agent)}
    return JsonResponse(resp, status=status)


# ── the record verbs: finding / method / review ──
#
# Filing everything as a lesson is how a board loses the difference between a FACT about how a
# system behaves (finding), a RULE earned from a mistake (lesson), a PROCEDURE the team follows
# (method), and a QUESTION ONLY A PERSON MAY ANSWER (review). Each lands as a note that says
# which it is. A gap (an ownable deficiency with a severity) keeps its own entity type.

METHOD_CATEGORIES = ("extraction", "analysis", "transformation", "verification", "governance",
                     "presentation")

_RECORDS = {
    # verb: (note category, kind tag, is standing knowledge)
    "finding": ("discovery", "finding"),
    "method": ("method", "method"),
    "review": ("risk", "review"),
}


def _record_writer(verb):
    category, tag = _RECORDS[verb]

    @writer(scope="note:write")
    def view(request, b):
        agent = b.get("agent") or "agent"
        title = _norm(b.get("title") or b.get("name"))
        if not title:
            return JsonResponse({"errors": [{"code": "need_title"}]}, status=400)
        tags = [tag]
        sub = str(b.get("category") or "").strip().lower()
        if verb == "method" and sub and sub not in METHOD_CATEGORIES:
            return JsonResponse({"errors": [{"code": "bad_category",
                                             "msg": " | ".join(METHOD_CATEGORIES)}]}, status=400)
        if sub:
            tags.append(re.sub(r"[^a-z0-9._-]+", "-", sub)[:40])
        tags += [str(t).strip().lower() for t in (b.get("tags") or []) if str(t).strip()]
        body = str(b.get("note") or b.get("body_md") or "")
        if b.get("evidence"):
            body = (body + "\n\nEvidence: " + str(b["evidence"])).strip()
        eid = b.get("id") or ids.make_id(hub_app.PROJECT_KEY, "note", "%s-%s" % (verb, _slug(title, verb)))
        payload = {"type": "note", "category": category, "title": title, "body_md": body,
                   "status": "standing", "tags": list(dict.fromkeys(tags))}
        related = [r for r in (b.get("relates_to") or []) if isinstance(r, str) and r.count(":") >= 2]
        if related:
            payload["relates_to"] = related
        for key in ("verify", "verified_as_of"):
            if str(b.get(key) or "").strip():
                payload[key] = str(b[key]).strip()
        state = hub_app.current_state()
        existing = (state.get("entities") or {}).get(eid)
        expected = b.get("expected_version")
        if verb == "finding" and expected is None:
            # A RESTATED FINDING JOINS THE ONE ON THE BOARD: the same title (so the same id),
            # or a live finding whose normalised title is this one. Its evidence is kept.
            target = existing
            if target is None:
                want = _norm_title(title)
                target = next((e for e in (state.get("entities") or {}).values()
                               if isinstance(e, dict) and e.get("type") == "note"
                               and tag in [str(t).lower() for t in (e.get("tags") or [])]
                               and not knowledge.is_dead(e, knowledge.superseded_ids(state))
                               and _norm_title(e.get("title")) == want), None)
            if target is not None:
                rresp, rstatus = _reinforce(target, agent, body, "finding")
                if rstatus not in (200, 201):
                    return JsonResponse(rresp, status=rstatus)
                return JsonResponse({"data": {
                    "id": target["id"], "reinforced": True, "written": False,
                    "version": (rresp.get("data") or {}).get("version"),
                    "reinforced_count": len(target.get("reinforced_by") or []) + 1,
                    "msg": "this finding is already on the board as %s; your evidence was added "
                           "to it (reinforced_by) instead of a second record" % target["id"]}})
        if existing and expected is None:
            # Re-filing the same titled record is an UPDATE of it; a caller that did not read
            # first gets the precondition answer with the current version, never a silent twin.
            return JsonResponse({"errors": [{"code": "precondition_required",
                "msg": "%s already exists; pass expected_version to update it" % eid,
                "id": eid, "current": existing.get("version")}]}, status=428)
        resp, status = _append("note", eid, payload, expected_version=expected, agent=agent,
                               idem=b.get("idem_key"), etype="note.created")
        return JsonResponse(resp, status=status)

    view.__name__ = verb
    return view


finding = _record_writer("finding")
method = _record_writer("method")
review = _record_writer("review")


# ── attest: attach the check that proves a record's STATE, without rewriting it ──

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@writer(scope="note:write")
def attest(request, b):
    """Attach the check that answers a knowledge record's claim NOW, and/or re-date it.

    Body: id (a full note id), verify (the command or URL that answers it now), verified_as_of
    (YYYY-MM-DD, the day it was last known true; defaults to today). At least one of the two.

    A state claim with no check is labelled UNVERIFIED on every knowledge surface until someone
    who knows the truth says where it lives. Only these two fields change: the record keeps its
    text, its id, its overlap tags and its history -- rewriting the whole record to add a check
    is how a correction silently loses the story behind it. A new check starts with a clean
    re-check state, so a CHECK FAILED against the OLD check is not carried onto the new one."""
    import datetime as _dt
    agent = b.get("agent") or "agent"
    eid = _resolve(b.get("id") or b.get("ref"))
    verify = _norm(b.get("verify"))
    asof = str(b.get("verified_as_of") or b.get("asof") or "").strip()[:10]
    if not eid:
        return JsonResponse({"errors": [{"code": "need_id", "msg": "the full id of a lesson, "
                                         "finding or method (search prints them)"}]}, status=400)
    if not verify and not asof:
        return JsonResponse({"errors": [{"code": "need_verify_or_asof",
            "msg": "attest needs verify (the command or URL that answers it now) and/or "
                   "verified_as_of (YYYY-MM-DD)"}]}, status=400)
    if asof and not _ISO_DAY.match(asof):
        return JsonResponse({"errors": [{"code": "bad_asof", "msg": "verified_as_of is YYYY-MM-DD"}]},
                            status=400)
    for attempt in (0, 1):
        state = hub_app.current_state()
        ent = (state.get("entities") or {}).get(eid)
        if not ent:
            return JsonResponse({"errors": [{"code": "not_found", "msg": eid}]}, status=404)
        kind = knowledge.knowledge_kind(ent)
        if ent.get("type") != "note" or kind is None or kind == "review":
            return JsonResponse({"errors": [{"code": "not_knowledge",
                "msg": "%s is not a lesson, finding or method" % eid}]}, status=409)
        payload = {"verified_as_of": asof or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")}
        if verify:
            payload["verify"] = verify
            if verify != _norm(ent.get("verify")) and isinstance(ent.get("recheck"), dict):
                # The old check's verdict does not describe the new check.
                payload["recheck"] = {"status": "skipped", "checked_at": payload["verified_as_of"],
                                      "detail": "check replaced by attest; not re-run yet"}
                payload["tags"] = [t for t in (ent.get("tags") or []) if t != "needs-review"]
        resp, status = _append("note", eid, payload, expected_version=ent.get("version"),
                               agent=agent, idem=b.get("idem_key"), etype="note.created")
        if status != 409 or attempt:
            break
    if status in (200, 201):
        resp.setdefault("data", {}).update(
            {"attested": {k: payload[k] for k in ("verify", "verified_as_of") if k in payload},
             "label": knowledge.label_of({**ent, **payload}, kind)})
    return JsonResponse(resp, status=status)
