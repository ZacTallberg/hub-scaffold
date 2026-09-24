"""Bounded agent-to-agent escalation: what an unattended responder may take, and when.

Every question an unattended run raises carries its escalation depth as a FIELD (``hop``), set by
the process, not by the model: the launcher exports ``HUB_RESPONDER_HOP`` (one deeper than the
item it was given) and ``python -m hub_core.client ask`` stamps it. Then:

* hop 0 — a person's (or an attended session's) question: workable at once.
* hop 1 — an escalation: an unattended run stopped and asked. Workable ONCE MORE, by whichever
  machine wins the claim, after ``COOLDOWN_S``: long enough that whatever the first pass was
  waiting on (a pipeline, a deploy) has finished, and that the machine which raised it is not
  simply re-run on the same state.
* hop 2+ — what THAT second pass raised. A person's: two passes on two occasions could not clear
  it. A chain is therefore bounded at two hops, each at least a cooldown apart.

Refusing EVERY escalation forever bounds chains too, but it turns every escalation into a
person-only queue: on the board this rule came from, 29 of 30 open questions were mechanical
follow-ups ("the pipeline was not green at my clock", "please retry the job") that no responder
could touch. Retaking none and retaking all are both wrong; one more pass is the bound.

``workable()`` is the ONE predicate: the scan that decides whether to spend a session counts
exactly what ``respond`` will take. A scan that counts items the responder then refuses launches
passes that are killed at the ceiling with nothing to show.
"""
from __future__ import annotations

import re

MAX_HOP = 2
COOLDOWN_S = 1800
_PROSE_STAMP = re.compile(r"via=responder(?:\s+hop=(\d+))?")


def hop_of(item: dict) -> int:
    """The item's escalation depth: the structured field, else a legacy prose stamp
    (``via=responder hop=N``; a bare ``via=responder`` counts as hop 1)."""
    try:
        structured = int(item.get("hop") or 0)
    except (TypeError, ValueError):
        structured = 0
    text = "%s %s" % (item.get("title") or "", item.get("body") or "")
    prose = [int(m.group(1) or 1) for m in _PROSE_STAMP.finditer(text)]
    return max([structured] + prose)


def workable(item: dict) -> tuple[bool, str]:
    """(may an unattended responder take it now, why)."""
    hop = hop_of(item)
    if hop >= MAX_HOP:
        return False, "hop %d of an unattended chain: a person's" % hop
    if not hop:
        return True, "raised by a person or an attended session"
    if not client_stamps_hops():
        return False, "an escalation, and this machine's client cannot stamp the next hop"
    age = item.get("age_s")
    if age is None:
        return False, "an escalation whose age cannot be read: waiting (fail closed)"
    if int(age) < COOLDOWN_S:
        return False, "an escalation %ds old: taken once more after %ds" % (int(age), COOLDOWN_S)
    return True, "an escalation past its cooldown: one more pass"


def client_stamps_hops() -> bool:
    """Can the client a session will use stamp the next hop? Fail closed if not: a session whose
    client drops the stamp would raise questions that read as hop 0 again, and the chain the hop
    count exists to bound would not be. The launcher points sessions at THIS package's client
    (PYTHONPATH), so this reads that file."""
    from pathlib import Path
    try:
        source = (Path(__file__).resolve().parent.parent / "client.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "HUB_RESPONDER_HOP" in source


def next_hop(item: dict) -> int:
    """What a pass working this item stamps on anything it raises."""
    return hop_of(item) + 1
