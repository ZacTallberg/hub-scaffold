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
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from .. import client
from ..process_lock import _pid_alive
from . import agent, home, log, machine, read_json, write_json

MAX_ATTEMPTS = 2
TASK_MAX_RUNS = 3
UNATTENDED_CAPABILITY = "unattended"
RESPONSES_KEEP = 300
RUNS_KEEP = 60


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
        if not readiness.get("available"):
            return None
        plan = [step for step in (data.get("plan") or []) if isinstance(step, dict)]
        lines = ["- [%s] %s%s" % ("x" if step.get("done") else " ", step.get("step") or "",
                                  (" -- " + step["note"]) if step.get("note") else "")
                 for step in plan]
        return {"kind": "task", "id": item_id, "title": data.get("title") or "",
                "acceptance": data.get("acceptance") or "", "priority": data.get("priority"),
                "plan_text": "\n".join(lines), "resume": bool(readiness.get("stale_reclaim")),
                "handed_back": int(data.get("handed_back") or 0),
                "from": str((data.get("provenance") or {}).get("agent") or "")}
    try:
        inbox = _read("inbox.json?include=synthetic&agent=%s" % quote(agent())).get("data") or {}
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
        # include=synthetic: a self-test ask (a canary) is for this reader, never a person.
        inbox = _read("inbox.json?include=synthetic&agent=%s" % quote(agent())).get("data") or {}
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
            if item["handed_back"] >= TASK_MAX_RUNS:
                out["waiting"].append({"id": row["id"], "kind": "task", "title": row.get("title"),
                                       "why": "handed back %d times across machines: a person's"
                                              % item["handed_back"]})
                continue
            out["workable"].append({"id": row["id"], "kind": "task", "title": row.get("title"),
                                    "resume": item["resume"], "handed_back": item["handed_back"],
                                    "why": "RESUME: a run left it in progress" if item["resume"]
                                    else "unattended and ready"})
    except (RuntimeError, ValueError) as error:
        out["errors"].append(str(error)[:300])
    return out


# ---------------------------------------------------------------- leases

def claim(task_id: str, ttl_s: int) -> tuple[str, dict]:
    status, body = _write("claim", {"id": task_id, "agent": agent(), "ttl_s": ttl_s})
    token = body.get("token") if isinstance(body, dict) else None
    return (token or ""), body


def heartbeat(task_id: str, token: str, ttl_s: int) -> bool:
    status, body = _write("heartbeat", {"id": task_id, "token": token, "ttl_s": ttl_s})
    return status == 200 and bool(body.get("ok"))


def hand_back(task_id: str, token: str, note: str) -> dict:
    status, body = _write("hand-back", {"id": task_id, "token": token, "agent": agent(),
                                        "note": note[:600]})
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


def release_left(leases_file: str, note: str, skip: str = "", uncharged: bool = False) -> list[dict]:
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
        result = hand_back(row["task"], row["token"], note)
        if result.get("status") == 200:
            freed.append({"task": row["task"], "handed_back": result.get("handed_back")})
            log("teardown: handed back %s (this run's lease, proven by its token)" % row["task"])
    return freed


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
    cleared on wake). Only the board's done is done; an unreadable board is NOT stale."""
    if ":task:" not in item_id or record.get("state") != "done":
        return False
    status = task_status(item_id)
    return status is not None and status != "done"


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
