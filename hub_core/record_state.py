"""Knowledge-record lifecycle: which records are DEAD, and how a live one is retired.

Two questions every knowledge path has to answer the same way, defined once here:

1. **Is this record dead?** A record whose status says the claim is no longer true must never be
   served as current fact — not by search, not by a guidance feed, not by an agent's lookup. The
   dead statuses are spelled out ONCE (``DEAD_STATUS``) because a list copied into several readers
   drifts: a status added to one schema (``stale``: "was true and no longer is") was honoured by
   none of the copies, and retired records kept being ranked into answers.

   ``resolved`` / ``closed`` are deliberately NOT dead: they record what happened and why, which
   is exactly what the next person hitting the same symptom needs. Dead means the CLAIM itself is
   no longer true.

2. **Is it overruled by a pointer?** Any id named in the ``supersedes`` of a record that is itself
   live is retired whatever its own status says. The pointer is the authority, so a retirement
   write that failed — or a type with no status field to write — cannot leave an overruled record
   in front of a reader.

And one question the write path asks: **how does a record of THIS type retire?** Each type
retires to a status its own schema enumerates, and a retirement needs a reason: opening a record
needs no evidence, retiring one does, or a closed row is indistinguishable from one somebody found
annoying. The reason is APPENDED to the record's text with a dated stamp, never written over what
was there, so the history survives and anyone who disagrees can re-open it.

Standard library only; the adapter supplies the entity and performs the append.
"""
from __future__ import annotations

from datetime import datetime, timezone

DEAD_STATUS = ("superseded", "dropped", "rejected", "retracted", "stale")

#: Per type: the statuses a retirement call may set, the status it sets when none is named, the
#: statuses that RE-OPEN (and so need no reason), and the text field the dated reason is appended
#: to. ``finding`` is listed for adopters who add that optional entity type (see
#: campaigns/augment-hub.md); a type absent from the registry simply never matches an entity.
LIFECYCLE = {
    "gap": {"statuses": ("open", "investigating", "mitigated", "closed", "wont-fix"),
            "default": None, "reopen": ("open", "investigating"), "text": "evidence"},
    "note": {"statuses": ("standing", "superseded"),
             "default": "superseded", "reopen": ("standing",), "text": "body_md"},
    "directive": {"statuses": ("active", "superseded", "fulfilled", "expired"),
                  "default": "superseded", "reopen": ("active",), "text": "body_md"},
    "adr": {"statuses": ("superseded", "deprecated", "rejected"),
            "default": "superseded", "reopen": (), "text": "amendments_md"},
    "finding": {"statuses": ("observed", "confirmed", "resolved", "stale"),
                "default": "stale", "reopen": ("observed", "confirmed"), "text": "evidence"},
}

#: Statuses whose own schema demands the closing work be named (gap: "Task(s) that close this
#: gap", minItems 1). Checked here so the refusal names the flag instead of surfacing a raw
#: schema error with nothing saying what to pass.
NEEDS_ADDRESSED_BY = {"gap": ("mitigated", "closed")}


class RetireRefused(ValueError):
    """A retirement the rules refuse; ``code`` is machine-readable, the message says what to do."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_dead(ent) -> bool:
    return isinstance(ent, dict) and ent.get("status") in DEAD_STATUS


def _targets(value):
    if isinstance(value, list):
        return [str(v or "").strip() for v in value]
    return [str(value or "").strip()]


def superseded_ids(entities) -> frozenset:
    """Every id named in the ``supersedes`` of a record that is itself live."""
    out = set()
    for ent in (entities.values() if isinstance(entities, dict) else entities):
        if not isinstance(ent, dict) or not ent.get("supersedes") or is_dead(ent):
            continue
        for target in _targets(ent.get("supersedes")):
            if target and target != ent.get("id"):
                out.add(target)
    return frozenset(out)


def is_retired(ent, superseded=frozenset()) -> bool:
    """Dead by its own status, or overruled by a live record's ``supersedes`` pointer."""
    return is_dead(ent) or (isinstance(ent, dict) and ent.get("id") in superseded)


def plan_retirement(ent: dict, *, status: str | None = None, note: str = "", agent: str = "agent",
                    addressed_by=None, superseded_by: str = "", now: datetime | None = None) -> dict:
    """Return the PARTIAL payload that moves ``ent`` to ``status``, or raise RetireRefused.

    The payload carries only what changes: the status, the reason appended (dated, attributed)
    to the type's text field, and — where the schema has one — the pointer to what replaced it.
    """
    otype = str(ent.get("type") or "")
    rules = LIFECYCLE.get(otype)
    if rules is None:
        raise RetireRefused("not_retirable",
                            f"{otype or 'this record'} has no retirement lifecycle; retirable types "
                            f"are {', '.join(sorted(LIFECYCLE))}")
    status = (status or rules["default"] or "").strip().lower()
    if not status:
        raise RetireRefused("need_status", f"name the status: one of {', '.join(rules['statuses'])}")
    if status not in rules["statuses"]:
        raise RetireRefused("bad_status", f"a {otype} can be set to {', '.join(rules['statuses'])}, "
                                          f"not {status!r}")
    note = str(note or "").strip()
    reopening = status in rules["reopen"]
    if not reopening and not note:
        raise RetireRefused("need_note", f"moving a {otype} to {status} requires a note saying what "
                                         f"retired it — the reason is the record")
    payload: dict = {"status": status}
    if status in NEEDS_ADDRESSED_BY.get(otype, ()):
        named = [str(v).strip() for v in (addressed_by or []) if str(v or "").strip()]
        merged = list(dict.fromkeys(list(ent.get("addressed_by") or []) + named))
        if not merged:
            raise RetireRefused("need_addressed_by", f"a {status} {otype} must name the work that "
                                                     f"closed it: pass addressed_by=[<task id>]")
        payload["addressed_by"] = merged
    by = str(superseded_by or "").strip()
    if by and by == ent.get("id"):
        raise RetireRefused("self_supersede", "a record cannot be superseded by itself")
    if by and otype == "adr":
        payload["superseded_by"] = list(dict.fromkeys(list(ent.get("superseded_by") or []) + [by]))
    if note:
        stamp_day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        stamp = f"[{status} {stamp_day} by {agent}] {note}" + (f" (superseded by {by})" if by else "")
        prior = str(ent.get(rules["text"]) or "").rstrip()
        payload[rules["text"]] = (prior + "\n\n" + stamp) if prior else stamp
    return payload
