"""The board is the only authority on what a run did — and the launcher's local bookkeeping.

OUTCOME FROM STATE, NEVER FROM THE EXIT CODE. A run's outcome for a task is the task's status on
the board after the run: ``done`` -> cleared, still ``in_progress`` -> left-active, ``todo`` ->
not-cleared. "No longer offered to me" is NOT cleared: a task a run left in progress is no longer
offered either, and reading it as cleared journals open work as finished. A question is cleared
when it is no longer open. An unreadable board is not-cleared (fail closed; never a false success).

RESUME, NOT ABANDON. A task left ``in_progress`` by a run that ended — its lease released or
expired — is ``readiness.stale_reclaim`` on the board. It is offered again, and the prompt says
RESUMING: the next run's first job is whatever the last one recorded (the pushed sha, the pipeline
it was waiting on), not starting over.

HAND-BACK WITH PROOF. The launcher claims the task itself and hands the session the fencing token
(``HUB_LEASE_TOKEN``) plus a lease journal (``HUB_RUN_LEASES``, which the client appends to on
every claim). At teardown every journalled lease whose task is still ``in_progress`` is handed
back through ``/hub/api/hand-back``: the hub checks the token, so the launcher can only ever
release a lease THIS run held — never a person's, never another run's. A run killed so hard that
its launcher died too is swept by the next launcher: its journal names the leases, its recorded
pid is gone, and the same proof applies.

ATTEMPTS ARE CHARGED ONLY FOR REAL TRIES. Per item, on this machine: ``MAX_ATTEMPTS``. A lane
fault (usage limit, dead harness, API error) refunds its attempt. A local ``done`` record the
board contradicts is dropped and refunded. A run whose launcher died mid-run is refunded after
twice the longest bound. Across machines, a task that has been handed back ``TASK_MAX_RUNS``
times (the hand-back row's own count, on the board, read by every machine) is left for a person.
Only a run that did NEW work (a checkpoint or a push after it started) counts toward that cap: a
resume that re-read a pipeline still running, or waited on a push it could not make, did not lose
to the task, and charging it retired tasks at the cap with their fix live. Such a run is handed
back IDLE -- counted apart, with its own backstop at twice the cap, so it can never loop forever.
A task at either cap gets ``needs_person`` on the board (reason, last pushed sha, the failing
evidence), written once, because a cap that only logs locally leaves a task reading "todo,
unattended" everywhere else and nobody learns it is theirs.

A REFUSAL IS A RESPONSE. A stand-down behind another machine's claim, an escalation still in its
cooldown, an item at its cap or behind the hourly ceiling each writes a ``deferred_until`` record,
and the scan does not offer that item again before it: without one, a capped item was re-offered
and refused every cycle, and an item that stood down behind a machine that then went silent was
never offered again.

ONE MACHINE PER ITEM. A task has its fenced lease. Anything else (a question, a needs-attention
condition) is claimed through ``/hub/api/item-claim`` BEFORE a run is queued, refreshed while it
waits for a lane, and released the moment the run ends -- so a second machine stands down instead
of spending a session on the same thing. An unreachable claim route proceeds, loudly: a rare
double-fix during a hub blip costs less than a problem nobody fixes during one.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from .. import client
from .. import task_rows
from ..process_lock import _pid_alive
from . import agent, home, log, machine, read_json, write_json

MAX_ATTEMPTS = 2
TASK_MAX_RUNS = 3
UNATTENDED_CAPABILITY = "unattended"
RESPONSES_KEEP = 300
RUNS_KEEP = 60
#: A stand-down behind another machine's claim is looked at again after this long -- not the
#: claim's TTL: a finished run releases its claim at once, and waiting out the TTL stranded the
#: item long after its holder let go. A re-check costs one launcher start, never a session.
HELD_RECHECK_S = 900
#: An item at its attempt cap is looked at again this rarely (and refused again, cheaply).
CAPPED_RECHECK_S = 6 * 3600
#: A stand-down behind the hourly launch ceiling is looked at again once the hour has moved.
CEILING_RECHECK_S = 600
ATTENTION_PREFIX = "attention:"


def base() -> str:
    return client._base_url(None)


def _read(path: str) -> dict:
    return client._get(base(), path)


def _write(operation: str, payload: dict) -> tuple[int, dict]:
    """POST through the served seam; (status, body). Never raises."""
    headers = {"X-Hub-Machine": machine(), "X-Hub-Focus": "unattended launcher"}
    try:
        return 200, client._post(base(), operation, payload, extra_headers=headers)
    except RuntimeError as error:
        try:
            parsed = json.loads(str(error))
            return int(parsed.get("status") or 0), parsed.get("response") or {}
        except (ValueError, AttributeError):
            return 0, {"error": str(error)[:300]}
    except ValueError as error:
        return 0, {"error": str(error)[:300]}


# ---------------------------------------------------------------- reading items

def task(task_id: str) -> dict | None:
    """The task as the board holds it NOW (with holder / readiness / handed_back), or None."""
    try:
        data = _read("task/%s.json" % quote(task_id.rsplit(":", 1)[-1])).get("data")
    except RuntimeError:
        return None
    return data if isinstance(data, dict) else None


def task_status(task_id: str) -> str | None:
    data = task(task_id)
    return str(data.get("status") or "") if data else None


def outcome(status: str | None) -> str:
    return {"done": "cleared", "in_progress": "left-active", "todo": "not-cleared"}.get(
        status or "", "not-cleared")


def is_unattended(data: dict) -> bool:
    """A task is an unattended responder's only when it SAYS so: its routing requires the
    ``unattended`` capability. An unmarked task stays a person's — most ready work belongs to
    whoever created it, and a session that takes it anyway is work nobody asked for."""
    required = ((data.get("routing") or {}).get("required_capabilities") or [])
    return UNATTENDED_CAPABILITY in [str(c).lower() for c in required]


def resolve(item_id: str) -> dict | None:
    """The item, live — never trusted from the spawn argument. None when it is no longer offered
    here (somebody else took it, it closed, it was un-marked); ``{"kind": "unavailable"}`` when
    the hub cannot be read, which is NEVER read as cleared."""
    if ":task:" in item_id:
        data = task(item_id)
        if data is None:
            try:
                _read("cursor.json")
            except RuntimeError:
                return {"kind": "unavailable", "id": item_id}
            return None
        if not is_unattended(data):
            return None
        readiness = data.get("readiness") or {}
        holder = data.get("holder") if isinstance(data.get("holder"), dict) else {}
        # A holder the hub reports ABANDONED (its console is not among the live ones) is nobody's,
        # whoever held it: a task claimed by a machine that then went silent otherwise waits out
        # the whole lease. Offered as a resume; the hub's claim gate decides whether the takeover
        # is granted (it takes over only a holder it can PROVE gone), so this offers nothing the
        # hub will not grant, and a refusal is a cheap stand-down, never a session.
        abandoned = holder.get("abandoned") is True and holder.get("live") is not True
        if not readiness.get("available") and not (readiness.get("state") == "leased"
                                                   and abandoned):
            return None
        plan = [step for step in (data.get("plan") or []) if isinstance(step, dict)]
        lines = ["- [%s] %s%s" % ("x" if step.get("done") else " ", step.get("step") or "",
                                  (" -- " + step["note"]) if step.get("note") else "")
                 for step in plan]
        charged, idle = hand_back_counts(data)
        return {"kind": "task", "id": item_id, "title": data.get("title") or "",
                "acceptance": data.get("acceptance") or "", "priority": data.get("priority"),
                "plan_text": "\n".join(lines),
                "resume": bool(readiness.get("stale_reclaim")) or abandoned,
                "abandoned_by": str(holder.get("agent") or "") if abandoned else "",
                "handed_back": charged, "handed_back_idle": idle,
                "from": str((data.get("provenance") or {}).get("agent") or "")}
    if item_id.startswith(ATTENTION_PREFIX):
        return resolve_attention(item_id)
    try:
        inbox = _read("inbox.json?agent=%s" % quote(agent())).get("data") or {}
    except RuntimeError:
        return {"kind": "unavailable", "id": item_id}
    for item in inbox.get("items") or []:
        if isinstance(item, dict) and item.get("id") == item_id and item.get("kind") == "question":
            return dict(item)
    return None


def scan() -> dict:
    """Everything an unattended responder here could take, and everything it may not, with why.

    Questions: the operator's inbox (this launcher's agent must be the operator identity to see
    them). Tasks: marked unattended and ready (including a stale in-progress task: a resume)."""
    from . import escalation
    out = {"workable": [], "waiting": [], "hub": "", "errors": []}
    try:
        out["hub"] = base()
        inbox = _read("inbox.json?agent=%s" % quote(agent())).get("data") or {}
        for item in inbox.get("items") or []:
            if not isinstance(item, dict) or item.get("kind") != "question":
                continue
            ok, why = escalation.workable(item)
            row = {"id": item.get("id"), "kind": "question", "title": item.get("title"),
                   "hop": escalation.hop_of(item), "why": why}
            (out["workable"] if ok else out["waiting"]).append(row)
        tasks = _read("task.json").get("data") or []
        for row in tasks:
            if not isinstance(row, dict) or row.get("status") not in ("todo", "in_progress"):
                continue
            if not is_unattended(row):
                continue
            item = resolve(row["id"])
            if not item or item.get("kind") != "task":
                out["waiting"].append({"id": row["id"], "kind": "task", "title": row.get("title"),
                                       "why": "held or not ready"})
                continue
            capped = at_run_cap(item)
            if capped:
                out["waiting"].append({"id": row["id"], "kind": "task", "title": row.get("title"),
                                       "why": capped + ": a person's"})
                continue
            out["workable"].append({"id": row["id"], "kind": "task", "title": row.get("title"),
                                    "resume": item["resume"], "handed_back": item["handed_back"],
                                    "why": ("RESUME: its holder %s is gone" % item["abandoned_by"])
                                    if item["abandoned_by"] else
                                    "RESUME: a run left it in progress" if item["resume"]
                                    else "unattended and ready"})
        listing = attention_list()
        if listing is None:
            out["errors"].append("the needs-attention list is not built or not readable; "
                                 "no attention item offered")
        for it in (listing or {}).get("items") or []:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            ok, why = attention_offered(it)
            row = {"id": ATTENTION_PREFIX + str(it["id"]), "kind": "attention",
                   "title": it.get("title"), "why": why}
            (out["workable"] if ok else out["waiting"]).append(row)
    except (RuntimeError, ValueError) as error:
        out["errors"].append(str(error)[:300])
    # A refusal is a response: an item deferred by an earlier launcher is not offered before
    # its time, and the scan says so instead of launching a process that refuses again.
    offered = []
    for row in out["workable"]:
        why = deferred(row["id"])
        if why:
            out["waiting"].append({**row, "why": why})
        else:
            offered.append(row)
    out["workable"] = offered
    return out


def at_run_cap(item: dict) -> str:
    """Why a task is past the cross-machine run cap, or ''. Charged runs cap at TASK_MAX_RUNS;
    idle runs (no new checkpoint or push) are counted apart and backstop at twice that."""
    charged = int(item.get("handed_back") or 0)
    idle = int(item.get("handed_back_idle") or 0)
    if charged >= TASK_MAX_RUNS:
        return "handed back %d times across machines" % charged
    if charged + idle >= 2 * TASK_MAX_RUNS:
        return "handed back %d times across machines, %d of them runs that did no new work" % (
            charged + idle, idle)
    return ""


def hand_back_counts(data: dict) -> tuple[int, int]:
    """(charged runs, idle runs) from the task's hand-back row -- read off the PLAN, which every
    machine sees, never a local count. A zero is a real count, never "absent"."""
    charged = idle = 0
    for step in data.get("plan") or []:
        if isinstance(step, dict) and step.get("kind") == "handed_back":
            charged += task_rows.charged_runs(step)
            idle += task_rows.idle_runs(step)
    return charged, idle


# ---------------------------------------------------------------- needs-attention items

def attention_list() -> dict | None:
    """The hub's needs-attention LIST, or None when it cannot be told (unreachable, or not built
    yet -- a list with no ``generated_at`` is a hub that has not computed it since it started)."""
    try:
        data = _read("attention.json").get("data")
    except RuntimeError:
        return None
    if not isinstance(data, dict) or not data.get("generated_at") \
            or not isinstance(data.get("items"), list):
        return None
    return data


def attention_base_id(item_id: str) -> str:
    """``attention:<kind>:<subject>[:<since>][:escalated]`` -> the hub item's own id."""
    rest = item_id[len(ATTENTION_PREFIX):] if item_id.startswith(ATTENTION_PREFIX) else item_id
    for suffix in (":escalated", ":leader"):
        if rest.endswith(suffix):
            rest = rest[:-len(suffix)]
    head, _, tail = rest.rpartition(":")
    return head if head and tail.isdigit() else rest


