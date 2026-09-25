"""What the board KNOWS, as opposed to what it is doing — one vocabulary for every surface.

Search, the per-prompt memory index, the mirror feed and the lesson overlap tagger all have to
answer the same three questions about a record, and every place that answered them separately
drifted: a record was knowledge on one surface and absent from another, a retired rule kept
riding every prompt because one reader checked `status` and another checked `supersedes`, and
a field list written for one type silently indexed six others by title alone.

  * IS IT KNOWLEDGE, and of what kind?          `knowledge_kind()`
  * IS IT RETIRED?                               `is_dead()` / `superseded_ids()`
  * WHAT TEXT IS IT FOUND BY?                    `text_of()` (per type, never a shared accessor)

Knowledge records ride the base entity types — the scaffold keeps lessons, findings and
methods as tagged `note`s rather than minting optional types — so the kind is read from what a
record CARRIES (its tags and links), never from an id prefix.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import re

from . import record_state

#: A record in one of these states is history. It must never be served as current knowledge.
#: ONE definition, owned by hub_core.record_state (the retire verb writes these statuses):
#: search, the prompt index, the feed and the overlap tagger all read it through here.
DEAD_STATUS = frozenset(record_state.DEAD_STATUS)

#: WHERE EACH TYPE KEEPS ITS MEANING — per type, read off the schemas. A shared field list is a
#: proxy for what a record contains: a generic accessor once measured tasks at 88 characters
#: and ADRs at 66, understating half the types by up to 26x, because it read fields those
#: records do not use. A zero from a missing field and a zero from an empty one are
#: indistinguishable once they reach a total, so each type names its own fields.
#:
#: EXCLUDED BY DECISION: `deploy` (a sha, a result and a marker — a structured question the
#: deploy surfaces answer), `ack` (a signature on a directive, which is itself indexed) and
#: `run` (an execution log, not a statement about the world).
FIELDS = {
    "note": ("title", "body_md"),
    "task": ("title", "acceptance", "plan"),
    "directive": ("title", "body_md"),
    "gap": ("title", "evidence"),
    "cap": ("name", "needs", "iface", "pivot_notes"),
    "adr": ("title", "context_md", "decision_md", "consequences_md"),
    "feat": ("name", "summary"),
}
INDEXED_TYPES = tuple(FIELDS)

#: Tags that make a note standing knowledge (the ask/answer loop's crystallized notes carry
#: `pattern` + `memory`; the record verbs carry their own kind tag).
MEMORY_TAGS = frozenset({"pattern", "memory", "solution"})
KIND_TAGS = ("lesson", "finding", "method", "review")

#: A rough prior for a local ranker: what an agent most often needs first.
KIND_RANK = {"answer": 0, "lesson": 1, "method": 2, "finding": 3, "review": 4, "note": 5, "capability": 6}


def _tags(ent) -> set:
    return {str(t).lower() for t in (ent.get("tags") or [])}


def is_chatter(ent) -> bool:
    """A record that is TRAFFIC, not knowledge — judged on what it carries, never its id.

    Two classes, both measured as the top result for real questions on the instance this was
    lifted from:
      * inter-agent MESSAGES (a `message` tag, or a recipient). Where notes double as a
        message bus they can be a third of the index and take the top of every query.
      * QUESTIONS. The top hit for a question was the question itself, restated: an agent that
        asks and receives its own words back has been handed noise, and the record that
        resolved it is pushed down. The resolution lives in the ANSWER (a directive whose body
        carries what was done), which is indexed; an unanswered question is a person still
        waiting, which the inbox surfaces.
    """
    if str(ent.get("type") or "") != "note":
        return False
    tags = _tags(ent)
    if "message" in tags or ent.get("to"):
        return True
    return "question" in tags


def knowledge_kind(ent):
    """What kind of knowledge a ledger record is, or None when it is not knowledge at all."""
    t = ent.get("type")
    if t == "directive" and ent.get("answers"):
        # WHAT AGENTS WERE TOLD WHEN THEY WERE STUCK: a real question and the reply that
        # resolved it. Leaving these out made the prompt index's recall on real asks zero by
        # construction.
        return "answer"
    if t != "note" or is_chatter(ent):
        return None
    tags = _tags(ent)
    for kind in KIND_TAGS:
        if kind in tags:
            return kind
    return "note" if tags & MEMORY_TAGS else None


def superseded_ids(state) -> frozenset:
    """Every id named in the `supersedes` of a record that is itself live. The POINTER is the
    authority: a retirement write that failed, or a type with no status to write, cannot leave
    the overruled record in front of an agent. Delegates to hub_core.record_state."""
    return record_state.superseded_ids(state.get("entities") or {})


def is_dead(ent, superseded=frozenset()) -> bool:
    return record_state.is_retired(ent, superseded)


def _flatten(value) -> str:
    """A field's text whatever its shape. A task's `plan` is a LIST of checkpoint dicts, and
    the note on each is the trail of what was found — the reason a task is worth indexing.
    Stringifying the dict instead would index the word "note_at" once per checkpoint."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(p for p in (_flatten(v) for v in value) if p)
    if isinstance(value, dict):
        return " ".join(p for p in (_flatten(v) for k, v in value.items()
                                    if k in ("step", "note", "text", "title", "detail")) if p)
    return ""


