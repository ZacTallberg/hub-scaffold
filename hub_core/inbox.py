"""Addressed delivery: the half of the ask/answer loop that actually reaches someone.

A board can RECORD a question and render it beautifully, and the asker is still blocked in
silence if the only delivery mechanism is somebody happening to have a browser tab open.
This module turns stored questions, messages and directives into an ADDRESSED set per agent
(and, when the caller names one, per console) and lets a caller BLOCK until that set changes —
a worker's supervisor loop (or the operator's own polling process) spends the sleep it was
already doing held here instead, so an ask reaches its answerer in about a second and the
answer lands back the same way.

Framework-free and pure over the folded state; the wait loop takes injected callables so the
adapter owns store lifecycles. The rules this module holds, each one paid for on an origin
system:

* THE WAIT IS BOUNDED, AND THE SLOT COMES FIRST. It never blocks a request thread longer than
  MAX_WAIT_S, never more than MAX_WAITERS at once, and a caller turned away at the ceiling is
  answered from the last projection built for it (when young enough) instead of paying for a
  fresh fold the hub has just decided it cannot afford. `degraded` says so.
* THE FINGERPRINT, not the event cursor, decides whether to wake a caller. Unrelated board
  traffic advances the cursor constantly; waking every waiter for a task status change makes
  the channel expensive and teaches people to ignore it.
* EVERY QUESTION CARRIES ITS AGE, LONGEST WAIT FIRST. An item without `waited_s` cannot be
  triaged, and a detector keyed on that field is silent forever without it. An unreadable
  stamp is None — never 0 — and sorts last, so "unknown" is never mistaken for "just arrived".
* ASKS NEVER GET STUCK. Addressed -> that agent's; unaddressed -> the operator's; unanswered
  past ASK_UNSTICK_S -> EVERY console's (except the asker's own). A human-only gate is never
  widened — handing it to every console just spends sessions proving nobody else can act.
* DETECTORS SEE EVERYTHING; ONLY DELIVERY NARROWS. question_items() is the whole queue;
  questions_for() is the per-reader filter. Filtering inside the producer silences every
  caller that needs the whole queue.
* A SESSION-ADDRESSED ITEM REACHES ITS CONSOLE, and mail for a console that has ended falls
  through to that agent's most recently active live console, naming the original session —
  a console that closed is not a reason for mail to reach nobody.
* A CORRECTED ANSWER IS A NEW DELIVERY. Answers carry `delivery_revision`; an ack of an older
  revision does not close the correction.

The representation is deliberately first-class: a question is a note tagged
``question``+``open`` carrying its asker in a stable ``asker`` field (provenance.agent becomes
whoever last touched the note, which is the answerer after an answer). A message is a note
tagged ``message``+``open`` with ``to``. An answer is a directive with ``answers`` naming the
question and ``targets`` naming the asker. An ack is that agent's record that delivery landed.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
import time


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.environ.get(name, default)), high))
    except (TypeError, ValueError):
        return default


MAX_WAIT_S = 25          # under common proxy read timeouts and the SSE lifetime
# PER PROCESS: what this cap protects is this process's own request threads.
MAX_WAITERS = _env_int("HUB_INBOX_WAITERS_MAX", 4, 1, 16)
POLL_S = 0.35            # change-signal cadence while blocked
# How old a remembered projection may be and still answer a wait. Some items age on the clock
# alone (an ask turns stuck, then unsticks), so nothing is served older than this.
SNAPSHOT_REUSE_S = 30.0
# Presence moves on every request; the inbox only derives session routing from it, so its
# stamp in the change signal is bucketed — a waiter wakes at most once per bucket for it.
PRESENCE_SIGNAL_S = 15
# An ask nobody has answered in this long stops being one agent's backlog and reaches every
# console — and therefore every unattended responder that takes work from its inbox.
ASK_UNSTICK_S = _env_int("HUB_ASK_UNSTICK_S", 4 * 3600, 60, 30 * 86400)
# An open ask waiting this long is STUCK: the board and the attention rail say so, with its age.
ASK_STUCK_S = _env_int("HUB_ASK_STUCK_S", 2 * 3600, 60, 30 * 86400)
# A delivery older than this says when it was WRITTEN, so a reader does not act on stale mail
# as though it just arrived.
WRITTEN_NOTICE_S = 600
INBOX_BODY_LIMIT = 8000

_WAITERS = threading.BoundedSemaphore(MAX_WAITERS)
_LAST_SNAPSHOT: dict = {}          # key -> (signal, snapshot, monotonic at)
_LAST_SNAPSHOT_LOCK = threading.Lock()
_LAST_OFFERED: dict = {}           # (agent, session) -> fingerprint last offered

# A note carrying any of these was written BY AUTOMATION, so it is telemetry and can never
# be an item addressed to a person. Relying on every future caller to pick the right verb
# is not a control — this is, and it holds for automation written by somebody who never
# read the lesson that created it.
AUTOMATION_TAGS = {"probe", "selfcheck", "automated", "healthcheck", "heartbeat", "canary"}
# An ask only a PERSON can satisfy (an approval, a physical action). Filed with this tag, or
# matched by the adopter's gate pattern, it is delivered as a gate: never widened to every
# console, never handed to a responder that would retire it before a human saw it.
HUMAN_ONLY_TAG = "human-only"
# A SYNTHETIC ask is a self-test FOR MACHINES (the responder's daily canary): the loop it
# proves exists precisely so no person has to attend it. It is kept OUT of every delivery by
# default -- a person's inbox, the notifier's long-poll, the fingerprint that wakes them --
# and handed only to a reader that opts in (?include=synthetic: the responder). It is NOT an
# automation tag: those are dropped for every reader, and a canary the responder cannot see
# is a canary that can only ever time out -- or, worse, read as "retired" and pass.
SYNTHETIC_TAG = "synthetic"


def _text(value, limit=2000) -> str:
    return str(value or "").replace("\x00", " ")[:limit]


def body_text(value, limit=INBOX_BODY_LIMIT) -> str:
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


def waited_since(stamp, now=None):
    """Seconds since an ISO-8601 board timestamp, or None when it cannot be read — never 0,
    so "we do not know how long" can never be mistaken for "it just arrived". A stamp in the
    future (clock skew) is also None: an age the reader cannot trust is not an age."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    age = (time.time() if now is None else now) - parsed.timestamp()
    return age if age >= -5 else None