def attention_standing(item_id: str):
    """Is this condition still standing? True / False, or None when that cannot be told.

    THE LIST IS THE AUTHORITY, NEVER THE INBOX: the inbox carries attention items only once the
    list has been built, so for a while after every hub restart it carries none, and a condition
    read through it would be recorded cleared while it still stands."""
    listing = attention_list()
    if listing is None:
        return None
    base_id = attention_base_id(item_id)
    return any(isinstance(i, dict) and i.get("id") == base_id for i in listing["items"])


def attention_offered(it: dict) -> tuple[bool, str]:
    """(may an unattended responder here take this condition, why). Only what the hub marks
    ``actor: agent`` -- a list without the field says nothing about who can clear it, and a
    guess would put a session on something only a person can do. Informational items travel
    nowhere; an item that can only be cleared ON the machine it names is offered only there."""
    actor = str(it.get("actor") or "")
    if actor != "agent":
        return False, ("a person's (%s)" % (it.get("who") or "the owner") if actor == "person"
                       else "the hub does not say an agent can clear it (no actor field)")
    if it.get("severity") == "info":
        return False, "informational: shown on the card, never worked"
    target = str(it.get("machine") or "").strip().lower()
    if it.get("on_its_machine") and target and target != machine():
        return False, "can only be cleared on %s" % target
    return True, "needs attention, and an agent can clear it"


