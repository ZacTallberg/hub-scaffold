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
  not delivered twice (``lanes``). Nothing here ever launches work: most fixes need the person.
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
CREDENTIAL_WARN_S = 14 * 86400
CREDENTIAL_CRITICAL_S = 3 * 86400
QUESTION_WAIT_S = 2 * 3600
CLEARED_KEEP = 30

SEVERITIES = ("critical", "warn", "info")
AREAS = ("seats", "tasks", "communication", "credentials", "errors", "hub")


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
            "lanes": list(lanes), "deliver_owner": deliver_owner}


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
#   records), errors_unclaimed (bar-clearing unacked error rows), questions (open question items)

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
    """A task held by a console that is no longer live. The board reads in flight, nobody is on
    it, and every automatic close refuses it (the hub never finishes a task another agent holds).
    Fires only when the live console list was actually read: with no consoles at all every lease
    looks orphaned, and an alarm that cannot tell those apart is worse than none."""
    sessions = ctx.get("sessions")
    if not sessions:
        return []
    live = {str(r.get("session") or "")[:8] for r in sessions if not r.get("finished")}
    live.discard("")
    tasks = {t.get("id"): t for t in ctx.get("tasks") or [] if isinstance(t, dict)}
    out = []
    for lease in ctx.get("leases") or []:
        sid = str(lease.get("session") or "")[:8]
        tid = str(lease.get("task") or "")
        if not sid or sid in live:
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
    has been silent a working day WHILE it still holds a live lease (work nobody is doing)."""
    now = ctx["now"]
    leased = {str(l.get("agent") or "").lower() for l in ctx.get("leases") or []}
    out = []
    for agent, row in (ctx.get("presence") or {}).items():
        for m in row.get("machines") or []:
            stamps = [float(m.get(k) or 0) for k in ("heartbeat_at", "activity_at", "last_seen")]
            last = max(stamps) if stamps else 0
            if not last:
                continue
            silent = now - last
            if silent < SILENT_INFO_S:
                continue
            holds = agent.lower() in leased
            severity = "warn" if holds and silent >= SILENT_WARN_S else "info"
            machine = str(m.get("machine") or "")
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


DETECTORS = (
    ("tasks", detect_tasks, ("tasks",)),
    ("unstarted", detect_unstarted, ("tasks", "leases", "runs")),
    ("leases", detect_leases, ("tasks", "leases", "sessions")),
    ("untracked", detect_untracked, ("sessions",)),
    ("seats", detect_seats, ("presence", "leases")),
    ("clients", detect_clients, ("presence",)),
    ("credentials", detect_credentials, ("credentials",)),
    ("errors", detect_errors, ("errors",)),
    ("questions", detect_questions, ("questions",)),
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
    return {"kind": "attention",
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
        if it["severity"] == "info":
            continue
        owner = it.get("agent") or operator
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

