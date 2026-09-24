"""Who a ready task may be OFFERED to -- one rule shared by the readiness rail and atomic pull.

A task can be ready and still not be the caller's to pull. Three facts decide it, each a field on
the task, and each is checked in exactly one place so the rail (``next.json``) and ``take`` can
never disagree about who may have it:

* ``assigned_to`` -- the task was GIVEN to a named agent (``POST /hub/api/hand``). Until they
  claim it, nobody else is offered it.
* ``machine`` -- MACHINE AFFINITY: the task's input exists on one machine only (a file in a
  person's downloads, a host-only share). A worker on another machine cannot do it, so it is
  offered only to a caller that declares that machine. A caller that declares NO machine is not
  offered an affinity task either: "I did not say where I am" cannot satisfy "only there".
* ``hop`` -- ESCALATION DEPTH. A task or question raised by an UNATTENDED run carries how many
  unattended hops deep it is. An unattended caller may take a hop-1 item once more, after a
  cooldown; hop 2 and beyond is a person's. Refusing every stamped item forever leaves every
  escalation waiting for a human; offering every one lets two agents bounce work between
  themselves indefinitely. Attended callers (a person's session) see everything.

A withheld row is never silently dropped: ``withheld`` returns the reason, and the rail reports
the counts by reason, because a queue that quietly hides work reads exactly like an empty one.

Pure and standard-library only.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

#: The deepest hop an unattended caller may still take. Hop 1 = raised by one unattended run.
MAX_UNATTENDED_HOP = 1
#: How long a hop-1 escalation waits before an unattended caller may take it once more, so the
#: pass that raised it is not immediately the pass that re-takes it.
HOP_COOLDOWN_S = 30 * 60

_MARK = re.compile(r"via=responder(?:\s+hop=(\d+))?")


def _norm(value) -> str:
    return str(value or "").strip().lower()


def hop_of(entity) -> int:
    """How many unattended hops deep this item is: the structured ``hop`` field, or the deepest
    ``via=responder hop=N`` marker in its text (a bare marker is hop 1). 0 = raised by a person
    or an attended session. The DEEPEST wins, so a model that typed a bare marker into a hop-2
    item can never re-open a chain that was meant to end."""
    if not isinstance(entity, dict):
        return 0
    hops = []
    try:
        hops.append(max(0, int(entity.get("hop") or 0)))
    except (TypeError, ValueError):
        pass
    text = " ".join(str(entity.get(k) or "") for k in ("title", "acceptance", "body_md"))
    hops.extend(int(m.group(1) or 1) for m in _MARK.finditer(text))
    return max(hops or [0])


def _epoch(stamp) -> float:
    text = str(stamp or "").strip().replace("Z", "+00:00")
    if not text:
        return 0.0
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def withheld(task: dict, *, agent: str = "", machine: str = "", unattended: bool = False,
             now: float | None = None) -> str:
    """Why this ready task is NOT offered to this caller, or "" when it may be.

    `agent` "" means the caller did not identify itself: assignment is then reported on the row
    (``assigned_to``) rather than used to hide it, so an operator's view of the rail stays whole.
    """
    now = time.time() if now is None else now
    owed = _norm(task.get("assigned_to"))
    who = _norm(agent)
    if owed and who and owed != who:
        return "assigned to %s" % owed
    want = _norm(task.get("machine"))
    if want:
        mine = _norm(machine)
        if not mine:
            return "only %s can do it (its input is there), and the caller named no machine" % want
        if mine != want:
            return "only %s can do it (its input is there)" % want
    if unattended:
        hop = hop_of(task)
        if hop > MAX_UNATTENDED_HOP:
            return ("raised %d unattended hops deep: a person's, never another unattended run's"
                    % hop)
        if hop == MAX_UNATTENDED_HOP:
            prov = task.get("provenance") or {}
            born = _epoch(prov.get("created_at") or prov.get("updated_at"))
            if born and now - born < HOP_COOLDOWN_S:
                return ("an unattended run's escalation: offered once more after %d min"
                        % max(1, int((HOP_COOLDOWN_S - (now - born)) // 60) + 1))
    return ""


def caller(request_get, headers) -> dict:
    """The caller facts a read or a pull carries: ``?agent=``/``?machine=``/``?unattended=``
    or the presence headers every client write already sends."""
    def pick(query, header):
        return str(request_get.get(query) or headers.get(header) or "").strip()
    flag = pick("unattended", "X-Hub-Unattended").lower()
    return {"agent": pick("agent", "X-Hub-Agent"),
            "machine": pick("machine", "X-Hub-Machine"),
            "unattended": flag in ("1", "true", "yes")}