def resolve_attention(item_id: str) -> dict | None:
    listing = attention_list()
    if listing is None:
        return {"kind": "unavailable", "id": item_id}
    base_id = attention_base_id(item_id)
    it = next((i for i in listing["items"] if isinstance(i, dict) and i.get("id") == base_id),
              None)
    if it is None or not attention_offered(it)[0]:
        return None
    lines = [str(it.get("title") or "")]
    if it.get("detail"):
        lines.append(str(it["detail"]))
    lines.append("Who acts: %s" % (it.get("who") or "the owner"))
    lines.append("Fix: %s" % (it.get("fix") or ""))
    if it.get("evidence"):
        lines.append("Evidence: %s" % json.dumps(it["evidence"], default=str)[:800])
    return {"kind": "attention", "id": item_id, "title": str(it.get("title") or "")[:300],
            "from": "the hub", "attention_kind": str(it.get("kind") or "attention"),
            "body": "\n".join(lines)[:4000]}


# ---------------------------------------------------------------- leases

def claim(task_id: str, ttl_s: int) -> tuple[str, dict]:
    status, body = _write("claim", {"id": task_id, "agent": agent(), "ttl_s": ttl_s})
    token = body.get("token") if isinstance(body, dict) else None
    return (token or ""), body


def heartbeat(task_id: str, token: str, ttl_s: int) -> bool:
    status, body = _write("heartbeat", {"id": task_id, "token": token, "ttl_s": ttl_s})
    return status == 200 and bool(body.get("ok"))


