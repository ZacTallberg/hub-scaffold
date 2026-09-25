#!/usr/bin/env python3
"""Event-driven, one-item unattended responders — spend a session only when something happened.

A scheduled "worker pass" that wakes on a timer and looks at the queue is the expensive way to be
autonomous: on the system this was extracted from, 26 timer-launched passes in 26 hours spent tens
of millions of tokens and every one of them woke to a queue it was correctly forbidden to touch.
An EVENT that has actually happened is the better trigger. This module is that launcher:

    python -m hub_core.responder poll            # read the board once; launch a responder per workable item
    python -m hub_core.responder poll --dry-run  # show what would launch and why, launch nothing
    python -m hub_core.responder respond <item>  # ONE bounded session for ONE item, then gone
    python -m hub_core.responder status          # the local run ledger: waiting / running / ended
    python -m hub_core.responder canary          # prove the ask -> responder -> answer loop (daily)
    python -m hub_core.responder check-env [--report]   # environment repair table, OK / FIXED / NEEDS A PERSON

WHAT IS WORK (and what never is). Three kinds of item wake a responder:
  * a QUESTION addressed to the operator identity this responder answers for;
  * an unclaimed operational ERROR on the bar, and only a FRESH one (last seen within a day) —
    an old unclaimed error stays listed for a person, it is never pushed into a session;
  * a TASK explicitly marked for the unattended lane (``routing.required_capabilities``
    contains ``unattended``), still ``todo``.
A task that is merely rotting (in progress, nobody moving) is NEVER a wake reason: a stalled task
is a human decision — close, resume, or hand back — and waking a full session per stalled task
was measured at 7.7M tokens burned on one abandoned task. Surface it aggressively; act on it
conservatively. A ready queue is not a wake reason either: most of it belongs to whoever filed it.

SAFE BY CONSTRUCTION. The launcher never ACTS; every judgement lives in the bounded session and
its charter. The launcher: re-reads the item LIVE before spending anything and stands down if it
was taken or cleared; caps attempts per item; refuses any item raised by another unattended
session (``via=responder`` — stamped mechanically by ``hub_core.client ask`` whenever
``HUB_UNATTENDED=1``, which this launcher sets); runs one session per lane at a time; bounds each
session with a per-kind clock the session is TOLD; kills the whole process tree at the clock and
proves the pid is gone; and verifies the outcome from the BOARD, never from the session's
self-report.

NON-INTERACTIVE. An unattended session can never raise a credential dialog (git's terminal prompt
and the credential manager's UI are both disabled in its environment — a prompt nobody is
watching is not a pause, it is a hang to the timeout) and never inherits a stdin it could block
on (``stdin=DEVNULL``: a runtime that reads piped input from a fresh console blocks forever before
its first model call — measured at 46% of scheduled passes dying with zero output).

INVISIBLE ON WINDOWS. The session gets its OWN console, hidden (``CREATE_NEW_CONSOLE`` +
``SW_HIDE``), not ``CREATE_NO_WINDOW``: a process with no console makes every console grandchild
its tools spawn allocate a fresh VISIBLE window on the owner's desktop. Leaf helpers (git,
taskkill) run with ``CREATE_NO_WINDOW``. Schedule the ``poll`` under ``pythonw.exe``
(``adapters/windows/register-responder.ps1``) so the scheduler shows no window either.

EVERY RUN IS A RECORD. The local ledger (``<home>/runs.json``) holds one row per session from the
moment its launcher starts WAITING for a lane (``queued``) through ``started`` to a verified
``outcome``; the run id and item ride the session's environment (``HUB_RUN_ID``, ``HUB_RUN_KIND``,
``HUB_RUN_ITEM``, ``HUB_RUN_TITLE``), and the board sees each run as its own presence seat whose
focus says what it is doing. On EVERY terminal outcome the launcher posts a board note itself —
naming any uncommitted files the session left behind (it never commits them) — so an abandoned
pass is loud, never silent.

Configuration (environment): HUB_API_BASE, HUB_AGENT_TOKEN (or HUB_WRITE_TOKEN), HUB_AGENT_ID
(the identity whose inbox this responder drains — the operator identity for questions),
HUB_RESPONDER_RUNTIME (the session command: a JSON argv list or a shell-style string, with
``{prompt_file}`` or ``{prompt}`` where the prompt goes), HUB_RESPONDER_WORKSPACE (session working
directory, default the current one), HUB_RESPONDER_HOME (state directory, default
``~/.hub-responder``), HUB_MACHINE. Kill switch: create ``<home>/DISABLED``.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from . import client as hub
from . import textguard

UNATTENDED_CAPABILITY = "unattended"
LOOP_STAMP = "via=responder"
SHORT_BOUND_S = 1500          # a question or an error: a fix plus its ship cycle
TASK_BOUND_S = 5400           # a task: build, push, wait for the deploy to go green, record
SHIP_BY_MARGIN_MIN = 8        # the session is told to push/record this long before the kill
MAX_ATTEMPTS = 2              # per item, ever, on this machine; then it is a person's
MAX_SPAWN_PER_POLL = 3        # one poll never starts more launchers than this
FRESH_S = 24 * 3600           # an error older than this is listed for a person, never worked
LOCK_STALE_S = 900            # a lane lock nobody has touched for this long is a dead holder's
LOCK_BEAT_S = 60
QUEUE_RECHECK_S = 120         # a queued launcher re-reads its item this often and stands down
RUNS_KEEP = 60
ENV_CHECK_EVERY_S = 24 * 3600
CANARY_EVERY_S = 24 * 3600
CANARY_OVERDUE_S = 1800
CANARY_ABSENT_READS = 3
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW, leaf helpers only


# ---------------------------------------------------------------- configuration and state

def _home() -> Path:
    raw = os.environ.get("HUB_RESPONDER_HOME")
    return Path(raw) if raw else Path(os.path.expanduser("~")) / ".hub-responder"


def _agent() -> str:
    return (os.environ.get("HUB_AGENT_ID") or "operator").strip().lower()


def _machine() -> str:
    return (os.environ.get("HUB_MACHINE") or platform.node() or "this-machine").strip().lower()


def _workspace() -> str:
    return os.environ.get("HUB_RESPONDER_WORKSPACE") or os.getcwd()


def _bound(kind: str) -> int:
    name = "HUB_RESPONDER_TASK_S" if kind == "task" else "HUB_RESPONDER_SHORT_S"
    default = TASK_BOUND_S if kind == "task" else SHORT_BOUND_S
    try:
        return max(60, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _lane(kind: str) -> str:
    """A task has the long clock and its own lane, so a one-minute answer never waits behind a
    ninety-minute build."""
    return "long" if kind == "task" else "short"


def _read_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    """Atomic replace, RETRIED against a clock (a reader, an indexer or a scanner holding the
    destination makes a Windows replace fail for a moment) and logged when it is still lost --
    never silently dropped, which made a queued or running run vanish from the ledger."""
    from .atomic import replace
    temp = path.with_suffix(".tmp.%d" % os.getpid())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
        replace(temp, path, timeout_s=5.0)
    except OSError as error:
        try:
            temp.unlink()
        except OSError:
            pass
        _log("ledger write LOST for %s (%s: %s)" % (path.name, type(error).__name__, error))


def _log(message: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        _home().mkdir(parents=True, exist_ok=True)
        with (_home() / "responder.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


def _disabled() -> str:
    return "kill switch file present: %s" % (_home() / "DISABLED") \
        if (_home() / "DISABLED").exists() else ""


# ---------------------------------------------------------------- the run ledger

def _runs_path() -> Path:
    return _home() / "runs.json"


def _run_update(run_id: str, **fields) -> None:
    if not run_id:
        return
    data = _read_json(_runs_path(), {})
    record = data.get(run_id)
    if not isinstance(record, dict):
        return
    if record.get("ended") and "ended" not in fields:
        return                           # the first terminal reading stands
    record.update(fields)
    _write_json(_runs_path(), data)


def _run_queued(kind: str, item: str, title: str, bound_s: int, attempt: int) -> str:
    """A launcher WAITING for a lane is already a run: the record exists from the moment it
    waits (``queued`` + this launcher's pid, no ``started``), so a request that sits behind a
    long build shows as waiting instead of appearing nowhere."""
    run_id = "%s-%s-%s" % (kind, time.strftime("%Y%m%dT%H%M%S"), hashlib.sha1(
        ("%s|%s|%s" % (kind, item, time.time())).encode("utf-8")).hexdigest()[:6])
    data = _read_json(_runs_path(), {})
    data[run_id] = {"kind": kind, "item": str(item)[:120], "title": str(title or "")[:160],
                    "queued": time.time(), "bounded_s": int(bound_s), "attempt": int(attempt),
                    "pid": os.getpid(), "agent": _agent(), "machine": _machine(),
                    "workspace": _workspace()[:200]}
    items = sorted(data.items(), key=lambda kv: float((kv[1] or {}).get("queued") or 0))
    _write_json(_runs_path(), dict(items[-RUNS_KEEP:]))
    return run_id


def _run_finished(run_id: str, outcome: str, rc=None, **extra) -> None:
    fields = {"ended": time.time(), "outcome": str(outcome)[:32], "rc": rc, "pid": None}
    fields.update(extra)
    _run_update(run_id, **fields)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True,
                             text=True, timeout=20, creationflags=_NO_WINDOW).stdout or ""
        return str(pid) in out
    except (OSError, subprocess.SubprocessError):
        return False


def status() -> dict:
    """The ledger with each row's honest state: a record with a live pid and no ``started`` is
    WAITING for a lane; a live pid with ``started`` is RUNNING; a dead pid with no ``ended`` is a
    launcher that died without writing its outcome (said so, never shown as running)."""
    rows = []
    now = time.time()
    for run_id, record in sorted(_read_json(_runs_path(), {}).items(),
                                 key=lambda kv: float((kv[1] or {}).get("queued") or 0),
                                 reverse=True):
        if not isinstance(record, dict):
            continue
        if record.get("ended"):
            state = "ended"
        elif _pid_alive(record.get("pid")):
            state = "running" if record.get("started") else "waiting-for-lane"
        else:
            state = "died-unrecorded"
        since = record.get("started") or record.get("queued") or now
        rows.append({"run": run_id, "state": state, **record,
                     "age_min": round((now - float(since)) / 60, 1)})
    return {"home": str(_home()), "disabled": _disabled() or None, "runs": rows}


# ---------------------------------------------------------------- board access (fail-soft)

def _base() -> str:
    return hub._base_url(None)


def _presence(base: str, run_id: str, focus: str) -> None:
    """Each unattended run is its own seat on the board: session = run id, focus = what it is
    doing right now. Headless sessions never open a console the board could otherwise see."""
    try:
        hub._post(base, "presence", {"agent": _agent()}, extra_headers={
            "X-Hub-Machine": _machine(), "X-Hub-Session": run_id, "X-Hub-Focus": focus[:200],
            "X-Hub-Cwd": _workspace()[:200]})
    except (RuntimeError, ValueError):
        pass


def _agent_error(base: str, code: str, message: str, details: str = "",
                 severity: str = "error") -> None:
    try:
        hub._post(base, "agent-error", {"agent": _agent(), "source": "responder", "code": code,
                                        "message": message[:800], "details": details[:2000],
                                        "severity": severity, "machine": _machine()})
    except (RuntimeError, ValueError):
        pass


def _post_update(base: str, run_id: str, item_id: str, outcome: str, summary: str) -> None:
    """The last-mile backstop: the launcher records EVERY terminal outcome on the board itself.
    A session that died mid-work, gave up, or cleared an item without a closing verb posts
    nothing — its work would be invisible. A duplicate line is far cheaper than a silent
    abandonment. Tagged ``automated`` so it is telemetry, never an item addressed to a person."""
    # The summary relays the session's own output, which routinely carries terminal colour
    # codes; the write seam refuses control characters, so neutralize them HERE or the one
    # note that makes an abandonment visible is itself refused.
    payload = {"agent": _agent(), "local": "responder-" + run_id.lower(),
               "title": textguard.neutralize("Responder %s: %s" % (outcome, item_id))[:200],
               "category": "context", "status": "standing",
               "body_md": textguard.neutralize(summary)[:4000],
               "tags": ["responder", "unattended", "automated", outcome]}
    if ":" in item_id and not item_id.startswith("error:"):
        payload["relates_to"] = [item_id]
    try:
        hub._post(base, "note", payload)
    except (RuntimeError, ValueError) as error:
        _log("update note for %s not recorded: %s" % (run_id, str(error)[:200]))


def _claim_item(base: str, item_id: str, release: bool = False) -> str:
    """ONE MACHINE PER ITEM, fleet-wide: ``granted`` | ``held:<machine>`` | ``unreachable``.

    The lane lock serialises THIS machine; a question or an unclaimed error is visible to every
    machine's poll at once, so without a hub claim two computers each spend a session fixing the
    same thing. Taken with the lane held and the item confirmed live -- the last moment before a
    session is spent -- and given back when the run ends. A task is not claimed here: its fenced
    lease (the session's `start`) already is the claim."""
    try:
        hub._post(base, "item-claim", {"agent": _agent(), "item": item_id, "machine": _machine(),
                                       **({"release": True} if release else {})})
        return "released" if release else "granted"
    except RuntimeError as error:
        text = str(error)
        if '"status": 409' in text or "claimed_elsewhere" in text:
            try:
                holder = ((json.loads(text).get("response") or {}).get("data") or {}).get("holder")
            except (ValueError, AttributeError):
                holder = None
            return "held:%s" % (holder or "another machine")
        return "unreachable"
    except ValueError:
        return "unreachable"


def _adhoc_run_id(item_id: str) -> str:
    """An id for an exit that never queued a run, so its note is still one addressable line."""
    return "exit-%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), hashlib.sha1(
        ("%s|%s" % (item_id, time.time())).encode("utf-8")).hexdigest()[:6])