def age_phrase(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, seconds)
    if seconds < 3600:
        return "%d min" % max(1, int(seconds // 60))
    if seconds < 172800:
        return "%d h" % int(seconds // 3600)
    return "%d days" % int(seconds // 86400)


def _written(at, now=None) -> dict:
    """{"written": "written 42 min ago"} for a delivery older than WRITTEN_NOTICE_S; nothing
    when the stamp is missing, unparseable, or in the future — a header that guesses is worse
    than none."""
    age = waited_since(at, now)
    if age is None or age < WRITTEN_NOTICE_S:
        return {}
    return {"written": "written %s ago" % age_phrase(age)}


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _sid(value) -> str:
    """Session ids compare on their first 8 characters — the width presence keeps."""
    return str(value or "").strip()[:8]


def _prov_at(ent) -> str:
    prov = ent.get("provenance") or {}
    return _text(prov.get("created_at") or prov.get("updated_at") or "", 40)


def sid_match(a, b) -> bool:
    """Do two session ids name the same console? FULL ids compare exactly; an 8-character
    prefix is accepted only when one side is all the other sent (an older row, a lease that
    recorded eight) — prefixes collide across a board's worth of consoles."""
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        return False
    if len(a) > 8 and len(b) > 8:
        return a == b
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 8 and long_.startswith(short)


# ── questions ────────────────────────────────────────────────────────────────────────────────

_REPLACES = re.compile(r"(?i)\b(?:replaces|supersedes|instead of)\b[^\n]{0,120}?"
                       r"(?<![\w-])((?:[a-z0-9_-]+:note:)?q-[a-z0-9._-]+-[0-9a-f]{8})\b")


def superseded_asks(state) -> dict:
    """{superseded question id: the OPEN ask that replaces it}.

    An ask that says it replaces another ("... (replaces the old name in q-alice-1a2b3c4d)")
    used to leave BOTH on the answerer's list, so the person at the keyboard was asked to do
    the superseded thing too. Read-time and reversible: the older note is never rewritten, only
    kept off every delivered list while the replacing ask is open; it comes back the moment
    that ask is answered or withdrawn."""
    out = {}
    ents = state.get("entities") or {}
    for eid, ent in ents.items():
        if not isinstance(ent, dict) or ent.get("type") != "note":
            continue
        tags = [_norm(t) for t in (ent.get("tags") or [])]
        if "question" not in tags or "open" not in tags:
            continue
        text = "%s\n%s" % (ent.get("title") or "", ent.get("body_md") or "")
        for m in _REPLACES.finditer(text):
            ref = m.group(1)
            target = ref if ":note:" in ref else next(
                (k for k in ents if str(k).endswith(":note:" + ref)), "")
            if target and target != eid and target in ents:
                out[target] = eid
    return out



def question_items(state, *, human_gate=None, gate_satisfied=None, now=None) -> list:
    """EVERY open question from a person — the whole queue, unfiltered, LONGEST WAIT FIRST.

    Each row carries `to` (its addressee; empty = the operator's), `waited_s`/`age`, and
    `unstuck` once it has waited past ASK_UNSTICK_S — which is what delivery narrows on.

    `human_gate(text) -> bool` is the adopter's classifier for asks only a person can satisfy
    (read over title AND body: an ask naming the approval only in its context is still one).
    `gate_satisfied(text) -> str` names the evidence that such an approval has ALREADY landed;
    it must read a cache only — this runs inside every inbox fold and every held wait, and a
    read path that shells out or queries a remote system takes the whole hub down with it.
    When it answers, the ask stops being a gate and becomes an ordinary question anybody can
    close, carrying the evidence so whoever picks it up closes it instead of re-deriving it."""
    now = time.time() if now is None else now
    out = []
    superseded = superseded_asks(state)
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "note":
            continue
        tags = [_norm(t) for t in (ent.get("tags") or [])]
        if "question" not in tags or "open" not in tags:
            continue
        if AUTOMATION_TAGS.intersection(tags):
            continue
        if eid in superseded:
            continue                      # a later open ask says it replaces this one
        prov = ent.get("provenance") or {}
        asker = _text(ent.get("asker") or ent.get("from_agent") or prov.get("agent") or "", 60)
        title = _text(ent.get("title"), 300)
        body = body_text(ent.get("body_md"))
        blob = "%s\n%s" % (title, str(ent.get("body_md") or ""))
        human_only = HUMAN_ONLY_TAG in tags
        if not human_only and human_gate is not None:
            try:
                human_only = bool(human_gate(blob))
            except Exception:                                # noqa: BLE001 - never break delivery
                human_only = False
        granted = ""
        if human_only and gate_satisfied is not None:
            try:
                granted = str(gate_satisfied(blob) or "")
            except Exception:                                # noqa: BLE001
                granted = ""
            if granted:
                human_only = False
        at = _prov_at(ent)
        waited = waited_since(at, now)
        unstuck = waited is not None and waited >= ASK_UNSTICK_S and not human_only
        tier = _norm(ent.get("tier"))
        out.append({
            "kind": "gate" if human_only else "question",
            "id": eid,
            "from": asker or "a board member",
            "from_session": _text(ent.get("from_session"), 64),
            "to": _norm(ent.get("to"))[:60],
            "title": ("[%s] " % tier if tier and tier != "member" else "") + title,
            "body": body,
            "body_complete": len(str(ent.get("body_md") or "")) <= INBOX_BODY_LIMIT,
            "at": at,
            "waited_s": int(waited) if waited is not None else None,
            "age": age_phrase(waited),
            "stuck": waited is not None and waited >= ASK_STUCK_S,
            # A HUMAN GATE (`review`): delivered like any question, never taken by an
            # unattended session.
            "review": "review" in [str(t).lower() for t in (ent.get("tags") or [])],
            # Escalation depth (0 = a person or an attended session) and how long it has
            # waited: an unattended responder takes a hop-1 escalation only after a cooldown.
            "hop": _hop(ent.get("hop")),
            "age_s": _age_s(at),
            # Answers the asker's OWN unattended pass offered to a question meant for a person:
            # kept for that person to confirm, while the question stays open.
            **({"proposed_answers": [dict(x) for x in ent["proposed_answers"][-3:]
                                     if isinstance(x, dict)]}
               if isinstance(ent.get("proposed_answers"), list) and ent["proposed_answers"] else {}),
            **({"unstuck": True} if unstuck else {}),
            **({"human_only": True} if human_only else {}),
            **({"synthetic": True} if SYNTHETIC_TAG in tags else {}),
            **({"granted": granted,
                "granted_note": "the approval this ask wanted has landed (%s); close it with "
                                "`answer`" % granted} if granted else {}),
            **_written(at, now),
        })
    # Longest wait first; an unreadable stamp sorts LAST — promoting an unknown over a
    # measured two-day wait would bury the very row this ordering exists to raise.
    out.sort(key=lambda i: (i.get("waited_s") is not None, i.get("waited_s") or 0),
             reverse=True)
    return out


def questions_for(questions, agent: str, operator: str) -> list:
    """The DELIVERY filter over question_items() — who is shown which question:

        addressed        -> that agent's, and nobody else's
        unaddressed      -> the operator's
        waited too long  -> EVERYBODY'S (never back to its own asker; never a human gate)
    """
    me = _norm(agent)
    is_operator = bool(me) and me == _norm(operator)
    out = []
    for item in questions:
        if item.get("unstuck") and _norm(item.get("from")) != me:
            out.append(item)
            continue
        addressed = item.get("to") or ""
        if addressed:
            if addressed == me:
                out.append(item)
        elif is_operator:
            out.append(item)
    return out


def stuck_summary(questions) -> dict:
    """{stuck, stuck_after_seconds, stuck_ids, oldest_s} over an unfiltered question list."""
    stuck = [q for q in questions if q.get("stuck")]
    return {"stuck": len(stuck), "stuck_after_seconds": ASK_STUCK_S,
            "stuck_ids": [q["id"] for q in stuck][:20],
            "oldest_s": max((q.get("waited_s") or 0 for q in stuck), default=0)}


# ── session routing ─────────────────────────────────────────────────────────────────────────

def _route(item, agent: str, session: str, live) -> dict | None:
    """Session routing for one item addressed to `agent`. Returns the item (possibly annotated)
    or None when it belongs to a different live console.

    * No `session` on the item, or no session named by the reader: delivered (an agent-wide
      view — the CLI, the board, an older client — sees all of its agent's mail).
    * The item's console is the reader: delivered.
    * The item's console is LIVE elsewhere: not this reader's.
    * The item's console has ENDED: it falls through to the agent's most recently active live
      console, with `rerouted_from` naming the original — mail for a closed window must reach
      the box, not nobody."""
    target = str(item.get("session") or "").strip()
    me = str(session or "").strip()
    if not target or not me or sid_match(target, me):
        return item
    mine = [row for row in (live or []) if _norm(row.get("agent")) == _norm(agent)]
    if any(sid_match(row.get("session_id") or row.get("session"), target) for row in mine):
        return None
    # The mail's own MACHINE first: a message for a console is a message for that computer,
    # so when the console ends, another console on the same machine is the natural reader;
    # only with none live there does it fall to the agent's freshest console anywhere.
    pinned = _norm(item.get("machine"))
    pool = [row for row in mine if pinned and _norm(row.get("machine")) == pinned] or mine
    freshest = min(pool, key=lambda row: row.get("age_s") if row.get("age_s") is not None
                   else 10 ** 9, default=None)
    if freshest is not None and sid_match(freshest.get("session_id") or freshest.get("session"), me):
        return dict(item, rerouted_from=target[:8],
                    reroute_note="addressed to console %s, which is no longer live" % target[:8])
    return None


# ── messages and directives ─────────────────────────────────────────────────────────────────

#: A message written before messages carried `expires_at` lives this long past its creation.
LEGACY_MESSAGE_TTL_S = 72 * 3600


def _instant(raw):
    try:
        at = _dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    try:
        return at.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def message_expiry(ent) -> tuple:
    """(expires_epoch, implied) for a message note.

    A stamped `expires_at` wins. A note without one expires at created_at + LEGACY_MESSAGE_TTL_S
    (`implied` True). A note with neither a readable expiry nor a readable creation time returns
    (None, True): it cannot be aged, and an unageable message is one the delivery path must NOT
    push into a console -- a console that reopens after days is otherwise handed every stale
    message at once, as if each were current."""
    at = _instant(ent.get("expires_at")) if ent.get("expires_at") else None
    if at is not None:
        return at, False
    prov = ent.get("provenance") or {}
    created = _instant(prov.get("created_at") or prov.get("updated_at") or "")
    if created is None:
        return None, True
    return created + LEGACY_MESSAGE_TTL_S, True


#: A message older than this, delivered now, says what became of what it is ABOUT: a stale
#: "resolve p-X" after p-X closed, or "hold your pushes" after the push landed, is how a late
#: payload does damage while looking actionable.
LATE_NOTICE_S = _env_int("HUB_LATE_MESSAGE_S", 6 * 3600, 600, 30 * 86400)
_LATE_PROBLEM = re.compile(r"\bp-[0-9a-f]{8,16}\b")
_LATE_TASK = re.compile(r"(?<![\w-])((?:[a-z0-9_-]+:)?task:[A-Za-z0-9._-]+)")
_LATE_SHA = re.compile(r"(?<![-\w:/.])[0-9a-f]{7,40}(?![-\w])")


def late_subject_state(text: str, state, problem_states=None) -> str:
    """What has become of what a LATE message is about — problems by id (``problem_states``:
    {id: state} from the adapter), tasks by id from the board. A commit cannot be checked from
    the delivery path, so it says so; with nothing checkable, it says to check. Never raises."""
    text = str(text or "")
    bits = []
    try:
        if problem_states is not None:
            for pid in sorted(set(_LATE_PROBLEM.findall(text)))[:3]:
                st = problem_states.get(pid)
                bits.append("%s is %s" % (pid, ("now " + st) if st else "no longer on the board"))
        ents = (state or {}).get("entities") or {}
        for ref in sorted(set(_LATE_TASK.findall(text)))[:3]:
            local = ref.rsplit(":", 1)[-1]
            task = ents.get(ref) if ref.count(":") >= 2 else next(
                (e for k, e in ents.items() if str(k).endswith(":task:" + local)), None)
            if isinstance(task, dict):
                bits.append("task %s is now %s" % (local, task.get("status") or "unknown"))
        if not bits and _LATE_SHA.search(text.lower()):
            bits.append("it names a commit: check whether it has landed before acting")
    except Exception:                                        # noqa: BLE001 - a hint, never a failure
        pass
    return "; ".join(bits) if bits else "check current state before acting"


def message_items(state, agent: str, machine: str = "", now=None,
                  include_expired: bool = False, problem_states=None) -> list:
    """Open messages ADDRESSED to this agent (agent -> agent, no operator in the loop). A
    message pinned to a DIFFERENT machine of the same agent is not this machine's to deliver
    or retire; a reader that names no machine sees it.

    An EXPIRED message is never part of the delivered set (items_for, inbox/wait). It is kept --
    nothing is deleted -- and `include_expired` lists it, marked, for a reader who asks."""
    agent, machine = _norm(agent), _norm(machine)
    if not agent:
        return []
    now = time.time() if now is None else now
    out = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "note":
            continue
        tags = [_norm(t) for t in (ent.get("tags") or [])]
        if "message" not in tags or "open" not in tags:
            continue
        if _norm(ent.get("to")) != agent:
            continue
        pinned = _norm(ent.get("machine"))
        if pinned and machine and pinned != machine:
            continue
        exp, implied = message_expiry(ent)
        expired = exp is None or now >= exp
        if expired and not include_expired:
            continue
        prov = ent.get("provenance") or {}
        sender = _text(ent.get("from_agent") or prov.get("agent") or "", 60)
        sender_session = _text(ent.get("from_session"), 64)
        at = _prov_at(ent)
        structured = ent.get("structured") if isinstance(ent.get("structured"), dict) else {}
        waited = waited_since(at, now)
        late = waited is not None and waited >= LATE_NOTICE_S
        title = _text(ent.get("title"), 300)
        if late:
            title = "[late, written %s ago; %s] %s" % (
                age_phrase(waited), late_subject_state(
                    "%s\n%s" % (title, ent.get("body_md") or ""), state, problem_states), title)
        out.append({
            "kind": "message", "id": eid, "from": sender or "a board member",
            "from_session": sender_session, "to": agent,
            "title": title, **({"late": True} if late else {}),
            "body": body_text(ent.get("body_md")),
            "body_complete": len(str(ent.get("body_md") or "")) <= INBOX_BODY_LIMIT,
            "session": _text(ent.get("session"), 64), "machine": pinned, "at": at,
            "reply_cmd": "python -m hub_core.client msg %s%s --note \"<your reply>\"" % (
                sender or "<agent>", (" --session " + sender_session) if sender_session else ""),
            "ack_cmd": "python -m hub_core.client inbox --agent %s --ack %s"
                       % (agent, eid.rsplit(":", 1)[-1]),
            "expires_at": (_dt.datetime.fromtimestamp(exp, _dt.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ") if exp is not None else ""),
            "expiry_implied": implied,
            **({"expired": True} if expired else {}),
            **({"structured": {k: _text(v, 2000) for k, v in structured.items()}}
               if structured else {}),
            **_written(at, now),
        })
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


def expired_message_items(state, agent: str, machine: str = "", now=None) -> list:
    """The expired messages still addressed to `agent` -- LISTED on request, never delivered."""
    return [i for i in message_items(state, agent, machine, now, include_expired=True)
            if i.get("expired")]


def _pinned_elsewhere(ent, machine: str, session: str) -> bool:
    """A directive PINNED to one computer (`machine`) or one console (`session`) is delivered
    only there. A caller that does not say where it is cannot claim to be the pinned place —
    an unpinned client receiving a pinned instruction is exactly the misdelivery the pin
    exists to prevent."""
    want_machine = str(ent.get("machine") or "").strip().lower()
    want_session = str(ent.get("session") or "").strip()[:8]
    if want_machine and want_machine != (machine or "").strip().lower():
        return True
    if want_session and want_session != (session or "").strip()[:8]:
        return True
    return False


def ack_covers(directive, ack) -> bool:
    """One receipt covers one delivered answer revision, never a later correction."""
    if not directive or not ack or ack.get("directive") != directive.get("id"):
        return False
    revision = directive.get("delivery_revision")
    return revision is None or ack.get("delivery_revision") == revision


def directive_items(state, agent: str, now=None, *, machine: str = "", session: str = "") -> list:
    """Active directives aimed at this agent — including the answer to its own question. An
    item this agent has ALREADY ACKED (at its current revision) is closed and not addressed to
    it any more; without that, the asker's own machine keeps announcing a reply it acknowledged.

    A directive PINNED to a machine or console (``machine`` / ``session`` set by the writer) is
    delivered only there. An ANSWER's ``session`` is its asking console, not a pin: it is
    routed by the addressed set (``_route``), which lets mail for an ended console fall through."""
    agent = _norm(agent)
    now = time.time() if now is None else now
    entities = state.get("entities") or {}
    acks = {}
    for ent in entities.values():
        if isinstance(ent, dict) and ent.get("type") == "ack" and _norm(ent.get("agent")) == agent:
            acks[ent.get("directive")] = ent
    out = []
    for eid, ent in entities.items():
        if not isinstance(ent, dict) or ent.get("type") != "directive":
            continue
        if ent.get("status") != "active" or ack_covers(ent, acks.get(eid)):
            continue
        targets = [_norm(t) for t in (ent.get("targets") or [])]
        if agent not in targets and "all" not in targets:
            continue
        pin = ent if not ent.get("answers") else {"machine": ent.get("machine")}
        if _pinned_elsewhere(pin, machine, session):
            continue
        prov = ent.get("provenance") or {}
        answered = ent.get("answers") or ""
        at = _text(prov.get("updated_at") or prov.get("created_at") or "", 40)
        out.append({
            "kind": "answer" if answered else "directive",
            "id": eid,
            "from": _text(prov.get("agent") or "the operator", 60),
            "title": body_text(ent.get("title"), 4000),
            "body": body_text(ent.get("body_md")),
            "body_complete": len(str(ent.get("body_md") or "")) <= INBOX_BODY_LIMIT,
            "at": at,
            "answers": _text(answered, 120),
            "session": _text(ent.get("session"), 64),
            **({"delivery_revision": ent["delivery_revision"]}
               if "delivery_revision" in ent else {}),
            "remediation_cmd": _text(ent.get("remediation_cmd"), 400),
            **_written(at, now),
            "machine": _text(ent.get("machine"), 120),
        })
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


def _parked_on_reply(task) -> bool:
    """A decider REPLIED (a question back) and nothing has answered since: the decision leaves
    the decider's inbox while the filer answers, and returns once a newer checkpoint follows."""
    rows = [s for s in (task.get("plan") or []) if isinstance(s, dict) and s.get("done")
            and not s.get("lifecycle")]
    if not rows:
        return False
    last = max(rows, key=lambda s: str(s.get("note_at") or ""))
    return str(last.get("step") or "") == "Reply"


def assignment_items(state, agent: str) -> list:
    """Tasks GIVEN to this agent by name (POST /hub/api/hand) that it has not started yet.

    A board field nobody is pushed is an assignment its recipient finds by luck, so the same
    addressed set -- and its long-poll -- carries it. It leaves the inbox the moment the task
    moves on (claimed, finished, or handed to someone else); no ack is needed, the claim IS
    the acknowledgement."""
    agent = (agent or "").strip().lower()
    if not agent:
        return []
    out = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "task":
            continue
        if str(ent.get("assigned_to") or "").strip().lower() != agent:
            continue
        if ent.get("status") not in ("todo", "blocked"):
            continue
        prov = ent.get("provenance") or {}
        out.append({
            "kind": "assignment",
            "id": eid,
            "from": _text(prov.get("agent") or "the operator", 60),
            "title": _text(ent.get("title"), 300),
            "body": body_text(ent.get("acceptance")),
            "at": _text(prov.get("updated_at") or prov.get("created_at") or "", 40),
            "priority": _text(ent.get("priority"), 4),
        })
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


def decision_items(state) -> list:
    """Open decisions (work_kind ``decision``, no recorded decision), addressed to whoever
    decides, each with a deep link that opens it on the board. A decision never goes to an
    unattended worker; it is a person's call, so it is delivered to one."""
    out = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict) or ent.get("type") != "task":
            continue
        if str(ent.get("work_kind") or "").lower() != "decision":
            continue
        if ent.get("status") in ("done", "dropped") or isinstance(ent.get("decision"), dict):
            continue
        if _parked_on_reply(ent):
            continue
        prov = ent.get("provenance") or {}
        out.append({
            "kind": "decision", "id": "decision:%s:%s" % (eid, ent.get("version")),
            "task": eid, "from": _text(prov.get("agent") or "a board member", 60),
            "title": "Decision needed: %s" % _text(ent.get("title"), 280),
            "body": body_text(ent.get("acceptance") or ent.get("title")),
            "at": _text(prov.get("created_at") or prov.get("updated_at") or "", 40),
            "link": "/hub/#task-%s" % str(eid).rsplit(":", 1)[-1],
            "reply_cmd": ('python -m hub_core.client decide %s --then file|close|reply '
                          '--decision "..."' % eid)})
    out.sort(key=lambda item: item.get("at") or "", reverse=True)
    return out


# ── the addressed set ───────────────────────────────────────────────────────────────────────

def fold_gates(items: list) -> list:
    """Every human-gate chore the OPERATOR is handed, as ONE line with the count and the oldest
    wait (the ids ride `folded` and the body). Twenty near-identical "approve X on the host"
    rows are one chore for one person at one keyboard; as twenty rows they bury the decisions
    on the same list. One gate or none is returned unchanged."""
    gates = [i for i in items if i.get("kind") == "gate"]
    if len(gates) <= 1:
        return list(items)
    rest = [i for i in items if i.get("kind") != "gate"]
    waits = [g.get("waited_s") for g in gates if isinstance(g.get("waited_s"), (int, float))]
    oldest = max(waits) if waits else None
    lines = ["- %s%s" % (str(g.get("title") or g.get("id"))[:160],
                         " (%s)" % g["age"] if g.get("age") else "") for g in gates]
    folded = {"kind": "gate", "id": "gates:%s" % hashlib.sha256(
                  "|".join(sorted(str(g.get("id")) for g in gates)).encode()).hexdigest()[:12],
              "from": "the hub",
              "title": "%d chores wait on a person%s" % (
                  len(gates), (", the oldest for %s" % age_phrase(oldest)) if oldest is not None else ""),
              "body": "\n".join(lines), "at": "", "age": age_phrase(oldest),
              "waited_s": int(oldest) if oldest is not None else None,
              "folded": [str(g.get("id") or "") for g in gates],
              "reply_cmd": "python -m hub_core.client inbox --agent <you>"}
    return [folded] + rest


def items_for(state, agent: str, operator: str, operator_extra=None, *, machine: str = "",
              session: str = "", live=None, human_gate=None, gate_satisfied=None, visible=None,
              synthetic=False, now=None, problem_states=None) -> list:
    """Everything currently addressed to `agent` (and, when `session` is named, to that console).

    Order: a message from a teammate leads (somebody reached out to THIS agent directly), then
    human gates and questions (longest wait first), then directives and answers. `visible` is
    the caller's visibility filter (the contributor veil); it runs BEFORE fingerprinting, so a
    reader is never woken for — nor recorded as offered — an item it cannot see.

    ``operator_extra`` are computed items the adapter raises for a PERSON (a seat gone silent,
    persistent drift): conditions that cannot fix themselves, delivered to the operator only.

    ``synthetic`` admits self-test asks (SYNTHETIC_TAG). Off by default, so a person -- and the
    notifier that toasts them -- is never woken for a canary; the responder opts in."""
    agent = _norm(agent)
    questions = questions_for(question_items(state, human_gate=human_gate,
                                             gate_satisfied=gate_satisfied, now=now),
                              agent, operator)
    if not synthetic:
        questions = [q for q in questions if not q.get("synthetic")]
    if agent and agent == _norm(operator):
        questions = fold_gates(questions)
    items = (message_items(state, agent, machine, now, problem_states=problem_states) + questions
             + directive_items(state, agent, now, machine=machine, session=session)
             + assignment_items(state, agent))
    if agent and agent == _norm(operator):
        items += list(operator_extra or [])
    routed = []
    for item in items:
        kept = _route(item, agent, session, live)
        if kept is not None:
            routed.append(kept)
    if visible is not None:
        try:
            routed = [i for i in routed if visible(i)]
        except Exception:                                    # noqa: BLE001 - fail closed
            routed = []
    return routed


def fingerprint(items) -> str:
    """Identity of the addressed SET, so unrelated board traffic never wakes a waiter. A
    corrected answer (new delivery_revision) and a re-routed item are new deliveries."""
    seed = "\n".join("%s|%s|%s|%s|%s" % (i.get("id"), i.get("kind"), i.get("session", ""),
                                         i.get("delivery_revision", ""), i.get("unstuck", ""))
                     for i in items)
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]


def snapshot(state, agent: str, operator: str, operator_extra=None, *, hub_dir=None,
             **kwargs) -> dict:
    """The addressed set + its fingerprint. With `hub_dir`, an OFFER receipt is written for
    each item — only when the addressed set CHANGES: this is the wait loop's hot path and a
    console polling every few seconds must not be able to flood the record of what it was
    told. Fail-soft; receipts never break delivery."""
    items = items_for(state, agent, operator, operator_extra, **kwargs)
    fp = fingerprint(items)
    if hub_dir is not None and items:
        key = (_norm(agent), _sid(kwargs.get("session")))
        if _LAST_OFFERED.get(key) != fp:
            _LAST_OFFERED[key] = fp
            if len(_LAST_OFFERED) > 2048:
                _LAST_OFFERED.clear()
            try:
                from . import receipts
                for item in items:
                    receipts.record(hub_dir, str(item.get("kind") or "item"),
                                    str(item.get("id") or ""), "offered", agent=_norm(agent),
                                    detail=str(item.get("title") or "")[:200])
            except Exception:                                # noqa: BLE001
                pass
    return {"items": items, "fingerprint": fp, "count": len(items)}


def change_signal(seq, presence_stamp=None, *extra) -> str:
    """The cheap change fingerprint a wait compares between folds: the ledger head plus the
    presence stamp BUCKETED to PRESENCE_SIGNAL_S (every authenticated request rewrites a
    presence row; a raw stamp would re-fold every waiter several times a second while the
    fleet is busy), plus any sidecar stamps the adapter adds."""
    bucket = ""
    if presence_stamp:
        try:
            count, newest_ns = presence_stamp
            bucket = "%d:%d" % (count, int(newest_ns // (PRESENCE_SIGNAL_S * 1_000_000_000)))
        except (TypeError, ValueError):
            bucket = ""
    return "|".join([str(seq), bucket] + [repr(e) for e in extra])


def _remember(key, signal, snap) -> None:
    with _LAST_SNAPSHOT_LOCK:
        if len(_LAST_SNAPSHOT) >= 512:
            _LAST_SNAPSHOT.clear()
        _LAST_SNAPSHOT[key] = (signal, snap, time.monotonic())


def _remembered(key, signal=None):
    """The last projection built for this caller, if young enough and — when a signal is
    given — built at that same signal. A copy, so a caller cannot edit the memory."""
    with _LAST_SNAPSHOT_LOCK:
        hit = _LAST_SNAPSHOT.get(key)
    if not hit or time.monotonic() - hit[2] > SNAPSHOT_REUSE_S:
        return None
    if signal is not None and (not signal or hit[0] != signal):
        return None
    return dict(hit[1])


def wait(agent: str, known_fingerprint: str, timeout: float, *,
         snapshot_fn, signal_fn, key=None) -> dict:
    """Block until this caller's addressed set differs from `known_fingerprint`.

    `snapshot_fn(agent)` returns the current addressed snapshot (the EXPENSIVE fold);
    `signal_fn()` returns a cheap change fingerprint of everything the snapshot depends on. A
    signal_fn that errors must return "" — which never equals a real signal, so a failed read
    degrades safely to always-fold rather than silently skipping a real change. `key` names the
    caller for projection reuse (agent, machine, session, visibility tier).

    THE SLOT COMES FIRST: a caller turned away at the waiter ceiling gets its last projection
    (if younger than SNAPSHOT_REUSE_S) instead of a fresh fold; an admitted caller reuses its
    remembered projection when nothing it depends on has moved. Returns as soon as the set
    changes, at the timeout, or immediately when degraded; `waited_s` and `degraded` are
    reported either way."""
    timeout = max(0.0, min(float(timeout or 0), MAX_WAIT_S))
    started = time.monotonic()
    key = key or (agent,)
    if not _WAITERS.acquire(blocking=False):
        current = _remembered(key)
        if current is None:
            current = snapshot_fn(agent)
            _remember(key, "", current)
        current["waited_s"] = 0.0
        current["degraded"] = True      # honest: this was a poll, not a wait
        return current
    try:
        last_signal = signal_fn()
        current = _remembered(key, last_signal)
        if current is None:
            current = snapshot_fn(agent)
            _remember(key, last_signal, current)
        if current["fingerprint"] != known_fingerprint or timeout <= 0:
            current["waited_s"] = 0.0
            current["degraded"] = False
            return current
        deadline = started + timeout
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            signal = signal_fn()
            if signal and signal == last_signal:
                continue                # nothing this caller depends on moved — no fold
            last_signal = signal
            current = snapshot_fn(agent)   # only NOW pay for the full projection
            _remember(key, signal, current)
            if current["fingerprint"] != known_fingerprint:
                break
    finally:
        _WAITERS.release()
    current["waited_s"] = round(time.monotonic() - started, 2)
    current["degraded"] = False
    return current


def gate_pattern_classifier(pattern: str):
    """A human_gate callable from a regex (the HUB_HUMAN_GATE_PATTERN setting), or None."""
    if not pattern:
        return None
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        return None
    return lambda text: bool(rx.search(str(text or "")))


_REPLACES = re.compile(r"\((?:replaces|supersedes|instead of)[^)]*\)", re.I)


def all_requested_present(text, name_pattern, present) -> str:
    """A HUB_GATE_RESOLVER building block: the evidence that EVERY item an approval ask
    requests now exists, or "" (still a gate).

    The shape that goes wrong: a resolver that returns the FIRST existing name anywhere in the
    ask. An ask for a NEW worker whose context explains it runs beside an EXISTING service was
    labelled granted on the existing name and broadcast to every console as closable while the
    requested one did not exist. So:

      * the requested names are the ones in the ask's FIRST line (the question); the rest of
        the text is explanation. Only when the first line names none is the whole text read;
      * a name the ask REPLACES ("(replaces the old-name ...)") is not requested;
      * every requested name must be positively present — one unseen name keeps it a gate
        (fail closed); no requested name at all is never a grant.

    `name_pattern` is a regex (or compiled pattern) matching one item name; `present(name)`
    answers from a CACHE only — this runs inside every inbox fold. A presence cache should
    remember a name that has EVER been seen present, so a grant does not flip back to a gate
    whenever the cache's freshness window lapses."""
    rx = re.compile(name_pattern) if isinstance(name_pattern, str) else name_pattern
    body = str(text or "")
    head = _REPLACES.sub("", body.split(chr(10), 1)[0])
    wanted = sorted(set(rx.findall(head))) or sorted(set(rx.findall(_REPLACES.sub("", body))))
    wanted = [w if isinstance(w, str) else w[0] for w in wanted]
    if not wanted:
        return ""
    for name in wanted:
        try:
            if not present(name):
                return ""
        except Exception:                                    # noqa: BLE001 - doubt keeps the gate
            return ""
    return ", ".join(wanted)


def render_line(item) -> str:
    """One-line human form, shared by notifications and CLI consumers."""
    kind = item.get("kind")
    if kind == "gate":
        if item.get("folded"):
            return str(item.get("title") or "")
        return "Needs a person (%s): %s" % (item.get("age") or "new", item.get("title") or "")
    if kind == "question":
        return "%s asks%s: %s" % (item.get("from") or "someone",
                                  (" (waiting %s)" % item["age"]) if item.get("age") else "",
                                  item.get("title") or "")
    if kind == "answer":
        return "Answer from %s: %s" % (item.get("from") or "the operator", item.get("title") or "")
    if kind == "message":
        return "Message from %s: %s" % (item.get("from") or "a board member", item.get("title") or "")
    if item.get("kind") in ("decision", "task-stall", "attention", "overlap", "error"):
        return str(item.get("title") or "")
    if item.get("kind") == "assignment":
        return "%s gave you %s: %s" % (item.get("from") or "the operator", item.get("id") or "",
                                       item.get("title") or "")
    if item.get("kind") == "offline":
        return "OFFLINE %s" % (item.get("title") or "")
    if item.get("kind") == "drift":
        return "DRIFT %s" % (item.get("title") or "")
    return "Directive: %s" % (item.get("title") or "")


def as_json(payload) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