def hand_back(task_id: str, token: str, note: str, idle: bool = False) -> dict:
    payload = {"id": task_id, "token": token, "agent": agent(), "note": note[:600]}
    if idle:
        payload["idle"] = True          # counted apart from the cap (the hub keeps the count)
    status, body = _write("hand-back", payload)
    return {"status": status, **(body if isinstance(body, dict) else {})}


def journalled_leases(path: str) -> list[dict]:
    rows, seen = [], set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = (row.get("task"), row.get("token"))
        if row.get("task") and row.get("token") and key not in seen:
            seen.add(key)
            rows.append(row)
    return rows


def release(task_id: str, token: str) -> bool:
    status, body = _write("release", {"id": task_id, "token": token, "agent": agent()})
    return status == 200 and bool(body.get("ok"))


def release_left(leases_file: str, note: str, skip: str = "", uncharged: bool = False,
                 idle_task: str = "") -> list[dict]:
    """Hand back every lease this run journalled whose task is still in progress. The hub
    proves each one (token + subject); a lease somebody else now holds is refused, not taken.

    ``uncharged``: the run never worked the item (a LANE fault — usage limit, dead harness, API
    error). A hand-back row counts toward the cross-machine cap, and a lane fault is not a try on
    the item, so the lease is only RELEASED: the task stays in progress with nobody holding it,
    which the board reads as ``stale_reclaim`` — re-offered as a resume, never charged."""
    freed = []
    for row in journalled_leases(leases_file):
        if row["task"] == skip:
            continue
        if task_status(row["task"]) != "in_progress":
            continue
        if uncharged:
            if release(row["task"], row["token"]):
                freed.append({"task": row["task"], "released": True})
                log("teardown: released %s uncharged (a lane fault is not a try on the item)"
                    % row["task"])
            continue
        idle = bool(idle_task) and row["task"] == idle_task
        result = hand_back(row["task"], row["token"], note, idle=idle)
        if result.get("status") == 200:
            freed.append({"task": row["task"], "handed_back": result.get("handed_back"),
                          "idle": idle})
            log("teardown: handed back %s%s (this run's lease, proven by its token)"
                % (row["task"], " as an idle run" if idle else ""))
        elif result.get("status") != 409:
            # A refused hand-back leaves the task active with nobody on it: say so on the board.
            # (409 is the lease no longer being this run's -- somebody else holds the task.)
            report_fault("unattended_hand_back_refused",
                         "the hub refused the hand-back of a task an unattended run left active",
                         "task=%s status=%s\n%s" % (row["task"], result.get("status"),
                                                     json.dumps(result, default=str)[:600]),
                         severity="warning")
    return freed