#: A sentence naming another record by id ("unrelated to proj:note:l-3f80..."). Authors write
#: these as rebuttals of past false overlap matches; embedding or indexing them would pull the
#: two records together and teach the matcher its own mistake.
_RECORD_REF = re.compile(r"\b[a-z0-9][a-z0-9-]*:(?:task|note|directive|gap|adr|feat|cap):[\w.\-]+", re.I)


def text_of(ent) -> str:
    """The text a record is FOUND BY — the ONE accessor both retrieval channels read, so a
    type cannot be indexed fully in one channel and by title alone in the other."""
    fields = FIELDS.get(str(ent.get("type") or ""))
    if not fields:
        return ""
    body = " ".join(p for p in (_flatten(ent.get(f)) for f in fields) if p)
    kept = [s for s in re.split(r"(?<=[.!?])\s+", body) if not _RECORD_REF.search(s)]
    tags = " ".join(str(t) for t in (ent.get("tags") or []))
    return " ".join(p for p in (" ".join(kept), tags) if p).strip()


def body_of(ent) -> str:
    """The text a record carries BESIDE its headline — an excerpt that repeats the title (or
    ends in the record's own tags) tells a reader nothing the row did not already say."""
    fields = FIELDS.get(str(ent.get("type") or ""))
    if not fields:
        return ""
    if ent.get("type") == "directive" and ent.get("answers"):
        return _ANSWER_TAIL.sub("", str(ent.get("body_md") or "")).strip()
    return " ".join(p for p in (_flatten(ent.get(f)) for f in fields[1:]) if p).strip()


def title_of(ent) -> str:
    return str(ent.get("title") or ent.get("name") or str(ent.get("id") or "").rsplit(":", 1)[-1])


def searchable(ent, superseded=frozenset()) -> bool:
    """In the retrieval corpus: an indexed type, live, and not chatter."""
    return (isinstance(ent, dict) and ent.get("type") in FIELDS
            and not is_dead(ent, superseded) and not is_chatter(ent))


# ── record text as delivered: bounded, marked when cut ──

TITLE_PREVIEW = 110


def clip(value, limit) -> str:
    """Whitespace-normalized and cut at a word boundary WITH a marked ellipsis: a rule amputated
    mid-instruction must never read as a complete one."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


_ANSWER_TAIL = re.compile(r"\n+---\nIn answer to your question:.*\Z", re.S)


def rule_and_why(ent, kind) -> tuple:
    """(rule, why) — the record's own statement and the story behind it."""
    if kind == "answer":
        return _ANSWER_TAIL.sub("", str(ent.get("body_md") or "")), ""
    if ent.get("type") == "note":
        return str(ent.get("title") or ""), str(ent.get("body_md") or "")
    return str(ent.get("title") or ent.get("name") or ""), str(ent.get("body_md") or "")


def headline(ent, kind) -> str:
    """The row's headline: an answer is titled by the QUESTION it resolved."""
    title = title_of(ent)
    if kind == "answer" and title.lower().startswith("answer: "):
        title = title[8:]
    return title


def memory_rows(state) -> list:
    """Every live knowledge record as an index row, in STANDING ORDER: foundational first,
    then most recently updated. Recency alone is inverted against value — the earliest records
    are the environment facts that cost days to learn, and they fall out of any cap first."""
    rows = []
    superseded = superseded_ids(state)
    for ent in (state.get("entities") or {}).values():
        if not isinstance(ent, dict):
            continue
        kind = knowledge_kind(ent)
        if kind is None or is_dead(ent, superseded):
            continue
        rule, why = rule_and_why(ent, kind)
        prov = ent.get("provenance") or {}
        rows.append({"id": ent["id"], "type": kind,
                     "title": clip(headline(ent, kind), TITLE_PREVIEW),
                     "tier": ent.get("tier") or "normal",
                     # A STATE claim decays from the day it is written. The row carries the
                     # day it was last checked and the command that re-checks it, so the
                     # injected index can say so instead of presenting a stale measurement
                     # as standing law.
                     "verified_as_of": ent.get("verified_as_of") or "",
                     "verify": str(ent.get("verify") or "")[:200],
                     "rule": rule, "why": why,
                     "updated": prov.get("updated_at") or ""})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    rows.sort(key=lambda r: r["tier"] != "foundational")
    return rows


def delivered_key(row) -> str:
    """One record AS DELIVERED: its id plus a hash of the text it carried, so an edited record
    counts as new while an unchanged one is never sent twice."""
    text = "%s|%s|%s" % (row.get("title") or "", row.get("rule") or "", row.get("why") or "")
    return "%s#%s" % (row.get("id") or "?", hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10])


# ── the append-only mirror feed (/hub/knowledge/since) ──
#
# A mirror asks once for everything and then only for what changed, and ranks at home. The
# cursor is OPAQUE to the client and has two parts, one per source:
#   L<seq>   the ledger: records whose last event is after seq (provenance.seq, stamped by the fold)
#   C<sha>   the capability catalog, sent whole (`reset`) when it changes
# Ops: `put` (the record as it is now), `revoke` (drop it), `reset` (drop every record of
# `source` not in the puts that follow). A bare integer cursor is read as a ledger seq.