def _early_exit(base: str, run_id: str, item_id: str, outcome: str, why: str) -> None:
    """A run that ends before its session starts is still a terminal outcome: say so."""
    _post_update(base, run_id, item_id, outcome,
                 "Unattended run `%s` for %s ended **%s** before a session started: %s\n"
                 % (run_id, item_id, outcome, why))
    _presence(base, run_id, "responder %s: %s" % (outcome, item_id))


# ---------------------------------------------------------------- what is work

def _errors(base: str, include_all: bool = False) -> list:
    payload = hub._get(base, "errors.json" + ("?include=all" if include_all else ""))
    rows = payload.get("data") or []
    return [r for r in rows if isinstance(r, dict)]


def _fold_errors(rows: list) -> dict:
    """Newest row per fingerprint — one signature is one problem, however many rows it left."""
    out: dict = {}
    for row in rows:
        fp = str(row.get("fingerprint") or "")
        if fp and (fp not in out or float(row.get("epoch") or 0) > float(out[fp].get("epoch") or 0)):
            out[fp] = row
    return out


def _own_lane_fault(row: dict) -> bool:
    """The responder's own reports (lane faults, environment checks, a broken canary) are for a
    person. A responder woken to fix the lane it runs on is a loop."""
    return str(row.get("source") or "").endswith(".responder")