# ---------------------------------------------------------------- per-item claims (non-task)

def item_claim(item_id: str, release: bool = False) -> str:
    """``granted``, ``held:<machine>`` or ``unreachable`` for a NON-task item (a task has its
    lease). A same-machine re-claim is granted idempotently, which is how a queued launcher
    keeps its claim alive."""
    payload = {"item": item_id, "agent": agent(), "machine": machine()}
    if release:
        payload["release"] = True
    status, body = _write("item-claim", payload)
    if status == 200:
        return "released" if release else "granted"
    if status == 409:
        data = (body.get("data") if isinstance(body, dict) else None) or {}
        return "held:%s" % (data.get("holder") or "another machine")
    return "unreachable"


# ---------------------------------------------------------------- the task record itself

def did_new_work(data: dict, since: float) -> bool:
    """Did anything on the task move after ``since``: a work checkpoint or a push?"""
    from datetime import datetime

    def at(stamp) -> float:
        try:
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0
    pushed = (data or {}).get("pushed") if isinstance((data or {}).get("pushed"), dict) else {}
    if pushed and at(pushed.get("at")) >= since:
        return True
    return any(isinstance(step, dict) and not step.get("lifecycle")
               and step.get("kind") != "handed_back" and at(step.get("note_at")) >= since
               for step in (data or {}).get("plan") or [])