FEED_MAX = 500
FEED_TEXT_CHARS = 8000


def parse_cursor(raw) -> dict:
    cur = {"L": 0, "C": ""}
    raw = str(raw or "").strip()
    if raw.isdigit():
        cur["L"] = int(raw)
        return cur
    for part in raw.split("."):
        if not part:
            continue
        tag, val = part[0], part[1:]
        if tag == "L" and val.isdigit():
            cur["L"] = int(val)
        elif tag == "C":
            cur["C"] = re.sub(r"[^0-9a-f]", "", val)[:16]
    return cur


def cursor_str(cur) -> str:
    return "L%d.C%s" % (int(cur.get("L") or 0), cur.get("C") or "-")


def _sha(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:16]


def put_op(ent, kind) -> dict:
    prov = ent.get("provenance") or {}
    rule, why = rule_and_why(ent, kind)
    title = clip(headline(ent, kind), TITLE_PREVIEW)
    rule, why = clip(rule, FEED_TEXT_CHARS), clip(why, FEED_TEXT_CHARS)
    item = {"op": "put", "id": ent["id"], "type": kind, "title": title, "rule": rule, "why": why,
            "status": ent.get("status") or "", "tier": ent.get("tier") or "normal",
            "tags": list(ent.get("tags") or [])[:24],
            "written_at": prov.get("created_at") or "", "updated_at": prov.get("updated_at") or "",
            "author": prov.get("agent") or "", "verified_as_of": ent.get("verified_as_of") or "",
            "verify": str(ent.get("verify") or "")[:400], "version": ent.get("version") or 0,
            "seq": int(prov.get("seq") or 0), "kind_rank": KIND_RANK.get(kind, 9),
            "text_sha": _sha(title, rule, why)}
    for key in ("answers", "supersedes", "superseded_by"):
        if ent.get(key):
            item[key] = ent[key]
    return item


def ledger_ops(state, since: int, limit: int, visible=None) -> tuple:
    """(ops, last_seq_taken, more) for knowledge records changed after `since`.

    `visible` is a narrowed reader's record filter (the veil). A REVOKE CARRIES AN ID, and ids
    are often slugs of titles: a revoke for a record the reader was never shown tells it the
    record existed. The response scrub drops a revoke whose id or reason NAMES a hidden term,
    but a record hidden by its tag alone would still leak its id -- and the reader's mirror
    never held it, so there is nothing to revoke. With `visible`, such revokes are not sent."""
    entities = state.get("entities") or {}

    def shown(eid) -> bool:
        if visible is None:
            return True
        ent = entities.get(eid)
        try:
            return bool(ent) and bool(visible(ent))
        except Exception:                                    # noqa: BLE001 - fail closed
            return False
    changed = []
    for ent in (state.get("entities") or {}).values():
        if not isinstance(ent, dict):
            continue
        seq = int((ent.get("provenance") or {}).get("seq") or 0)
        if seq <= since:
            continue
        # A record that STOPPED being knowledge (retired, or re-tagged) still has to reach the
        # mirror as a revoke, so kind is judged ignoring death here.
        kind = knowledge_kind(ent)
        if kind is None and not (ent.get("type") == "note" and ent.get("status") in DEAD_STATUS):
            continue
        changed.append((seq, ent, kind))
    changed.sort(key=lambda c: c[0])
    more = len(changed) > limit
    superseded = superseded_ids(state)
    ops = []
    for seq, ent, kind in changed[:limit]:
        if kind is None or is_dead(ent, superseded):
            if shown(ent["id"]):
                ops.append({"op": "revoke", "id": ent["id"], "reason": ent.get("status") or "retired",
                            "at": (ent.get("provenance") or {}).get("updated_at") or "", "seq": seq})
            continue
        ops.append(put_op(ent, kind))
        # A put that supersedes something retires it in the same page: the target's own seq may
        # be older than the mirror's cursor, so it would never be revisited.
        target = ent.get("supersedes")
        for t in (target if isinstance(target, list) else [target]):
            if t and t != ent["id"] and shown(str(t)):
                ops.append({"op": "revoke", "id": str(t), "reason": "superseded by %s" % ent["id"],
                            "seq": seq})
    last = changed[min(limit, len(changed)) - 1][0] if changed and more else None
    return ops, last, more


def catalog_ops(items) -> list:
    ops = [{"op": "reset", "source": "capability"}]
    for i in items:
        what, when = clip(i.get("what"), FEED_TEXT_CHARS), clip(i.get("when"), 2000)
        ops.append({"op": "put", "id": i.get("id") or ("capability:%s" % i["name"]),
                    "type": "capability", "title": "%s (%s)" % (i["name"], i.get("kind") or "capability"),
                    "rule": what, "why": when, "get": str(i.get("get") or "")[:1000],
                    "kind_rank": KIND_RANK["capability"], "text_sha": _sha(i["name"], what, when)})
    return ops