def _looped(text: str) -> bool:
    return LOOP_STAMP in str(text or "")


def _review_gate(entry: dict) -> bool:
    """A review is a HUMAN gate: only a person may answer it, so no unattended session is ever
    launched for one, however it reached the inbox."""
    return (bool(entry.get("review"))
            or "review" in [str(t).lower() for t in (entry.get("tags") or [])]
            or str(entry.get("title") or "").lstrip().lower().startswith("review gate:"))


def _unattended(task: dict) -> bool:
    caps = ((task.get("routing") or {}).get("required_capabilities") or [])
    return UNATTENDED_CAPABILITY in [str(c).lower() for c in caps]


#: A task whose newest checkpoint is a PUSH younger than this, with no deployed checkpoint after
#: it, is held: its pipeline may still be running, and the verified deploy closes it
#: (hub_core.task_completion). Relaunching it re-does work that is already on its way out.
PUSH_HOLD_S = 6 * 3600


def _awaiting_deploy(task: dict, now: float) -> bool:
    import datetime as _dt
    rows = [s for s in (task.get("plan") or []) if isinstance(s, dict) and s.get("done")]
    if not rows:
        return False
    last = max(rows, key=lambda s: str(s.get("note_at") or ""))
    if not last.get("sha") or last.get("kind") == "deployed":
        return False
    try:
        at = _dt.datetime.fromisoformat(str(last.get("note_at") or "").replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False
    return 0 <= now - at.timestamp() < PUSH_HOLD_S


def _attention_is_ours(item) -> bool:
    """A needs-attention item this machine's responder may take: the hub says an AGENT can
    clear it (hub_core.attention.PERSON_ONLY names the rest), and one that only the machine it
    names can clear is taken only there."""
    if not isinstance(item, dict) or item.get("kind") != "attention" or not item.get("id"):
        return False
    if item.get("actor") != "agent":
        return False
    if item.get("on_its_machine"):
        return bool(item.get("machine")) and str(item["machine"]).strip().lower() == _machine().lower()
    return True


def find_work(base: str) -> dict:
    """Categorise the board for this responder. Read-only. ``items`` are the ids a responder
    may be launched for; ``surfaced`` counts what is visible and deliberately NOT worked."""
    items, reasons, surfaced = [], [], {}
    # include=synthetic: the canary is addressed to machines, so only this reader is handed it.
    inbox = (hub._get(base, "inbox.json?include=synthetic&agent=" + quote(_agent())).get("data")
             or {})
    asks = [i for i in (inbox.get("items") or []) if isinstance(i, dict)
            and i.get("kind") == "question"]
    gates = [i for i in asks if _review_gate(i)]
    if gates:
        surfaced["review_gates_left_for_a_person"] = len(gates)
    asks = [i for i in asks if not _review_gate(i)]
    workable = [i for i in asks if not _looped("%s %s" % (i.get("title"), i.get("body")))]
    if len(asks) != len(workable):
        surfaced["asks_from_unattended_sessions"] = len(asks) - len(workable)
    if workable:
        reasons.append("%d question(s) addressed to %s" % (len(workable), _agent()))
        items += [str(i["id"]) for i in workable]

    now = time.time()
    fresh, stale = [], []
    for fp, row in _fold_errors(_errors(base)).items():
        if row.get("acked") or _own_lane_fault(row):
            continue
        (fresh if now - float(row.get("epoch") or 0) < FRESH_S else stale).append(fp)
    if fresh:
        reasons.append("%d fresh unclaimed error signature(s)" % len(fresh))
        items += ["error:" + fp for fp in fresh]
    if stale:
        surfaced["stale_unclaimed_errors_left_for_a_person"] = len(stale)

    # NEEDS-ATTENTION IS WORK. An item the hub says an AGENT can clear (actor "agent") is taken
    # like an ask; one that only the machine it names can clear is taken only there. Person-only
    # conditions are left on the card.
    mine = [i for i in (inbox.get("items") or []) if _attention_is_ours(i)]
    if mine:
        reasons.append("%d needs-attention item(s) an agent can clear" % len(mine))
        items += [str(i["id"]) for i in mine]
    left = sum(1 for i in (inbox.get("items") or [])
               if isinstance(i, dict) and i.get("kind") == "attention" and not _attention_is_ours(i))
    if left:
        surfaced["attention_left_for_a_person"] = left

    tasks = hub._get(base, "task.json").get("data") or []
    held = [t for t in tasks if isinstance(t, dict) and t.get("status") == "todo"
            and _unattended(t) and _awaiting_deploy(t, now)]
    if held:
        surfaced["tasks_held_for_their_pushed_deploy"] = len(held)
    marked = [t for t in tasks if isinstance(t, dict) and t.get("status") == "todo"
              and _unattended(t) and not _looped("%s %s" % (t.get("title"), t.get("acceptance")))
              and not _awaiting_deploy(t, now)]
    if marked:
        reasons.append("%d task(s) marked for the unattended lane" % len(marked))
        items += [str(t["id"]) for t in marked]
    # Visible, never a wake reason: work in progress that may be rotting, and the ready queue.
    surfaced["in_progress_tasks_never_woken_for"] = sum(
        1 for t in tasks if isinstance(t, dict) and t.get("status") == "in_progress")
    surfaced["ready_tasks_not_ours"] = sum(
        1 for t in tasks if isinstance(t, dict) and t.get("status") == "todo" and not _unattended(t))
    return {"items": items, "reasons": reasons, "surfaced": surfaced,
            "signature": hashlib.sha256("|".join(sorted(items)).encode()).hexdigest()[:16]}


def resolve_item(base: str, item_id: str) -> dict | None:
    """The CURRENT item, live from the board — never trusted from the spawn argument. None means
    it is no longer offered here (someone else took it, it was cleared, or it was never ours):
    the cheap happy path. Raises RuntimeError when the board cannot be read, so the caller can
    tell 'not ours' from 'cannot tell' — the second is never a reason to act."""
    if item_id.startswith("error:"):
        fp = item_id[len("error:"):]
        row = _fold_errors(_errors(base, include_all=True)).get(fp)
        if not row or row.get("acked") or _own_lane_fault(row):
            return None
        return {"kind": "error", "id": item_id, "fingerprint": fp,
                "title": str(row.get("message") or "")[:300], "from": row.get("source") or "",
                "body": json.dumps({k: row.get(k) for k in (
                    "fingerprint", "severity", "source", "code", "message", "details",
                    "context", "ts", "occurrences_since_last")}, default=str)[:3800]}
    if ":task:" in item_id:
        try:
            task = hub._get(base, "task/%s.json" % item_id.rsplit(":", 1)[-1]).get("data") or {}
        except RuntimeError as error:
            if '"status": 404' in str(error):
                return None
            raise
        if task.get("status") != "todo" or not _unattended(task):
            return None
        if _awaiting_deploy(task, time.time()):
            return None                   # its push is on its way out; the deploy closes it
        if _looped("%s %s" % (task.get("title"), task.get("acceptance"))):
            return None
        plan = "\n".join("- [%s] %s%s" % ("x" if p.get("done") else " ", p.get("step") or "",
                                          (" -- " + p["note"]) if p.get("note") else "")
                         for p in (task.get("plan") or []) if isinstance(p, dict))
        return {"kind": "task", "id": item_id, "title": str(task.get("title") or "")[:300],
                "from": str((task.get("provenance") or {}).get("agent") or ""),
                "body": ("priority: %s\nacceptance (definition of done): %s\n%s" % (
                    task.get("priority") or "", task.get("acceptance") or "",
                    ("plan so far:\n" + plan) if plan else ""))[:3800]}
    inbox = (hub._get(base, "inbox.json?include=synthetic&agent=" + quote(_agent())).get("data")
             or {})
    for entry in inbox.get("items") or []:
        if isinstance(entry, dict) and str(entry.get("id")) == item_id and _attention_is_ours(entry):
            return {"kind": "attention", "id": item_id, "title": str(entry.get("title") or ""),
                    "from": entry.get("from") or "", "body": str(entry.get("body") or "")[:3800]}
        if isinstance(entry, dict) and str(entry.get("id")) == item_id \
                and entry.get("kind") == "question":
            if _review_gate(entry) or _looped("%s %s" % (entry.get("title"), entry.get("body"))):
                return None
            return {"kind": "question", "id": item_id, "title": str(entry.get("title") or ""),
                    "from": entry.get("from") or "", "body": str(entry.get("body") or "")[:3800]}
    return None


def verify_outcome(base: str, item: dict, killed: bool) -> str:
    """What the run did, read from the BOARD — never from the exit code or the session's
    summary. An unreadable board degrades to not-cleared (a retry under the attempt cap), never
    to a false success."""
    try:
        if item["kind"] == "task":
            task = hub._get(base, "task/%s.json" % item["id"].rsplit(":", 1)[-1]).get("data") or {}
            outcome = {"done": "cleared", "in_progress": "left-active"}.get(
                task.get("status"), "not-cleared")
        elif item["kind"] == "error":
            row = _fold_errors(_errors(base, include_all=True)).get(item["fingerprint"]) or {}
            mark = row.get("acked") or {}
            outcome = ("cleared" if str(mark.get("note") or "").lower().startswith("resolved")
                       else "claimed-not-resolved" if mark else "not-cleared")
        else:
            outcome = "cleared" if resolve_item(base, item["id"]) is None else "not-cleared"
    except RuntimeError:
        outcome = "unverified"
    if killed and outcome in ("not-cleared", "unverified"):
        return "timed-out"
    return outcome


# ---------------------------------------------------------------- lanes (single-flight)

def _lock_path(lane: str) -> Path:
    return _home() / ("lane-%s.lock" % lane)


def _acquire(lane: str) -> bool:
    path = _lock_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        holder = _read_json(path, {})
        age = time.time() - path.stat().st_mtime
        if age > LOCK_STALE_S or not _pid_alive(holder.get("pid")):
            path.unlink()                # a dead holder's lock: nobody has beaten it
    except (OSError, ValueError):
        pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
    return True


def _beat(lane: str) -> None:
    try:
        os.utime(_lock_path(lane), None)
    except OSError:
        pass


def _release(lane: str) -> None:
    try:
        if _read_json(_lock_path(lane), {}).get("pid") == os.getpid():
            _lock_path(lane).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- the session

CHARTER = """You are a ONE-OFF UNATTENDED RESPONDER answering for **{AGENT}** on **{MACHINE}**, spawned
because exactly one item landed. That item is your entire scope: do not read the queue, do not
pick up other work, do not start anything that outlives this session.

YOUR CLOCK: this session is killed {MINUTES} minutes after launch, hard, mid-command, with no
warning, and nothing you have not recorded on the board by then survives. Work back from it. By
minute {SHIP_BY}, whatever state you are in: commit and push (a WIP branch if it is not
shippable), record where it stands on the board, and exit. Never start a command that can run
longer than the minutes you have left.

HARD LIMITS (not yours to reinterpret):
- Never a destructive or irreversible operation: no DROP/DELETE/TRUNCATE, no migration to zero,
  no force-push, no recursive delete, no mass write to a system of record. If the fix needs one,
  escalate instead.
- Never send a message to a real person and never arm anything's outbound communications.
- Ship only through the project's normal path (its protected main / its deploy pipeline). Never
  bypass or weaken a check to make something land. Judge the DEPLOYED result.
- Verify empirically before concluding: run the real operation the change affects and read what
  it printed. Add no permanent tests; a probe is justified only at a critical boundary, runs once,
  and is deleted before commit.

Act through the board client: {CLIENT} <verb> (`search` first — the board may already know).

FINISH THE SHIP CYCLE — that is the whole job. A diagnosis nobody had time to apply is a bug
report, not a fix. When the fix is code:
  1. Stage YOUR OWN files by path (`git add <path>`), never `git add -A`: another session may be
     mid-edit in the same repository.
  2. Commit with a message that says what and why, then push through the normal path.
  3. A PUSH IS NOT A SHIP UNTIL ITS PIPELINE IS GREEN. Never finish a task, and never answer
     "shipped", on a commit whose deploy has not verified. If it is close, wait and then look at
     the deployed result. If it is not, DO NOT SIT ON IT — a wait is not free, you are holding a
     lane somebody else is queued for: `step` the task with "sha=<sha> pipeline=<id>" and what is
     left, leave it in progress, and exit.

A PUSH REFUSED FOR AUTH IS NOT AN ENDING. If `git push` is refused for authentication, commit on
top of the current branch and run `{CLIENT} handoff <task>`: a machine that CAN push replays it
(never forced) and the pushed sha lands on the task. `step` the task with the hand-off id and exit.
Never stop at a patch file, never hunt for other credentials, never rewrite the remote.

NEVER LEAVE UNCOMMITTED WORK. Short on time or unable to finish: put it on a branch
(`git checkout -b responder-wip/<slug>`, add YOUR files, commit "WIP: <item> - <what is left>",
push the branch), record the branch on the board, and escalate with `ask`. The launcher checks
for uncommitted files when you exit and names them on the board; a pushed WIP branch is
recoverable, a dirty tree somebody has to hunt for is not.

CLOSE THE LOOP, by kind:
- task     -> `start <id>` FIRST — that is the claim; if it is refused because somebody holds it,
              stand down and exit. Work to its acceptance text at full scope, ship, and only after
              the deploy verifies: `finish <id> --accept-note "<what changed, for the requester>"
              --evidence <sha|url>`.
- question -> `answer <id> --text "<the answer>"` (delivered to the asker in about a second).
              NEVER answer a review gate (tagged `review`, titled "Review gate:"): only a
              person may. If one reached you, touch nothing and exit. And never answer an ask
              whose only remedy is a PERSON'S action (hardware or console access, a host
              administration step, a reserved approval, a credential only they hold):
              answering retires it from their inbox, so the one signal that reaches them is
              gone. Leave it open, record what you measured, and name the person action in
              your final message.
- error    -> `ack-error <fingerprint> --note "claimed: <what you are checking>"` FIRST, so every
              other console sees it is in flight; fix the cause and ship; then
              `ack-error <fingerprint> --note "resolved: <root cause> evidence <sha|url>"`.
- attention-> a condition the hub computed from current state. Do what its `Fix:` line says (a
              task to verify on the deployed result and finish, a lease to reclaim or hand
              back, a client to update). It clears ITSELF the moment the fact goes; there is
              nothing to acknowledge. If the fix needs a person after all, escalate with `ask`.
If you cannot resolve it with confidence, escalate with `ask` (the client stamps it so no other
responder picks it up) and exit.

End your final message with: RESPONDER-SUMMARY finished=N answered=N resolved=N escalated=N
"""


def _charter(item: dict, bound_s: int) -> str:
    minutes = max(5, bound_s // 60)
    text = (CHARTER.replace("{AGENT}", _agent()).replace("{MACHINE}", _machine())
            .replace("{MINUTES}", str(minutes))
            .replace("{SHIP_BY}", str(max(3, minutes - SHIP_BY_MARGIN_MIN)))
            .replace("{CLIENT}", "python -m hub_core.client"))
    return text + "\nTHE ITEM (%s):\nid: %s\nfrom: %s\ntitle: %s\n%s\n" % (
        item["kind"], item["id"], item.get("from") or "?", item.get("title") or "",
        item.get("body") or "")


def _runtime_argv(prompt: str, run_id: str) -> list:
    raw = (os.environ.get("HUB_RESPONDER_RUNTIME") or "").strip()
    if not raw:
        raise ValueError("set HUB_RESPONDER_RUNTIME to the session command, e.g. "
                         "'agent-cli --print {prompt}' or a JSON argv list")
    argv = json.loads(raw) if raw.startswith("[") else shlex.split(raw, posix=True)
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("HUB_RESPONDER_RUNTIME must name a command")
    if any("{prompt_file}" in a for a in argv):
        path = _home() / "prompts" / (run_id + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        argv = [a.replace("{prompt_file}", str(path)) for a in argv]
    elif any("{prompt}" in a for a in argv):
        argv = [a.replace("{prompt}", prompt) for a in argv]
    else:
        argv = argv + [prompt]
    resolved = shutil.which(argv[0])
    if not resolved:
        raise ValueError("the session command %r is not on PATH" % argv[0])
    return [resolved] + argv[1:]


def _session_env(run: dict) -> dict:
    env = dict(os.environ)
    env["HUB_UNATTENDED"] = "1"            # marks the session; the client stamps its asks
    env["GIT_TERMINAL_PROMPT"] = "0"       # git never asks on a terminal nobody is watching
    env["GCM_INTERACTIVE"] = "never"       # a credential manager never shows a dialog
    env["HUB_RUN_ID"] = run["id"]
    env["HUB_RUN_KIND"] = run["kind"]
    env["HUB_RUN_ITEM"] = run["item"][:120]
    env["HUB_RUN_TITLE"] = run["title"][:160]
    env["HUB_SESSION_ID"] = run["id"]
    env.setdefault("HUB_MACHINE", _machine())
    env["HUB_AGENT_ID"] = _agent()
    return env


def _kill_tree(proc) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           timeout=30, creationflags=_NO_WINDOW)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if os.name != "nt":
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
    except (OSError, AttributeError):
        pass


def _reap(proc) -> str:
    """Every exit route ends here: sweep the tree (a returned parent is not a dead tree) and
    PROVE the pid is gone. 'I killed it' that nobody verified is the claim that was wrong."""
    if proc.poll() is None or os.name == "nt":
        _kill_tree(proc)
    for _ in range(10):
        if proc.poll() is not None and not _pid_alive(proc.pid):
            return "reaped"
        time.sleep(0.5)
    return "SURVIVED"


def run_session(argv: list, cwd: str, env: dict, bound_s: int, lane: str, run_id: str):
    """Run one session bounded; returns (rc or None when killed at the clock, output tail, reap)."""
    kwargs: dict = {}
    if os.name == "nt":
        # Its OWN console, HIDDEN — not CREATE_NO_WINDOW, which leaves the runtime console-less
        # so every console tool it spawns pops a fresh visible window on the owner's desktop.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0                                   # SW_HIDE
        kwargs.update(startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE
                      | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", **kwargs)
    _run_update(run_id, pid=proc.pid)
    deadline = time.time() + bound_s
    rc, out = None, ""
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=min(LOCK_BEAT_S, max(1, deadline - time.time())))
                rc = proc.returncode
                break
            except subprocess.TimeoutExpired:
                if time.time() >= deadline:
                    _kill_tree(proc)
                    try:
                        out, _ = proc.communicate(timeout=15)
                    except (subprocess.SubprocessError, OSError):
                        out = ""
                    rc = None
                    break
                _beat(lane)
    finally:
        reaped = _reap(proc)
        if reaped != "reaped":
            _log("WARNING: session pid %d %s after its run ended" % (proc.pid, reaped))
    return rc, (out or "")[-4000:], reaped


def dirty_snapshot(workspace: str) -> dict:
    """{repo: {path: fingerprint}} of every uncommitted file under the workspace, where the
    fingerprint is the porcelain status plus a digest of the file's bytes. Taken BEFORE a
    session and again after it, so the launcher names only what THIS session changed — a file
    an earlier run left dirty is not blamed on every later one. Read-only."""
    root = Path(workspace)
    if not root.is_dir():
        return {}
    candidates = [root] if (root / ".git").exists() else [p for p in sorted(root.iterdir())
                                                          if (p / ".git").exists()]
    snap: dict = {}
    for repo in candidates[:40]:
        try:
            out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                                  "--untracked-files=all"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=_NO_WINDOW).stdout or ""
        except (OSError, subprocess.SubprocessError):
            continue
        files: dict = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip().strip('"')
            try:
                target = repo / path
                stat = target.stat()
                digest = (hashlib.sha256(target.read_bytes()).hexdigest()[:16]
                          if stat.st_size <= 8 * 1024 * 1024
                          else "%d@%d" % (stat.st_size, stat.st_mtime_ns))
            except OSError:
                digest = "-"                                   # deleted, or a directory
            files[path] = line[:2] + ":" + digest
        snap[repo.name] = files
    return snap


def uncommitted(workspace: str, before: dict | None = None) -> str:
    """'repo: N files (a, b, c)' for uncommitted work THIS session left under the workspace, or
    ''. ``before`` is the :func:`dirty_snapshot` taken at launch; a file that was already dirty
    counts only if the session changed it further. Read-only — the launcher NEVER commits: only
    the session knew which files were its own."""
    before = before or {}
    parts = []
    for repo, now in dirty_snapshot(workspace).items():
        earlier = before.get(repo) or {}
        files = [path for path, mark in now.items() if earlier.get(path) != mark]
        if files:
            parts.append("%s: %d file%s (%s%s)" % (repo, len(files), "" if len(files) == 1
                                                    else "s", ", ".join(files[:6]),
                                                    ", ..." if len(files) > 6 else ""))
        if len(parts) >= 5:
            break
    return "; ".join(parts)


# ---------------------------------------------------------------- respond: one item, then gone

def respond(item_id: str) -> int:
    off = _disabled()
    if off:
        _log("respond %s: not running (%s)" % (item_id, off))
        return 0
    base = _base()
    responses_path = _home() / "responses.json"
    responses = _read_json(responses_path, {})
    record = responses.get(item_id) or {}
    if record.get("state") == "running" and not _pid_alive(record.get("pid")):
        record["attempts"] = max(0, int(record.get("attempts") or 1) - 1)   # a dead pass: refund
    if int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
        _log("respond %s: attempted %d times already; left for a person"
             % (item_id, int(record["attempts"])))
        _early_exit(base, _adhoc_run_id(item_id), item_id, "exhausted",
                    "attempted %d times already; left for a person, no session was spent."
                    % int(record["attempts"]))
        return 0
    try:
        item = resolve_item(base, item_id)
    except RuntimeError as error:
        _log("respond %s: cannot confirm it is still ours (%s); leaving it pending"
             % (item_id, str(error)[:160]))
        _early_exit(base, _adhoc_run_id(item_id), item_id, "unverified",
                    "the board could not be read (%s), so nothing was launched: 'cannot tell' "
                    "is never a reason to act." % str(error)[:160])
        return 0
    if item is None:
        _log("respond %s: no longer offered here; standing down" % item_id)
        _early_exit(base, _adhoc_run_id(item_id), item_id, "stood-down",
                    "not offered to an unattended session (taken, cleared, never ours, or a "
                    "review gate left for a person); no session was spent.")
        return 0

    kind, lane, bound = item["kind"], _lane(item["kind"]), _bound(item["kind"])
    run_id = _run_queued(kind, item_id, item.get("title") or "", bound,
                         int(record.get("attempts") or 0) + 1)
    _presence(base, run_id, "responder waiting for the %s lane: %s" % (lane, item_id))
    wait_until, recheck_at = time.time() + 2 * bound, time.time() + QUEUE_RECHECK_S
    while not _acquire(lane):
        if _disabled():
            _run_finished(run_id, "disabled")
            return 0
        if time.time() >= recheck_at:            # a wait must not outlive its reason
            recheck_at = time.time() + QUEUE_RECHECK_S
            try:
                if resolve_item(base, item_id) is None:
                    _log("respond %s: taken or cleared while queued; standing down" % item_id)
                    _run_finished(run_id, "stood-down")
                    _early_exit(base, run_id, item_id, "stood-down",
                                "taken or cleared elsewhere while this run waited for the "
                                "%s lane; no session was spent." % lane)
                    return 0
            except RuntimeError:
                pass
        if time.time() >= wait_until:
            _run_finished(run_id, "lane-full")
            _early_exit(base, run_id, item_id, "lane-full",
                        "the %s lane stayed busy for %dm; no session was spent. The next poll "
                        "may offer it again." % (lane, 2 * bound // 60))
            return 0
        time.sleep(5)
    try:
        # The wait may have been long: re-read with the lane HELD, the last moment before a
        # session is spent, so a stale item is a cheap exit.
        try:
            item = resolve_item(base, item_id)
        except RuntimeError as error:
            _run_finished(run_id, "unverified")
            _early_exit(base, run_id, item_id, "unverified",
                        "the board could not be read with the lane held (%s), so nothing was "
                        "launched: 'cannot tell' is never a reason to act." % str(error)[:160])
            return 0
        if item is None or _disabled():
            _run_finished(run_id, "stood-down" if item is None else "disabled")
            if item is None:
                _early_exit(base, run_id, item_id, "stood-down",
                            "taken or cleared elsewhere before launch; no session was spent.")
            return 0
        claimed = kind != "task"
        if claimed:
            verdict = _claim_item(base, item_id)
            if verdict.startswith("held:"):
                _run_finished(run_id, "stood-down")
                _log("respond %s: being worked by %s; standing down (nothing charged)"
                     % (item_id, verdict[5:]))
                _early_exit(base, run_id, item_id, "stood-down",
                            "claimed by %s, which is working it; no session was spent."
                            % verdict[5:])
                return 0
            if verdict == "unreachable":
                _log("respond %s: the item-claim route is unreachable; proceeding WITHOUT a "
                     "claim (a rare double-fix beats a problem nobody fixes)" % item_id)
        prompt = _charter(item, bound)
        try:
            argv = _runtime_argv(prompt, run_id)
        except ValueError as error:
            if claimed:
                _claim_item(base, item_id, release=True)
            _run_finished(run_id, "runtime-unavailable")
            _agent_error(base, "responder_runtime_unavailable", str(error))
            _log("respond %s: %s" % (item_id, error))
            return 1
        responses = _read_json(responses_path, {})
        attempts = int((responses.get(item_id) or {}).get("attempts") or 0) + 1
        responses[item_id] = {"state": "running", "attempts": attempts, "at": time.time(),
                              "pid": os.getpid(), "run": run_id}
        _write_json(responses_path, responses)
        _run_update(run_id, started=time.time())
        _presence(base, run_id, "responder running (%dm clock): %s" % (bound // 60, item_id))
        _log("respond %s: launching (%s lane, bounded %ds, attempt %d/%d)"
             % (item_id, lane, bound, attempts, MAX_ATTEMPTS))
        session = {"id": run_id, "kind": kind, "item": item_id, "title": item.get("title") or ""}
        dirty_before = dirty_snapshot(_workspace())     # so only THIS session's leftovers are named
        started = time.time()
        try:
            rc, tail, reaped = run_session(argv, _workspace(), _session_env(session), bound,
                                           lane, run_id)
        except OSError as error:
            if claimed:
                _claim_item(base, item_id, release=True)
            _run_finished(run_id, "failed")
            _agent_error(base, "responder_launch_failed", "the session command could not start",
                         str(error))
            return 1
    finally:
        _release(lane)

    if kind != "task":
        _claim_item(base, item_id, release=True)     # given back the moment the run ends
    outcome = verify_outcome(base, item, killed=rc is None)
    dirty = uncommitted(_workspace(), dirty_before)
    responses = _read_json(responses_path, {})
    entry = responses.get(item_id) or {"attempts": 1}
    entry.update({"state": "done" if outcome == "cleared" else outcome, "at": time.time(),
                  "rc": rc, "run": run_id})
    entry.pop("pid", None)
    responses[item_id] = entry
    _write_json(responses_path, responses)
    duration = int(time.time() - started)
    _run_finished(run_id, outcome, rc, duration_s=duration, reaped=reaped,
                  uncommitted=dirty or None, said=tail[-400:])
    summary = ("Unattended %s run `%s` for %s ended **%s** after %dm (rc=%s, attempt %d/%d, "
               "session %s).\n\n" % (kind, run_id, item_id, outcome, duration // 60, rc,
                                     int(entry.get("attempts") or 1), MAX_ATTEMPTS, reaped))
    if dirty:
        summary += ("**Left uncommitted work** (the launcher never commits it): %s\n\n" % dirty)
    if outcome != "cleared":
        summary += ("Not cleared. %s\n\n" % (
            "It stays on the board for a person: the attempt budget is spent."
            if int(entry.get("attempts") or 1) >= MAX_ATTEMPTS else
            "The next poll may offer it once more."))
    summary += "Session's last words:\n\n```\n%s\n```\n" % (tail[-1200:].strip() or "(none)")
    _post_update(base, run_id, item_id, outcome, summary)
    _presence(base, run_id, "responder %s: %s" % (outcome, item_id))
    _log("respond %s -> %s (%ds)%s" % (item_id, outcome, duration,
                                       ("; uncommitted: " + dirty) if dirty else ""))
    return 0


# ---------------------------------------------------------------- poll: the scheduled trigger

def _spawn_respond(item_id: str) -> int:
    """Start `respond <item>` detached and do not wait: the poll stays cheap and each item gets
    its own short-lived launcher that owns its own tree."""
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"]
                                        if env.get("PYTHONPATH") else "")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP  # DETACHED
                                   | _NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("pythonw.exe"):
        python = python[:-len("pythonw.exe")] + "python.exe"
    proc = subprocess.Popen([python, "-m", "hub_core.responder", "respond", item_id],
                            cwd=_workspace(), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return proc.pid


def poll(dry_run: bool = False) -> dict:
    off = _disabled()
    if off:
        return {"launched": [], "why": off}
    base = _base()
    state_path = _home() / "state.json"
    state = _read_json(state_path, {})
    report: dict = {}
    if not dry_run and time.time() - float(state.get("env_checked") or 0) > ENV_CHECK_EVERY_S:
        report["check_env"] = check_env(report_to_board=True)["summary"]
        state["env_checked"] = time.time()
    try:
        work = find_work(base)
    except RuntimeError as error:
        # Unreachable is a connectivity fact, not an empty queue: say so and launch nothing.
        _write_json(state_path, state)
        return {**report, "launched": [], "why": "board unreachable: %s" % str(error)[:200]}
    responses = _read_json(_home() / "responses.json", {})
    launched, skipped = [], {}
    for item_id in work["items"]:
        record = responses.get(item_id) or {}
        if record.get("state") == "done":
            skipped[item_id] = "already cleared by an earlier run"
        elif int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            skipped[item_id] = "attempt budget spent; a person's now"
        elif record.get("state") == "running" and _pid_alive(record.get("pid")):
            skipped[item_id] = "a launcher is already on it"
        elif len(launched) >= MAX_SPAWN_PER_POLL:
            skipped[item_id] = "per-poll launch cap; next poll"
        else:
            launched.append(item_id)
    if not dry_run:
        for item_id in launched:
            pid = _spawn_respond(item_id)
            _log("poll: spawned launcher pid %d for %s" % (pid, item_id))
        if os.environ.get("HUB_RESPONDER_CANARY") == "1":
            report["canary"] = canary()
    _write_json(state_path, state)
    return {**report, "reasons": work["reasons"], "surfaced": work["surfaced"],
            "launched" if not dry_run else "would_launch": launched, "skipped": skipped,
            "dry_run": dry_run}


# ---------------------------------------------------------------- canary: prove the loop

def canary() -> dict:
    """File a synthetic question through the REAL ask path once a day, then judge whether the
    loop answered it — honestly. Three rules keep the verdict true:

    * UNREACHABLE IS NOT A VERDICT. If the board cannot be read this pass, nothing is concluded:
      no green, no alarm. A network blip must never page anyone as 'the loop is broken'.
    * ONE EMPTY READ IS NOT PROOF. A canary absent from a reachable read is either retired
      because it was answered, or a transient empty/filtered read. Only several CONSECUTIVE
      absent reads count as retired; a single one proves nothing.
    * ALARM ONLY WHEN WE KNOW. The alarm fires only on a reachable read that SHOWS the canary
      still unanswered past its deadline — once per canary, never every pass.
    """
    base = _base()
    state_path = _home() / "state.json"
    state = _read_json(state_path, {})
    now = time.time()
    outstanding = state.get("canary_id")
    if outstanding:
        filed_at = float(state.get("canary_filed_at") or now)
        try:
            rows = hub._get(base, "questions.json").get("data") or []
        except RuntimeError as error:
            return {"verdict": "unreachable-no-verdict", "canary": outstanding,
                    "detail": str(error)[:160]}
        found = next((r for r in rows if isinstance(r, dict) and r.get("id") == outstanding), None)
        if found is None:
            streak = int(state.get("canary_absent_streak") or 0) + 1
            if streak < CANARY_ABSENT_READS:
                state["canary_absent_streak"] = streak
                _write_json(state_path, state)
                return {"verdict": "absent-once-no-verdict", "canary": outstanding,
                        "absent_reads": streak, "needed": CANARY_ABSENT_READS}
            state.update(canary_id=None, canary_absent_streak=0, canary_last_ok_at=now,
                         canary_latency_s=int(now - filed_at))
            _write_json(state_path, state)
            return {"verdict": "retired", "canary": outstanding, "latency_s": int(now - filed_at)}
        state["canary_absent_streak"] = 0
        if found.get("answered"):
            state.update(canary_id=None, canary_last_ok_at=now, canary_latency_s=int(now - filed_at))
            _write_json(state_path, state)
            return {"verdict": "answered", "canary": outstanding, "latency_s": int(now - filed_at)}
        if now - filed_at > CANARY_OVERDUE_S:
            minutes = int((now - filed_at) // 60)
            _agent_error(base, "responder_loop_broken",
                         "responder canary unanswered for %d min: the ask -> responder -> answer "
                         "loop is not closing; check the scheduled poll, the kill switch and the "
                         "session runtime" % minutes, details=outstanding)
            state.update(canary_id=None, canary_filed_at=None)
            _write_json(state_path, state)
            return {"verdict": "alarm-raised", "canary": outstanding, "waited_min": minutes}
        _write_json(state_path, state)
        return {"verdict": "pending", "canary": outstanding, "waited_s": int(now - filed_at)}

    last = max(float(state.get("canary_last_ok_at") or 0), float(state.get("canary_filed_at") or 0))
    if now - last < CANARY_EVERY_S and not os.environ.get("HUB_RESPONDER_CANARY_NOW"):
        return {"verdict": "not-due", "next_in_s": int(CANARY_EVERY_S - (now - last))}
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        result = hub._post(base, "ask", {
            # Asked AS this identity: a scoped credential may only write as its own subject,
            # and the operator's inbox carries every open question whoever asked it.
            "agent": _agent(), "anyway": True,
            # A self-test FOR MACHINES: never raised to a person (their inbox, the notifier),
            # only to readers that ask for synthetic items -- this responder.
            "synthetic": True,
            "question": "RESPONDER CANARY %s: reply with the single word CONFIRMED." % stamp,
            "context": "Synthetic end-to-end probe of the unattended responder loop, filed once a "
                       "day. Answering it IS the probe passing; answer normally."})
    except (RuntimeError, ValueError) as error:
        return {"verdict": "file-failed-no-verdict", "detail": str(error)[:200]}
    qid = ((result.get("data") or {}).get("id") or (result.get("data") or {}).get("question")
           or "")
    state.update(canary_id=qid, canary_filed_at=now, canary_absent_streak=0)
    _write_json(state_path, state)
    return {"verdict": "filed", "canary": qid, "deadline_min": CANARY_OVERDUE_S // 60}


# ---------------------------------------------------------------- environment repair

def check_env(report_to_board: bool = False) -> dict:
    """The idempotent repair table this machine needs to run responders: one line per check,
    OK / FIXED / NEEDS A PERSON, never silent. What it can fix it fixes; every NEEDS A PERSON
    line, with --report, becomes an operational error on the board owned by this machine."""
    rows = []

    def row(check, verdict, detail):
        rows.append({"check": check, "verdict": verdict, "detail": detail})

    home = _home()
    if home.is_dir():
        row("state-home", "OK", str(home))
    else:
        try:
            home.mkdir(parents=True, exist_ok=True)
            row("state-home", "FIXED", "created %s" % home)
        except OSError as error:
            row("state-home", "NEEDS A PERSON", "cannot create %s: %s" % (home, error))
    base = None
    try:
        base = _base()
        who = hub._get(base, "whoami.json").get("data") or {}
        if who.get("mode"):
            row("credential", "OK", "%s (%s)" % (who.get("subject"), who.get("mode")))
        else:
            row("credential", "NEEDS A PERSON", "the hub does not accept this credential: %s"
                % (who.get("problem") or "no credential presented"))
        operator = str(who.get("operator") or "")
        row("identity", "OK" if operator == _agent() else "NOTE",
            "answering as %s; questions are addressed to %s" % (_agent(), operator or "?"))
    except (RuntimeError, ValueError) as error:
        row("board", "NEEDS A PERSON", str(error)[:200])
    raw = (os.environ.get("HUB_RESPONDER_RUNTIME") or "").strip()
    try:
        argv = json.loads(raw) if raw.startswith("[") else shlex.split(raw, posix=True)
        found = shutil.which(argv[0]) if argv else None
        row("runtime", "OK" if found else "NEEDS A PERSON",
            found or ("HUB_RESPONDER_RUNTIME is %s" % ("unset" if not raw else
                                                     "not on PATH: " + str(argv[0]))))
    except (ValueError, IndexError) as error:
        row("runtime", "NEEDS A PERSON", "HUB_RESPONDER_RUNTIME unparseable: %s" % error)
    row("git", "OK" if shutil.which("git") else "NEEDS A PERSON",
        "sessions run with GIT_TERMINAL_PROMPT=0 and GCM_INTERACTIVE=never"
        if shutil.which("git") else "git is not on PATH")
    if os.name == "nt":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        row("pythonw", "OK" if pyw.exists() else "NEEDS A PERSON",
            str(pyw) if pyw.exists() else "no pythonw.exe beside %s: the scheduled poll would "
                                          "show a console window" % sys.executable)
    row("kill-switch", "NOTE" if _disabled() else "OK", _disabled() or "armed")
    needs = [r for r in rows if r["verdict"] == "NEEDS A PERSON"]
    if report_to_board and base and needs:
        for r in needs:
            _agent_error(base, "env-" + r["check"], "responder environment on %s: %s needs a "
                         "person -- %s" % (_machine(), r["check"], r["detail"]))
    summary = "%d OK, %d FIXED, %d NEEDS A PERSON" % (
        sum(r["verdict"] == "OK" for r in rows), sum(r["verdict"] == "FIXED" for r in rows),
        len(needs))
    return {"machine": _machine(), "rows": rows, "summary": summary}


# ---------------------------------------------------------------- CLI

def load_env_file() -> list:
    """Settings for a scheduled run, which inherits no shell environment: ``<home>/responder.env``
    holds KEY=VALUE lines (only HUB_* keys; an already-set variable wins). The credential is
    never one of them — HUB_AGENT_TOKEN_FILE names a file only this user can read, so the token
    stays out of the task definition, the registry and every process argument."""
    loaded = []
    path = _home() / "responder.env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if not sep or line.lstrip().startswith("#") or not key.startswith("HUB_"):
            continue
        if key in ("HUB_AGENT_TOKEN", "HUB_WRITE_TOKEN"):
            continue                    # a token pasted into the settings file is refused
        if not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    token_file = os.environ.get("HUB_AGENT_TOKEN_FILE")
    if token_file and not os.environ.get("HUB_AGENT_TOKEN"):
        try:
            os.environ["HUB_AGENT_TOKEN"] = Path(token_file).read_text(encoding="utf-8-sig").strip()
            loaded.append("HUB_AGENT_TOKEN(from file)")
        except OSError:
            pass
    return loaded


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="One-item unattended responders over a Hub.")
    parser.add_argument("--home", help="state directory (default HUB_RESPONDER_HOME or "
                                       "~/.hub-responder); its responder.env is loaded")
    commands = parser.add_subparsers(dest="command", required=True)
    p_poll = commands.add_parser("poll", help="read the board once; launch per workable item")
    p_poll.add_argument("--dry-run", action="store_true")
    p_respond = commands.add_parser("respond", help="one bounded session for one item")
    p_respond.add_argument("item")
    commands.add_parser("status", help="the local run ledger")
    commands.add_parser("canary", help="file or judge the daily loop canary")
    p_env = commands.add_parser("check-env", help="environment repair table")
    p_env.add_argument("--report", action="store_true",
                       help="board every NEEDS A PERSON line as this machine's problem")
    args = parser.parse_args(argv)
    if args.home:
        os.environ["HUB_RESPONDER_HOME"] = args.home
    load_env_file()
    try:
        if args.command == "respond":
            return respond(args.item)
        if args.command == "poll":
            result = poll(dry_run=args.dry_run)
        elif args.command == "status":
            result = status()
        elif args.command == "canary":
            result = canary()
        else:
            result = check_env(report_to_board=args.report)
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
