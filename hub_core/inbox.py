"""Addressed delivery: the half of the ask/answer loop that actually reaches someone.

A board can RECORD a question and render it beautifully, and the asker is still blocked in
silence if the only delivery mechanism is somebody happening to have a browser tab open.
This module turns stored questions and directives into an ADDRESSED set per agent and lets
a caller BLOCK until that set changes — a worker's supervisor loop (or the operator's own
polling process) spends the sleep it was already doing held here instead, so an ask reaches
the operator in about a second and the answer lands back the same way.

Framework-free and pure over the folded state; the wait loop takes injected callables so
the adapter owns store lifecycles. Two rules hold the wait honest:

* It NEVER blocks a request thread longer than MAX_WAIT_S, and never more than MAX_WAITERS
  at once — past the ceiling it degrades to an immediate answer (an honest poll), rather
  than starving the server of workers. `degraded` is reported so the caller can tell a
  real quiet period from a hub that declined to hold the connection.
* The FINGERPRINT of the addressed set, not the event cursor, decides whether to wake a
  caller. Unrelated board traffic advances the cursor constantly; waking every waiter for
  a task status change makes the channel expensive and teaches people to ignore it.

The representation is deliberately first-class: a question is a note tagged
``question``+``open`` carrying its asker in an ``asker`` field (stable across rewrites —
provenance.agent becomes whoever last touched the note, which is the answerer after an
answer, so deriving the asker from provenance mis-addresses every re-answered reply). An
answer is a directive with ``answers`` naming the question and ``targets`` naming the
asker. An ack is that agent's record that delivery landed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

MAX_WAIT_S = 25          # under common proxy read timeouts and the SSE lifetime
MAX_WAITERS = 4          # past this the wait degrades to a poll instead of starving the server
POLL_S = 0.35            # change-signal cadence while blocked

_WAITERS = threading.BoundedSemaphore(MAX_WAITERS)

# A note carrying any of these was written BY AUTOMATION, so it is telemetry and can never
# be an item addressed to a person. Relying on every future caller to pick the right verb
# is not a control — this is, and it holds for automation written by somebody who never
# read the lesson that created it.
AUTOMATION_TAGS = {"probe", "selfcheck", "automated", "healthcheck", "heartbeat", "canary"}


def _text(value, limit=2000) -> str:
    return str(value or "").replace("\x00", " ")[:limit]


def body_text(value, limit=8000) -> str:
    """A body carries the INSTRUCTION, so it gets room — and says so when clipped. A silent
    cap once dropped the second half of a multi-step procedure addressed to the one person
    who could act on it, and nothing in the output said anything was missing — which is
    worse than a refusal, because the reader acts on it."""
    s = str(value or "").replace("\x00", " ")
    if len(s) <= limit:
        return s
    return s[:limit] + ("\n… [clipped: %d of %d characters — read the rest on the board]"
                        % (limit, len(s)))


def _hop(value) -> int:
    try:
        return max(0, min(9, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _age_s(stamp):
    """Seconds since an ISO stamp, or None when it cannot be read (never a guess)."""
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(time.time() - when.timestamp()))


def question_items(state) -> list:
    """Open questions from a person, addressed to the operator, newest first."""
    out = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "note":
            continue
        tags = [str(t).lower() for t in (ent.get("tags") or [])]
        if "question" not in tags or "open" not in tags:
            continue
        if AUTOMATION_TAGS.intersection(tags):
            continue
        prov = ent.get("provenance") or {}
        asker = _text(ent.get("asker") or prov.get("agent") or "", 60)
        at = _text(prov.get("created_at") or prov.get("updated_at") or "", 40)
        out.append({
            "kind": "question",
            "id": eid,
            "from": asker or "a board member",
            "title": _text(ent.get("title"), 300),
            "body": body_text(ent.get("body_md")),
            "at": at,
            # Escalation depth (0 = a person or an attended session) and how long it has
            # waited: an unattended responder takes a hop-1 escalation only after a cooldown.
            "hop": _hop(ent.get("hop")),
            "age_s": _age_s(at),
        })
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


def directive_items(state, agent: str) -> list:
    """Active directives aimed at this agent — including the answer to its own question.
    An item this agent has ALREADY ACKED is closed and not addressed to it any more;
    without that, the asker's own machine keeps announcing a reply it acknowledged."""
    agent = (agent or "").strip().lower()
    acked = set()
    for ent in (state.get("entities") or {}).values():
        if isinstance(ent, dict) and ent.get("type") == "ack" \
                and str(ent.get("agent") or "").lower() == agent:
            acked.add(ent.get("directive"))
    out = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "directive":
            continue
        if ent.get("status") != "active" or eid in acked:
            continue
        targets = [str(t).lower() for t in (ent.get("targets") or [])]
        if agent not in targets and "all" not in targets:
            continue
        prov = ent.get("provenance") or {}
        answered = ent.get("answers") or ""
        out.append({
            "kind": "answer" if answered else "directive",
            "id": eid,
            "from": _text(prov.get("agent") or "the operator", 60),
            "title": _text(ent.get("title"), 300),
            "body": body_text(ent.get("body_md")),
            "at": _text(prov.get("created_at") or prov.get("updated_at") or "", 40),
            "answers": _text(answered, 120),
            "remediation_cmd": _text(ent.get("remediation_cmd"), 400),
        })
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