def failing_evidence(data: dict) -> str:
    """What the NEXT person needs from a run that did not close its task, from the hub's own
    joins on the record: the last pushed sha, its pipeline, whether a verified deploy found it
    live, and -- when the error stream holds one against that commit -- the failure itself. A
    hand-back that says only "ended with the task still active" sends whoever it escalates to
    off to find what the board already knew. '' when nothing was pushed."""
    pushed = (data or {}).get("pushed") if isinstance((data or {}).get("pushed"), dict) else {}
    sha = str(pushed.get("sha") or "")
    if not sha:
        return ""
    line = "Last pushed %s" % sha[:12]
    if pushed.get("pipeline_id"):
        line += " (pipeline %s)" % pushed["pipeline_id"]
    deployed = data.get("deployed") if isinstance(data.get("deployed"), dict) else {}
    dsha = str(deployed.get("sha") or "")
    if dsha and (dsha.startswith(sha[:12]) or sha.startswith(dsha[:12])):
        line += ", live: a verified deploy found it"
    problem = data.get("ci_problem") if isinstance(data.get("ci_problem"), dict) else {}
    if problem:
        line += ", FAILING: %s (%s)" % (str(problem.get("message") or "")[:220],
                                       problem.get("source") or "error stream")
    elif not dsha:
        line += ", no failure recorded against it and not yet seen live"
    return line + "."


def _upsert(task_id: str, fields: dict, token: str = "") -> bool:
    """Merge ``fields`` onto the task through the generic upsert, once more on a version race."""
    for _attempt in range(2):
        data = task(task_id)
        if not data:
            return False
        payload = dict(fields, id=task_id, agent=agent(), expected_version=data.get("version"))
        if token:
            payload["token"] = token
        status, body = _write("task", payload)
        if status == 200:
            return True
        if status != 409 or "conflict" not in json.dumps(body, default=str):
            log("could not set %s on %s (%s %s)" % (",".join(sorted(fields)), task_id, status,
                                                    json.dumps(body, default=str)[:200]))
            return False
    return False


def flag_needs_person(task_id: str, reason: str) -> bool:
    """A TASK AT ITS CAP IS A PERSON'S, AND THE BOARD MUST SAY SO: ``needs_person`` with the
    reason, the last pushed sha and the failing evidence. Written once, never overwritten by a
    later refusal."""
    if ":task:" not in task_id:
        return False
    data = task(task_id)
    if not data or data.get("needs_person") or data.get("status") == "done":
        return False
    evidence = failing_evidence(data)
    if evidence:
        reason = "%s. %s" % (reason.rstrip("."), evidence)
    pushed = data.get("pushed") if isinstance(data.get("pushed"), dict) else {}
    ok = _upsert(task_id, {"needs_person": {
        "reason": reason[:600], "sha": str(pushed.get("sha") or "")[:40],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "by": ("%s@%s" % (agent(), machine()))[:120]}})
    if ok:
        log("%s flagged needs_person (%s)" % (task_id, reason[:160]))
    return ok


def record_project(task_id: str, project: str, token: str = "") -> bool:
    """Put the resolved repository on the TASK as its ``project`` when it names none: a deploy
    record, a commit resolver and the next run all need the project, and a title slug is a guess
    that has missed before. A project somebody already set is never replaced."""
    if not project:
        return False
    data = task(task_id)
    if not data or data.get("project"):
        return False
    return _upsert(task_id, {"project": project}, token=token)


# ---------------------------------------------------------------- the lane's own failures

def report_fault(code: str, message: str, details: str, severity: str = "error") -> None:
    """A failure of the LANE, on the surface somebody works — folded per (agent, machine, code,
    message), so a lane down for an hour is ONE row with a count, not a hundred. Fail-soft: the
    pass is already failing, and an unreachable hub must not make one silent failure two."""
    _write("agent-error", {"agent": agent(), "source": "unattended-lane@" + machine(),
                           "severity": severity, "code": code, "message": message[:800],
                           "details": details[:2000], "machine": machine(),
                           "component": "unattended-launcher"})


