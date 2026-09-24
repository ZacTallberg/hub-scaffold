"""Rebuild the board from a ledger copy and PROVE the rebuild is the same board.

Row counts and an integrity check prove a backup's bytes arrived. They do not answer the
question a backup exists for: IF WE RESTORED THIS, WOULD WE GET THE SAME BOARD? This module folds
a ledger copy in isolation and compares it with the live fold entity by entity, and the result
names the exact entities that differ instead of returning a bare verdict.

The live fold is taken AT THE COPY'S HEAD (events with ``seq`` up to the copy's last seq), so a
write that lands while the backup is being taken is not reported as a difference: the copy is
compared with the board as it stood when the copy was cut.

Three refusals, each one a way this check could otherwise pass while proving nothing:

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


def verify(live_root, rebuilt_root) -> dict:
    """Fold the copy at ``rebuilt_root`` and the live ledger at ``live_root`` up to the copy's head,
    and compare. ``ok`` is true only when entities were compared and none differed; otherwise
    ``why`` says what failed in words a backup log can print."""
    rebuilt_events = _events(rebuilt_root)
    head = rebuilt_events[-1]["seq"] if rebuilt_events else 0
    live_events = [e for e in _events(live_root) if e.get("seq", 0) <= head]
    report = compare(project.fold(live_events), project.fold(rebuilt_events))
    report.update({"source": str(rebuilt_root), "head_seq": head,
                   "head_hash": rebuilt_events[-1].get("hash", "") if rebuilt_events else ""})
    if not report["rebuilt_entities"]:
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