def items_for(state, agent: str, operator: str) -> list:
    """Everything currently addressed to `agent`. The operator additionally receives every
    open question — questions are addressed to whoever can answer them."""
    agent = (agent or "").strip().lower()
    items = directive_items(state, agent)
    if agent and agent == (operator or "").strip().lower():
        items = question_items(state) + items
    return items


def fingerprint(items) -> str:
    """Identity of the addressed SET, so unrelated board traffic never wakes a waiter."""
    seed = "\n".join("%s|%s" % (i.get("id"), i.get("kind")) for i in items)
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]


def snapshot(state, agent: str, operator: str) -> dict:
    items = items_for(state, agent, operator)
    return {"items": items, "fingerprint": fingerprint(items), "count": len(items)}


def wait(agent: str, known_fingerprint: str, timeout: float, *,
         snapshot_fn, signal_fn) -> dict:
    """Block until this agent's addressed set differs from `known_fingerprint`.

    `snapshot_fn(agent)` returns the current addressed snapshot (the EXPENSIVE fold);
    `signal_fn()` returns a cheap change fingerprint of everything the snapshot depends on
    (typically the ledger head seq) — the full projection is recomputed only when the
    signal advances, never per poll tick. A signal_fn that errors must return "" — which
    never equals a real signal, so a failed read degrades safely to always-fold rather
    than silently skipping a real change.

    Returns as soon as the set changes, at the timeout, or immediately when the waiter
    ceiling is reached; `waited_s` and `degraded` are reported either way."""
    timeout = max(0.0, min(float(timeout or 0), MAX_WAIT_S))
    started = time.monotonic()
    current = snapshot_fn(agent)
    if current["fingerprint"] != known_fingerprint or timeout <= 0:
        current["waited_s"] = 0.0
        current["degraded"] = False
        return current
    if not _WAITERS.acquire(blocking=False):
        current["waited_s"] = 0.0
        current["degraded"] = True      # honest: this was a poll, not a wait
        return current
    try:
        deadline = started + timeout
        last_signal = signal_fn()
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            signal = signal_fn()
            if signal and signal == last_signal:
                continue                # nothing this agent depends on moved — no fold
            last_signal = signal
            current = snapshot_fn(agent)   # only NOW pay for the full projection
            if current["fingerprint"] != known_fingerprint:
                break
    finally:
        _WAITERS.release()
    current["waited_s"] = round(time.monotonic() - started, 2)
    current["degraded"] = False
    return current


def render_line(item) -> str:
    """One-line human form, shared by notifications and CLI consumers."""
    if item.get("kind") == "question":
        return "%s asks: %s" % (item.get("from") or "someone", item.get("title") or "")
    if item.get("kind") == "answer":
        return "Answer from %s: %s" % (item.get("from") or "the operator", item.get("title") or "")
    return "Directive: %s" % (item.get("title") or "")


def as_json(payload) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