# ---------------------------------------------------------------- local bookkeeping

def _responses_path() -> Path:
    return home() / "responses.json"


def responses() -> dict:
    return read_json(_responses_path(), {})


def save_responses(data: dict) -> None:
    if len(data) > RESPONSES_KEEP:
        data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[-RESPONSES_KEEP:])
    write_json(_responses_path(), data)


def stale_done(item_id: str, record: dict) -> bool:
    """A local ``done`` the board contradicts (a run suspended with the machine was journalled
    cleared on wake; a condition read as gone through an inbox that was merely not built yet).
    Only the board's done is done; an unreadable board is NOT stale."""
    if record.get("state") != "done":
        return False
    if item_id.startswith(ATTENTION_PREFIX):
        return attention_standing(item_id) is True
    if ":task:" not in item_id:
        return False
    status = task_status(item_id)
    return status is not None and status != "done"


def defer(item_id: str, until: float, why: str, attempts: int = 0) -> None:
    """A stand-down or a not-yet is a RESPONSE with a time it expires; the attempt count is
    kept, so a capped item refused again stays capped."""
    records = responses()
    records[item_id] = {"state": "deferred", "at": time.time(), "deferred_until": float(until),
                        "note": why[:200], "attempts": int(attempts or 0)}
    save_responses(records)


def deferred(item_id: str) -> str:
    """Why the item is deferred and until when, or '' when it may be offered now."""
    record = responses().get(item_id) or {}
    until = record.get("deferred_until")
    if record.get("state") != "deferred" or until is None or float(until) <= time.time():
        return ""
    return "deferred until %s: %s" % (time.strftime("%H:%M", time.localtime(float(until))),
                                      record.get("note") or "")


def _runs_path() -> Path:
    return home() / "runs.json"


def runs() -> dict:
    return read_json(_runs_path(), {})


def record_run(run_id: str, **fields) -> dict:
    data = runs()
    row = data.get(run_id) or {"id": run_id, "created": time.time(), "machine": machine(),
                               "launcher_pid": os.getpid()}
    row.update(fields)
    data[run_id] = row
    if len(data) > RUNS_KEEP:
        data = dict(sorted(data.items(), key=lambda kv: kv[1].get("created", 0))[-RUNS_KEEP:])
    write_json(_runs_path(), data)
    return row


def drop_run(run_id: str) -> None:
    """A stand-down that spent nothing and holds nothing leaves no row: kept, a respawn storm of
    stand-downs pushed the ENDED runs a lease proof needs out of the bounded ledger."""
    data = runs()
    if data.pop(run_id, None) is not None:
        write_json(_runs_path(), data)


def launches_in_last_hour(prefix: str = "") -> int:
    """Sessions actually LAUNCHED on this machine in the trailing hour (optionally only items
    whose id starts with ``prefix``) -- from the run ledger, never from what was queued."""
    now = time.time()
    return sum(1 for row in runs().values()
               if isinstance(row, dict) and row.get("launched") is not None
               and now - float(row["launched"]) < 3600
               and (not prefix or str(row.get("item") or "").startswith(prefix)))


def new_run_id() -> str:
    return "run-%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))


def sweep_orphans() -> list[dict]:
    """Runs whose LAUNCHER died mid-run (power loss, a kill): their leases outlive them. Hand
    back what they journalled, with the same token proof, and mark them ended."""
    swept = []
    for run_id, row in runs().items():
        if row.get("state") not in ("queued", "running"):
            continue
        if _pid_alive(int(row.get("launcher_pid") or 0)):
            continue
        freed = release_left(row.get("leases_file") or "", (
            "The unattended run holding this task ended without its launcher (the launcher "
            "process is gone); handed back by the next launcher on this machine."))
        record_run(run_id, state="ended", outcome="orphaned", ended=time.time(), freed=freed)
        swept.append({"run": run_id, "item": row.get("item"), "freed": freed})
    return swept
