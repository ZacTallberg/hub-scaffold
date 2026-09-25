"""NEEDS ATTENTION — every fixable condition about the board's workers, their seats, their
tasks and their credentials, as ONE server-side list: who acts, the exact fix, how long it has
stood, and the values that produced it.

A condition that lives only in a browser-side computation is recorded nowhere and delivered to
nobody: the same line sits on a card forever while the fixable state it hints at goes unread.
This module is the list the card, the API (``/hub/attention.json``), the client
(``attention``) and the inbox all read.

The contract every item keeps:

* It is COMPUTED from current state, so it clears itself the moment the condition does. It is
  never acked away; when the fact goes it moves to ``recently_cleared`` with how long it stood.
* It names WHO can act and the exact FIX, and carries the EVIDENCE (values) that produced it.
* Its age is real: first-seen is persisted in a sidecar (``attention-seen.json``).
* It REACHES a person through the inbox: critical at once, warn after OWNER_PATIENCE_S to its
  owner, and to the operator once it has stood OPERATOR_PATIENCE_S. Info lives on the card only.
  A condition another lane already delivers (a question, an error, a decision) is SHOWN here and
  not delivered twice (``lanes``).
* It names WHO CAN CLEAR IT (``actor``): ``agent`` or ``person``. Rarely does a condition truly
  need a person: an agent item reaches its owner's inbox at ANY severity once it has stood
  OWNER_PATIENCE_S (critical at once), and an unattended responder may take it
  (hub_core.responder), under its attempt cap. Only the short list in PERSON_ONLY -- the things
  an agent structurally cannot do -- waits for a human. On the instance this was lifted from,
  over half of one operator's attention list could have been cleared by an agent and none ever
  was, because the list launched nothing.
* A source that could not be read never breaks the list: it is named in ``sources`` and the
  verdict, and the detectors that needed it stay SILENT rather than guess — an alarm that lies
  is worse than none, and a verdict that drops what it could not measure always passes.
* A silent seat is reported as silent, never as "retired" — a machine that read as abandoned
  routinely comes back the same evening.

Pure over a ``ctx`` the adapter gathers; the only I/O is the first-seen sidecar under hub_dir.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import task_health

OWNER_PATIENCE_S = 30 * 60
OPERATOR_PATIENCE_S = 24 * 3600
SILENT_INFO_S = 3600
SILENT_WARN_S = 6 * 3600
#: A silent seat reaches its OWNER after this long (a warn), holding a lease or not: an evening
#: or a weekend day is not an alarm, a working week passing unnoticed is.
OWNER_SILENT_S = 24 * 3600
#: Past this a seat is DORMANT (hub_core.distribution.DORMANT_AFTER_S): ONE stable
#: "confirm it is retired, or turn it on" item, never a fresh line per sweep.
DORMANT_AFTER_S = 72 * 3600
#: ...and an archived (retired) row keeps that item this long, then it is history.
DORMANT_KEEP_S = 30 * 86400
#: The unattended launcher's run cap (hub_core.unattended.board.TASK_MAX_RUNS): a task handed
#: back this many times is never offered to a responder again -- it is a person's from here.
TASK_CAP_RUNS = 3
CREDENTIAL_WARN_S = 14 * 86400
CREDENTIAL_CRITICAL_S = 3 * 86400
QUESTION_WAIT_S = 2 * 3600
CLEARED_KEEP = 30

SEVERITIES = ("critical", "warn", "info")
AREAS = ("seats", "tasks", "communication", "credentials", "errors", "hub")

#: What ONLY a person can do; everything else is an agent's (a task to verify and close, a lease
#: to reclaim or hand back, a stale client to update). Each entry names the one thing an agent
#: structurally cannot do.
PERSON_ONLY = {
    "seat_silent": "the machine is off or off the network; nothing runs there to take the work",
    "untracked_consoles": "an attended console's claim is its person's to make",
    "credential_expiring": "a credential is rotated or re-issued at its source system",
    "question_waiting": "an ask is answered through the ask lane, not here",
    "errors_unclaimed": "the problems lane already offers every unclaimed problem",
}
#: Agent items that only the machine they name can clear (an install, a client update).
ON_ITS_MACHINE = {"client_stale"}


def actor_for(kind: str) -> str:
    """``person`` for PERSON_ONLY kinds, else ``agent``."""
    return "person" if kind in PERSON_ONLY else "agent"


def item(kind, area, severity, *, title, fix, who, detail="", agent="", machine="", subject="",
         evidence=None, lanes=(), deliver_owner=True) -> dict:
    """One attention item. ``subject`` disambiguates two items of one kind; ``lanes`` names a
    lane that already delivers this to the operator; ``deliver_owner=False`` means another lane
    already delivers it to the owner (the card still shows it, so the picture is on one surface)."""
    assert severity in SEVERITIES and area in AREAS, (severity, area)
    ident = ":".join(str(x) for x in (kind, subject or machine or agent or "board") if x)
    return {"id": ident, "kind": kind, "area": area, "severity": severity,
            "agent": str(agent or "").lower(), "machine": str(machine or ""), "who": who or "",
            "title": title, "detail": detail, "fix": fix, "evidence": evidence or {},
            "lanes": list(lanes), "deliver_owner": deliver_owner,
            "actor": actor_for(kind), "on_its_machine": kind in ON_ITS_MACHINE}


def age_phrase(seconds) -> str:
    s = int(seconds or 0)
    if s < 90:
        return "%d s" % s
    if s < 3600:
        return "%d min" % (s // 60)
    if s < 2 * 86400:
        return "%.0f h" % (s / 3600.0)
    return "%.0f d" % (s / 86400.0)


def _plural(n, one, many=None) -> str:
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


# --------------------------------------------------------------------------- detectors
# Each takes the gathered ctx and returns items. ctx keys (all optional; a missing source is
# declared in ctx["sources"] and its detectors are skipped):
#   now, operator, tasks (task rows), activity ({task_id: epoch}), leases (live lease rows),
#   runs (run rows), sessions (live console rows with task_id), presence (per-agent rows with
#   machines), hub_client (the client version the hub serves), credentials (public credential
#   records), errors_unclaimed (bar-clearing unacked error rows), questions (open question items),
#   answers (answer directives still active: {id, asker, at, revision})

def detect_tasks(ctx) -> list:
    operator = ctx.get("operator") or ""
    summary = task_health.summarize(ctx.get("tasks") or [], ctx.get("activity") or {}, ctx["now"])
    table = (("complete_unclosed", "every checkpoint is recorded and it is still in progress",
              'python -m hub_core.client finish %s --accept-note "<outcome>" --evidence <sha|url>'),
             ("stalled", "it has not moved",
              'Step it with what happened (python -m hub_core.client step %s --note "..."), or '
              "hand it back to the queue"),
             ("orphaned", "it is in progress with no checkpoints and has not moved",
              "Claim it (python -m hub_core.client start %s) or hand it back to the queue"))
    out = []
    for bucket, why, fix in table:
        for t in summary.get(bucket) or []:
            tid = str(t.get("id") or "")
            owner = str(t.get("owner") or "").lower()
            age = int(t.get("age_s") or 0)
            severity = ("warn" if bucket == "complete_unclosed" or age >= task_health.STALE_AFTER_S
                        else "info")
            out.append(item("task_" + bucket, "tasks", severity, agent=owner or operator,
                            who=owner or "the operator", subject=tid,
                            title="%s: %s — %s" % (tid, str(t.get("title") or "")[:90], why),
                            detail=str(t.get("reason") or ""), fix=fix % tid,
                            evidence={"task": tid, "age_s": age, "done": t.get("done"),
                                      "total": t.get("total"),
                                      "closeable": t.get("closeable"),
                                      "says_unfinished": t.get("says_unfinished") or ""},
                            lanes=("task-health",)))
    return out


def detect_unstarted(ctx) -> list:
    operator = ctx.get("operator") or ""
    out = []
    for req in task_health.unstarted_requests(ctx.get("tasks") or [], ctx.get("runs") or [],
                                              ctx.get("leases") or [], ctx["now"]):
        out.append(item("unattended_" + req["state"].replace("-", "_"), "tasks", "warn",
                        agent=operator, who="the operator", subject=req["task"],
                        title=req["title"], detail=req["body"], fix=req["reply_cmd"],
                        evidence={k: req.get(k) for k in ("task", "priority", "done", "total",
                                                          "evidenced", "lifecycle_steps")},
                        lanes=("task-health",)))
    return out


def detect_leases(ctx) -> list:
    """A task held by a console that is PROVABLY gone. The board reads in flight, nobody is on
    it, and every automatic close refuses it (the hub never finishes a task another agent holds).

    ABSENT FROM THE CONSOLE LIST IS NOT GONE. An idle console can drop out of the live rows
    while its session is alive; on the instance this was lifted from, this fired on a task held
    by a console that was working the whole time, and -- once agent items reached responders --
    several responders spent their sessions confirming the holder was live. So it fires only on
    the liveness verdict the adapter attaches to each lease (``holder_state == "gone"``: the
    holder's machine is reporting and that console is not among its live ones). A lease with no
    verdict, or any other verdict, is left alone until it expires on its own."""
    sessions = ctx.get("sessions")
    if not sessions:
        return []
    tasks = {t.get("id"): t for t in ctx.get("tasks") or [] if isinstance(t, dict)}
    out = []
    for lease in ctx.get("leases") or []:
        sid = str(lease.get("session") or "")[:8]
        tid = str(lease.get("task") or "")
        if not sid or str(lease.get("holder_state") or "") != "gone":
            continue
        if str((tasks.get(tid) or {}).get("status") or "") != "in_progress":
            continue
        agent = str(lease.get("agent") or "").lower()
        held = ctx["now"] - float(lease.get("claimed") or ctx["now"])
        out.append(item(
            "lease_orphaned", "tasks", "warn", agent=agent, who=agent, subject=tid,
            title="%s is held by console %s, which is no longer live" % (tid, sid),
            detail="The board shows it in flight and every automatic close refuses it until the "
                   "lease is released or reclaimed.",
            fix="Reclaim it here and finish it (python -m hub_core.client start %s), put it back on "
                "the queue (python -m hub_core.client hand %s), or let go of it "
                "(python -m hub_core.client unclaim %s)." % (tid, tid, tid),
            evidence={"task": tid, "session": sid, "held_s": int(held) if held > 0 else None,
                      "expires": lease.get("expires")}))
    return out


def detect_untracked(ctx) -> list:
    """Attended consoles doing real work in a project with no task claimed. Card-only: a claim
    is the person's to make, and the board cannot see this work until they do."""
    groups = {}
    for r in ctx.get("sessions") or []:
        if r.get("unattended") or r.get("has_task") or not r.get("project"):
            continue
        groups.setdefault((str(r.get("agent") or "").lower(), str(r.get("machine") or "")), []).append(r)
    out = []
    for (agent, machine), rows in groups.items():
        projects = sorted({str(r.get("project")) for r in rows})
        out.append(item("untracked_consoles", "tasks", "info", agent=agent, machine=machine,
                        who=agent, subject="%s:%s" % (agent, machine),
                        title="%s on %s working with no task: %s"
                              % (_plural(len(rows), "console"), machine or agent, ", ".join(projects)),
                        detail="The board shows this work as untracked, so nobody else can see it, "
                               "join it, or avoid duplicating it.",
                        fix='In each console: python -m hub_core.client start <task-id>, or create '
                            'one (python -m hub_core.client create --title "<project>: <work>" '
                            '--acceptance "<done when>").',
                        evidence={"consoles": [{"session": str(r.get("session") or "")[:8],
                                                "project": r.get("project")} for r in rows][:12]},
                        deliver_owner=False))
    return out


def detect_seats(ctx) -> list:
    """A seat that has stopped reporting. Info while it could be a closed laptop; warn once it
    has been silent a working day WHILE it still holds a live lease (work nobody is doing), or
    for a day whatever it holds -- a warn reaches the seat's OWNER, the one person who can turn
    it on (an info line reached nobody who could act). Past DORMANT_AFTER_S it becomes ONE stable
    retire-or-return item instead: the seat is still enrolled and still counts, and a machine that
    stopped calling home must not become nobody's item exactly when it became permanent."""
    now = ctx["now"]
    leased = {str(l.get("agent") or "").lower() for l in ctx.get("leases") or []}
    out = []
    seats = [(agent, m) for agent, row in (ctx.get("presence") or {}).items()
             for m in row.get("machines") or []]
    # Rows presence retirement ARCHIVED (it never deletes) are still enrolled seats: within
    # DORMANT_KEEP_S they keep their one retire-or-return item, or the seat would become
    # nobody's item at the very horizon where it became permanent.
    listed = {(str(a).lower(), str(m.get("machine") or "").lower()) for a, m in seats}
    for r in ctx.get("retired") or []:
        key = (str(r.get("agent") or "").lower(), str(r.get("machine") or "").lower())
        if not key[0] or not key[1] or key in listed:
            continue
        stamp = max(float(r.get(k) or 0) for k in ("heartbeat_at", "activity_at", "last_seen"))
        if stamp and DORMANT_AFTER_S <= now - stamp <= DORMANT_KEEP_S:
            listed.add(key)
            seats.append((key[0], r))
    for agent, m in seats:
        stamps = [float(m.get(k) or 0) for k in ("heartbeat_at", "activity_at", "last_seen")]
        last = max(stamps) if stamps else 0
        if not last:
            continue
        silent = now - last
        if silent < SILENT_INFO_S:
            continue
        holds = agent.lower() in leased
        machine = str(m.get("machine") or "")
        if silent >= DORMANT_AFTER_S:
            days = silent / 86400.0
            out.append(item("seat_dormant", "seats", "warn", agent=agent, machine=machine,
                            who=agent, subject="%s:%s" % (agent, machine or "-"),
                            title="%s%s has been silent %.0f days: confirm it is retired, or "
                                  "turn it on" % (agent, ("@" + machine) if machine else "",
                                                  days),
                            detail="It is still enrolled -- its credential works and it counts "
                                   "as a seat -- but it has made no request in %.0f days, so "
                                   "nothing reaches it and the distribution view no longer "
                                   "grades it.%s" % (days, " It still holds a lease."
                                                     if holds else ""),
                            fix="If it is retired, drop its row (python -m hub_core.client "
                                "forget-presence --machine %s) and revoke its credential; if "
                                "not, turn it on -- it catches up by itself." % (machine or "<machine>"),
                            evidence={"silent_s": int(silent), "holds_lease": holds}))
            continue
        severity = ("warn" if (holds and silent >= SILENT_WARN_S) or silent >= OWNER_SILENT_S
                    else "info")
        out.append(item("seat_silent", "seats", severity, agent=agent, machine=machine,
                        who=agent, subject="%s:%s" % (agent, machine or "-"),
                        title="%s%s has not reported for %s%s"
                              % (agent, ("@" + machine) if machine else "", age_phrase(silent),
                                 " and still holds a lease" if holds else ""),
                        detail="Silent, not retired: it reappears the moment it checks in.",
                        fix="Check the seat is running; if it is gone for good, drop its row "
                            "(python -m hub_core.client forget-presence --machine %s)"
                            % (machine or "<machine>"),
                        evidence={"silent_s": int(silent), "holds_lease": holds}))
    return out


def detect_clients(ctx) -> list:
    """A seat reporting a different client version than the hub serves: it is running an older
    (or diverged) worker kit, so fixes to the loop have not reached it."""
    want = str(ctx.get("hub_client") or "")
    if not want:
        return []
    out = []
    for agent, row in (ctx.get("presence") or {}).items():
        for m in row.get("machines") or []:
            have = str(m.get("client_digest") or "")
            if not have or have == want:
                continue
            machine = str(m.get("machine") or "")
            out.append(item("client_stale", "seats", "info", agent=agent, machine=machine,
                            who=agent, subject="%s:%s" % (agent, machine or "-"),
                            title="%s%s runs client %s; the hub serves %s"
                                  % (agent, ("@" + machine) if machine else "", have[:12], want[:12]),
                            detail="Behaviour fixed in the worker loop since that client was "
                                   "installed has not reached this seat.",
                            fix="Update the seat's checkout of this repository (a fast-forward "
                                "pull), or set HUB_CLIENT_SELF_UPDATE=1 to let the client converge "
                                "itself.",
                            evidence={"client": have, "hub_client": want,
                                      "reported_at": m.get("client_at")}))
    return out


def _handed_back_runs(task) -> int:
    """The launcher's hand-backs (kind ``handed_back``, summing ``times``). The hub's own lease
    releases (``lease_released``) are deliberately not counted: an expired lease is not a run
    giving up."""
    return sum(int(s.get("times") or 1) for s in (task.get("plan") or [])
               if isinstance(s, dict) and s.get("kind") == "handed_back")


def detect_capped(ctx) -> list:
    """An unattended task no responder will take again, and that no person has been told about.

    The launcher stops offering a task at its run cap and the queue shows it as ordinary todo,
    so the only reader left is a person who happens to open it -- on the origin system a P0 with
    a verified-live fix sat at the cap for almost a week. Fires on the launcher's
    ``needs_person`` field, on the cap reached by hand-back count, or on a deploy close the hub
    was REFUSED (``auto_close.state == refused``). Goes to the task's owner (``assigned_to``,
    else whoever filed it, else the operator): critical at once for P0, a warn after the owner's
    patience otherwise, and to the operator through the ordinary escalation. A pushed sha is
    named, because "a fix is live and only a person can finish it" is the cheapest item there is."""
    from . import task_rows
    operator = ctx.get("operator") or ""
    known = {str(a).lower() for a in (ctx.get("presence") or {})}
    out = []
    for t in ctx.get("tasks") or []:
        caps = [str(c).lower() for c in (((t.get("routing") or {}).get("required_capabilities")
                                          or []) if isinstance(t, dict) else [])]
        if not isinstance(t, dict) or not (t.get("unattended") in (True, 1, "1", "true")
                                            or "unattended" in caps):
            continue
        if str(t.get("status") or "") not in ("todo", "in_progress", "blocked"):
            continue
        tid = str(t.get("id") or "")
        need = t.get("needs_person") if isinstance(t.get("needs_person"), dict) else None
        runs = _handed_back_runs(t)
        ac = t.get("auto_close") if isinstance(t.get("auto_close"), dict) else None
        refused = ac if ac and ac.get("state") == "refused" else None
        if not (need or runs >= TASK_CAP_RUNS or refused):
            continue
        prov = t.get("provenance") if isinstance(t.get("provenance"), dict) else {}
        owner = ""
        for cand in (t.get("assigned_to"), t.get("owner"), prov.get("created_by"),
                     prov.get("agent")):
            cand = str(cand or "").strip().lower()
            if cand and (not known or cand in known):
                owner = cand
                break
        owner = owner or operator
        pushed = task_rows.pushed(t) or {}
        sha = str((need or {}).get("sha") or (refused or {}).get("sha") or pushed.get("sha") or "")
        prio = str(t.get("priority") or "").upper()
        if need:
            why = "the launcher handed it to a person: %s" % str(need.get("reason") or
                                                                 "no reason given")[:200]
        elif refused:
            why = "the deploy record's close was refused: %s" % str(refused.get("why") or "")[:200]
        else:
            why = "it was handed back %d times, so no responder will take it again" % runs
        out.append(item(
            "task_needs_person", "tasks", "critical" if prio == "P0" else "warn",
            agent=owner, who=owner or "the operator", subject=tid,
            title="%s: %s -- %s" % (tid, str(t.get("title") or "")[:80],
                                    "a fix is pushed and only a person can finish it" if sha
                                    else "no responder will take it; a person must"),
            detail="Why: %s.%s Responders stop at the cap and the queue shows it as ordinary "
                   "todo, so nothing else will surface it."
                   % (why, (" The newest pushed commit is %s." % sha[:12]) if sha else ""),
            fix=("Read its checkpoints (python -m hub_core.client recall %s). If the work is done, "
                 'finish it (python -m hub_core.client finish %s --accept-note "<outcome>" '
                 "--evidence <sha|url>); if it needs a decision, make it on the task; if it should "
                 "run again, hand it to someone (python -m hub_core.client hand %s --to <agent>)."
                 % (tid, tid, tid)),
            evidence={"task": tid, "priority": prio, "handed_back": runs, "sha": sha[:40],
                      "needs_person": need, "auto_close": refused}))
    return out


def detect_credentials(ctx) -> list:
    now = ctx["now"]
    operator = ctx.get("operator") or ""
    out = []
    for c in ctx.get("credentials") or []:
        if c.get("revoked_at") or not c.get("expires_at"):
            continue
        try:
            expires = float(c["expires_at"])
        except (TypeError, ValueError):
            try:
                expires = datetime.fromisoformat(str(c["expires_at"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        left = expires - now
        if left > CREDENTIAL_WARN_S:
            continue
        subject = str(c.get("subject") or "")
        severity = ("info" if left <= 0 else
                    "critical" if left <= CREDENTIAL_CRITICAL_S else "warn")
        out.append(item("credential_expiring" if left > 0 else "credential_expired", "credentials",
                        severity, agent=operator, who="the operator",
                        subject=str(c.get("credential_id") or subject),
                        title=("credential for %s expires in %s" % (subject, age_phrase(left))
                               if left > 0 else "credential for %s expired %s ago and was never "
                               "revoked" % (subject, age_phrase(-left))),
                        detail="A worker whose credential lapses is refused on every write and "
                               "looks, from the board, like a seat that went quiet.",
                        fix=("Issue a replacement and hand it to the worker, then revoke the old "
                             "one (POST /hub/api/agent-credential {action: issue|revoke})"),
                        evidence={"credential_id": c.get("credential_id"), "subject": subject,
                                  "expires_at": c.get("expires_at")}))
    return out


def detect_errors(ctx) -> list:
    operator = ctx.get("operator") or ""
    rows = ctx.get("errors_unclaimed") or []
    if not rows:
        return []
    if any("state" in r for r in rows):
        # The queue is FOLDED into problems (hub_core.problems): one line per thing somebody
        # fixes, claimed and resolved by its problem id.
        oldest = max((int(r.get("age_s") or 0) for r in rows), default=0)
        return [item("errors_unclaimed", "errors", "warn", agent=operator, who="the operator",
                     subject="errors",
                     title="%s nobody has claimed (oldest %s)"
                           % (_plural(len(rows), "problem"), age_phrase(oldest)),
                     detail="; ".join(str(r.get("title") or r.get("message") or "")[:80]
                                      for r in rows[:3]),
                     fix="Claim each one before digging (python -m hub_core.client claim <p-id>), "
                         "then resolve it with the root cause and evidence.",
                     evidence={"problems": [r.get("id") for r in rows[:8]]},
                     lanes=("errors",), deliver_owner=False)]
    oldest = max((ctx["now"] - float(r.get("epoch") or ctx["now"]) for r in rows), default=0)
    return [item("errors_unclaimed", "errors", "warn", agent=operator, who="the operator",
                 subject="errors",
                 title="%s on the error stream nobody has claimed (oldest %s)"
                       % (_plural(len(rows), "failure"), age_phrase(oldest)),
                 detail="; ".join(str(r.get("message") or "")[:80] for r in rows[:3]),
                 fix="Claim each one (python -m hub_core.client ack-error <fingerprint>) and fix "
                     "it, or reopen what recurs.",
                 evidence={"fingerprints": [r.get("fingerprint") for r in rows[:8]]},
                 lanes=("errors",), deliver_owner=False)]


def detect_questions(ctx) -> list:
    operator = ctx.get("operator") or ""
    out = []
    for q in ctx.get("questions") or []:
        # A synthetic ask (the responder canary) is a self-test for machines: the canary judges
        # its own loop, so its wait is never a person's to-do.
        if q.get("synthetic"):
            continue
        waited = task_health.age_s(q.get("at"), ctx["now"])
        if waited is None or waited < QUESTION_WAIT_S:
            continue
        out.append(item("question_waiting", "communication", "warn", agent=operator,
                        who="the operator", subject=str(q.get("id") or ""),
                        title="%s has waited %s for an answer: %s"
                              % (q.get("from") or "a worker", age_phrase(waited),
                                 str(q.get("title") or "")[:100]),
                        detail="A worker blocked on one fact is a worker not finishing.",
                        fix='python -m hub_core.client answer %s --text "..."' % q.get("id"),
                        evidence={"question": q.get("id"), "waited_s": int(waited)},
                        lanes=("questions",), deliver_owner=False))
    return out


def detect_answers(ctx) -> list:
    """AN UNREAD ANSWER IS NOT A MISSED DELIVERY. An answer stays active until its asker ACKS
    it, so "delivered but not yet acknowledged" is an inbox chore for the asker, never a fault
    in the delivery path -- calling it one sends a person to repair a client that is working.
    One item per asker, oldest first, with the exact command that clears it."""
    by_asker: dict = {}
    for a in ctx.get("answers") or []:
        age = task_health.age_s(a.get("at"), ctx["now"])
        if age is None:
            continue
        by_asker.setdefault(str(a.get("asker") or "").lower(), []).append((a, age))
    out = []
    for asker, rows in by_asker.items():
        if not asker:
            continue
        rows.sort(key=lambda r: -r[1])
        oldest = rows[0][1]
        if oldest < SILENT_INFO_S:
            continue
        first = rows[0][0]
        rev = int(first.get("revision") or 0)
        out.append(item("answers_unread", "communication",
                        "warn" if oldest >= SILENT_WARN_S else "info", agent=asker, who=asker,
                        title="%s to %s's questions not yet read (oldest %s)"
                              % (_plural(len(rows), "answer"), asker, age_phrase(oldest)),
                        detail="An answer leaves this list when its asker acknowledges it; it "
                               "is not a delivery fault.",
                        fix="python -m hub_core.client inbox --agent %s, read it, then "
                            "python -m hub_core.client ack %s%s"
                            % (asker, first.get("id"), (" --revision %d" % rev) if rev else ""),
                        evidence={"answers": [r[0].get("id") for r in rows][:10],
                                  "oldest_s": int(oldest)}))
    return out


DETECTORS = (
    ("tasks", detect_tasks, ("tasks",)),
    ("capped", detect_capped, ("tasks",)),
    ("unstarted", detect_unstarted, ("tasks", "leases", "runs")),
    ("leases", detect_leases, ("tasks", "leases", "sessions")),
    ("untracked", detect_untracked, ("sessions",)),
    ("seats", detect_seats, ("presence", "leases")),
    ("clients", detect_clients, ("presence",)),
    ("credentials", detect_credentials, ("credentials",)),
    ("errors", detect_errors, ("errors",)),
    ("questions", detect_questions, ("questions",)),
    ("answers", detect_answers, ("answers",)),
)


# --------------------------------------------------------------------------- compute + persist

def compute(ctx: dict, seen: dict | None = None) -> tuple:
    """Pure: ``(items ranked, first-seen map, ids that cleared since seen)``."""
    now = ctx["now"]
    ctx.setdefault("sources", {})
    seen = dict(seen or {})
    items = []
    for name, fn, needs in DETECTORS:
        if any(ctx["sources"].get(n, "ok") != "ok" for n in needs):
            continue
        try:
            items.extend(fn(ctx))
        except Exception as exc:                          # noqa: BLE001 - one detector, not the list
            ctx["sources"]["detector:" + name] = "failed: %s" % type(exc).__name__
    by_id = {}
    for it in items:
        by_id.setdefault(it["id"], it)
    fresh = {}
    for ident, it in by_id.items():
        first = float(seen.get(ident) or now)
        fresh[ident] = first
        it["since"] = first
        it["age_s"] = max(0, int(now - first))
    cleared = [i for i in seen if i not in fresh]
    order = {s: n for n, s in enumerate(SEVERITIES)}
    ranked = sorted(by_id.values(), key=lambda i: (order[i["severity"]], -i["age_s"], i["id"]))
    return ranked, fresh, cleared


def _store_path(hub_dir) -> Path:
    return Path(hub_dir) / "attention-seen.json"


def _load(hub_dir) -> dict:
    try:
        doc = json.loads(_store_path(hub_dir).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(hub_dir, doc: dict) -> None:
    path = _store_path(hub_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def build(hub_dir, ctx: dict) -> dict:
    """Compute, persist first-seen (only when the SET changed), and shape the payload."""
    now = ctx["now"]
    store = _load(hub_dir)
    seen = store.get("seen") or {}
    items, fresh, cleared = compute(ctx, seen)
    history = list(store.get("cleared") or [])
    titles = store.get("titles") or {}
    for ident in cleared:
        history.insert(0, {"id": ident, "title": titles.get(ident, ident), "cleared_at": now,
                           "stood_s": int(now - float(seen.get(ident) or now))})
    history = history[:CLEARED_KEEP]
    if set(fresh) != set(seen):
        try:
            _save(hub_dir, {"seen": fresh, "cleared": history,
                            "titles": {it["id"]: it["title"] for it in items}})
        except OSError:
            pass                                          # ages restart; the list is still right
    counts = {s: sum(1 for i in items if i["severity"] == s) for s in SEVERITIES}
    counts["total"] = len(items)
    counts["by_area"] = {a: sum(1 for i in items if i["area"] == a) for a in AREAS}
    actionable = [i for i in items if i["severity"] != "info"]
    if not items:
        verdict = "nothing needs attention"
    elif not actionable:
        verdict = "%s; nothing needs action" % _plural(len(items), "informational item")
    else:
        verdict = ("%s need attention (%d critical); the oldest has stood %s"
                   % (_plural(len(actionable), "item"), counts["critical"],
                      age_phrase(max(i["age_s"] for i in actionable))))
    failed = sorted(k for k, v in ctx.get("sources", {}).items() if v != "ok")
    if failed:
        verdict += " — NOT checked: %s" % ", ".join(failed)
    return {"generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
            "operator": ctx.get("operator") or "", "verdict": verdict, "counts": counts,
            "items": items, "recently_cleared": history, "sources": ctx.get("sources", {}),
            "contract": {"owner_patience_s": OWNER_PATIENCE_S,
                         "operator_patience_s": OPERATOR_PATIENCE_S,
                         "silent_warn_s": SILENT_WARN_S}}


def as_inbox_item(it: dict, *, escalated: bool = False) -> dict:
    """The inbox shape. The id carries first-seen, so a condition that clears and RETURNS is a
    new item delivered again, while a standing one is delivered once."""
    lines = [it["title"]]
    if it.get("detail"):
        lines.append(it["detail"])
    lines.append("Who acts: %s" % (it.get("who") or "the owner"))
    lines.append("Fix: %s" % it["fix"])
    if it.get("evidence"):
        lines.append("Evidence: %s" % json.dumps(it["evidence"], default=str)[:500])
    return {"kind": "attention", "actor": it.get("actor") or actor_for(it.get("kind") or ""),
            "attention_kind": it.get("kind") or "", "machine": it.get("machine") or "",
            "on_its_machine": bool(it.get("on_its_machine")),
            "id": "attention:%s:%d%s" % (it["id"], int(it.get("since") or 0),
                                         ":escalated" if escalated else ""),
            "from": "the hub", "severity": it["severity"], "area": it["area"],
            "title": "[%s] %s%s" % (it["severity"].upper(), it["title"],
                                    " (unaddressed %s)" % age_phrase(it["age_s"]) if escalated else ""),
            "body": "\n".join(lines), "at": "", "waited_s": it["age_s"], "reply_cmd": it["fix"]}


def items_for(agent: str, payload: dict | None) -> list:
    """Attention items ADDRESSED to ``agent``: its own warn/critical ones once they have stood
    OWNER_PATIENCE_S (critical at once), and — for the operator — anybody's that have stood
    OPERATOR_PATIENCE_S. Info never travels, and neither does a condition another lane delivers."""
    if not payload:
        return []
    agent = str(agent or "").strip().lower()
    operator = str(payload.get("operator") or "").lower()
    out = []
    for it in payload.get("items") or []:
        owner = it.get("agent") or operator
        if owner == agent and (it.get("actor") or actor_for(it.get("kind") or "")) == "agent":
            # AN AGENT'S ITEM TRAVELS at any severity, lanes or not: a responder may take it,
            # so "another lane tells the operator" is no reason to hold it back. It still waits
            # OWNER_PATIENCE_S (critical at once), so a condition that clears itself in minutes
            # never spends a session.
            if it["severity"] == "critical" or it["age_s"] >= OWNER_PATIENCE_S:
                out.append(as_inbox_item(it))
            continue
        if it["severity"] == "info":
            continue
        if owner == agent:
            if not it.get("deliver_owner", True):
                continue
            if agent == operator and it.get("lanes"):
                continue
            if it["severity"] == "critical" or it["age_s"] >= OWNER_PATIENCE_S:
                out.append(as_inbox_item(it))
        elif agent == operator and not it.get("lanes") and it["age_s"] >= OPERATOR_PATIENCE_S:
            out.append(as_inbox_item(it, escalated=True))
    return out


def etag(payload: dict) -> str:
    """Ages are NOT in the tag (a reader ticks them from age_s), so a quiet board answers 304."""
    basis = json.dumps([{k: v for k, v in i.items() if k != "age_s"}
                        for i in payload.get("items") or []]
                       + [payload.get("sources"), payload.get("recently_cleared") or []],
                       sort_keys=True, default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]

