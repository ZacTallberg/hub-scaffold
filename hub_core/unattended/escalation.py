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
    """The item's escalation depth.

    The hub STAMPS ``hop`` on an ask from the caller's own process (never the model), so when
    the field is present it is the AUTHORITY -- including ``0``, which is a meaningful value (a
    person's question that merely QUOTES a responder's words is still hop 0). Only an item
    without the field (an older hub, or a task whose text carries the marker) falls back to the
    legacy prose stamp (``via=responder hop=N``; a bare ``via=responder`` counts as hop 1)."""
    raw = item.get("hop")
    if raw is not None:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    text = "%s %s" % (item.get("title") or "", item.get("body") or "")
    prose = [int(m.group(1) or 1) for m in _PROSE_STAMP.finditer(text)]
    return max([0] + prose)


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
    """Does the client a session will use actually stamp the next hop? Fail closed if not: a
    session whose client drops the stamp would raise questions that read as hop 0 again, and the
    chain the hop count exists to bound would not be.

    This checks BEHAVIOUR, not text: it parses a real ``ask`` command line through the client's own
    parser, with a sentinel ``HUB_RESPONDER_HOP`` in the environment, runs the payload builder the
    parser dispatches to, and requires the sentinel back in ``payload["hop"]``. A file that merely
    MENTIONS the variable (a comment, a help string) passes a text search with the stamping code
    deleted; it cannot pass this. The launcher points sessions at THIS package's client
    (PYTHONPATH), so this exercises that module. Any exception is a no."""
    import os

    sentinel = MAX_HOP + 5  # a value no default, argument or cached stamp could produce
    previous = os.environ.get("HUB_RESPONDER_HOP")
    try:
        from .. import client

        os.environ["HUB_RESPONDER_HOP"] = str(sentinel)
        arguments = client._parser().parse_args(["ask", "--question", "hop stamp self-check"])
        verb, payload = arguments.payload(arguments)
        return verb == "ask" and payload.get("hop") == sentinel
    except (Exception, SystemExit):  # argparse exits via SystemExit; every failure is a no
        return False
    finally:
        if previous is None:
            os.environ.pop("HUB_RESPONDER_HOP", None)
        else:
            os.environ["HUB_RESPONDER_HOP"] = previous


def next_hop(item: dict) -> int:
    """What a pass working this item stamps on anything it raises."""
    return hop_of(item) + 1
