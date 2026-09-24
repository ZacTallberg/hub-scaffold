"""Rebuild the board from a ledger copy and PROVE the rebuild is the same board.

Row counts and an integrity check prove a backup's bytes arrived. They do not answer the
question a backup exists for: IF WE RESTORED THIS, WOULD WE GET THE SAME BOARD? This module folds
a ledger copy in isolation and compares it with the live fold entity by entity, and the result
names the exact entities that differ instead of returning a bare verdict.

Both folds are taken AT A CURSOR RECORDED FROM THE LIVE LEDGER BEFORE THE COPY WAS CUT
(``live_cursor``: its head seq, that event's hash, and the file's size). A write that lands while
the backup is being taken is therefore not a difference, and -- the reason the cursor comes from
the SOURCE and never from the copy -- a copy missing a suffix of the ledger cannot pass by being
compared with the board only as far as it happens to reach. Folding "up to the copy's own head"
would call a truncated copy identical, and a short copy is the most likely damage there is.

Four refusals, each one a way this check could otherwise pass while proving nothing:

* COPY SHORTER THAN SOURCE -- the copy's head seq is below the recorded live cursor, or its event
  at that seq does not carry the recorded hash (a different or rewritten history).

* EMPTY SOURCE -- a copy that folds to no entities is a backup of nothing; with no entities, no
  entity differs, so agreement would be trivial.
* VACUOUS COMPARISON -- ``compared`` (how many ids were examined) is part of the result, and zero
  is a failure, never a pass.
* NO SHARED CACHE -- both sides are folded here, directly, from the events of the store each one
  names. A memoized read model keyed on anything other than the store's own chain could hand the
  copy the live board's entities, and the copy would agree with itself.

Standard library only. Nothing here writes to either ledger; opening the copy creates that
copy's own index sidecar, which is why a caller extracts it into an isolated directory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import project
from .store import EventStore

#: Fields that legitimately differ between two folds of the same events. Keep it EMPTY unless a
#: field is proven volatile: every name here is a difference the comparison stops being able to see.
VOLATILE_FIELDS: tuple = ()


def _canonical(entity: dict) -> str:
    body = {k: v for k, v in entity.items() if k not in VOLATILE_FIELDS}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def entity_digests(entities: dict) -> dict:
    """{entity id: sha256 of its canonical form}."""
    return {str(eid): hashlib.sha256(_canonical(ent).encode("utf-8")).hexdigest()
            for eid, ent in (entities or {}).items() if isinstance(ent, dict)}


def board_digest(entities: dict) -> str:
    """One digest over the whole board, so a manifest can carry the board's identity in a field."""
    per = entity_digests(entities)
    joined = "\n".join("%s %s" % (eid, per[eid]) for eid in sorted(per))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _events(root) -> list:
    store = EventStore(Path(root))
    try:
        return store.events()
    finally:
        store.close()                  # a leaked handle is a real file lock on Windows


def compare(live: dict, rebuilt: dict) -> dict:
    live_d, rebuilt_d = entity_digests(live), entity_digests(rebuilt)
    live_ids, rebuilt_ids = set(live_d), set(rebuilt_d)
    changed = sorted(i for i in (live_ids & rebuilt_ids) if live_d[i] != rebuilt_d[i])
    missing = sorted(live_ids - rebuilt_ids)
    extra = sorted(rebuilt_ids - live_ids)
    return {
        "compared": len(live_ids | rebuilt_ids),
        "live_entities": len(live_ids),
        "rebuilt_entities": len(rebuilt_ids),
        "identical": not (changed or missing or extra),
        "changed": changed, "missing": missing, "extra": extra,
        "live_digest": board_digest(live), "rebuilt_digest": board_digest(rebuilt),
    }


def live_cursor(live_root) -> dict:
    """The live ledger's head as it stands NOW: ``{"seq", "hash", "bytes"}``. Take it BEFORE
    bundling a copy and hand it to ``verify``; the copy must reach at least this far."""
    root = Path(live_root)
    jsonl = root / "events.jsonl"
    size = jsonl.stat().st_size if jsonl.is_file() else 0
    store = EventStore(root)
    try:
        head = store.latest_cursor()
    finally:
        store.close()
    return {"seq": int(head.get("seq") or 0), "hash": str(head.get("hash") or ""), "bytes": size}


def verify(live_root, rebuilt_root, cursor: dict | None = None) -> dict:
    """Fold the copy at ``rebuilt_root`` and the live ledger at ``live_root`` up to ``cursor`` (a
    ``live_cursor`` recorded before the copy was cut; taken now when omitted), and compare. ``ok``
    is true only when the copy reaches the cursor with the same event there, entities were
    compared, and none differed; otherwise ``why`` says what failed in words a backup log can
    print."""
    if cursor is None:
        cursor = live_cursor(live_root)
    head, head_hash = int(cursor.get("seq") or 0), str(cursor.get("hash") or "")
    rebuilt_all = _events(rebuilt_root)
    copy_head = rebuilt_all[-1]["seq"] if rebuilt_all else 0
    at_head = next((e for e in rebuilt_all if e.get("seq") == head), None)
    rebuilt_events = [e for e in rebuilt_all if e.get("seq", 0) <= head]
    live_events = [e for e in _events(live_root) if e.get("seq", 0) <= head]
    report = compare(project.fold(live_events), project.fold(rebuilt_events))
    report.update({"source": str(rebuilt_root), "head_seq": head, "head_hash": head_hash,
                   "copy_head_seq": copy_head})
    if copy_head < head:
        report.update(ok=False, why="copy shorter than source: the copy ends at seq %d, the live "
                      "ledger was at seq %d before the copy was cut" % (copy_head, head))
    elif head and (at_head is None or (head_hash and at_head.get("hash") != head_hash)):
        report.update(ok=False, why="copy diverges from source: its event at seq %d does not "
                      "carry the live hash %s" % (head, head_hash[:12] or "-"))
    elif not report["rebuilt_entities"]:
        report.update(ok=False, why="the ledger copy folded to NO entities -- a backup that "
                                    "rebuilds an empty board proves nothing")
    elif not report["compared"]:
        report.update(ok=False, why="the comparison examined 0 entities, so it cannot have "
                                    "found a difference -- refusing a vacuous pass")
    elif not report["identical"]:
        shown = report["changed"][:5] + report["missing"][:5] + report["extra"][:5]
        report.update(ok=False, why="the rebuilt board differs from the live one: %d changed, "
                      "%d missing, %d extra (%s)" % (len(report["changed"]),
                                                     len(report["missing"]),
                                                     len(report["extra"]),
                                                     ", ".join(shown) or "-"))
    else:
        report.update(ok=True, why="")
    return report
