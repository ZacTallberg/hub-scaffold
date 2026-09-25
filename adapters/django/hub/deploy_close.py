"""The deploy record's close, retried until it lands -- off every request thread.

``hub_write.deploy`` closes the tasks whose recorded commits a verified release made live
(hub_core.task_completion). That moment is not the only one a close can succeed at. The usual
refusal is a live lease -- the unattended run is still watching the same pipeline -- and it goes
away moments later when the run hands the task back. Without a retry the task then sits with
"deployed and verified" on it until a person notices. Three more entrances close that gap:

* the hand-back / release / lease-sweep that frees the lease kicks the close sweep;
* the board's own read path kicks it on a throttle (``SWEEP_INTERVAL_S``);
* ``POST /hub/api/deploy/reconcile`` replays the newest deploy record through the matcher, for
  tasks stranded before a matcher change.

NEVER ON THE REQUEST. A close claims a lease and appends to the ledger per task; on a board read
that is latency the reader pays for somebody else's close. Every entrance only starts ONE
single-flight daemon thread. The reconcile's state lives in a FILE (``deploy-reconcile.json``),
because a hub may run as more than one process and module memory in the one that started the
replay is invisible to the one answering the read; a ``running`` state whose file has not moved
for ``RECONCILE_DEAD_S`` belongs to a process that died (a deploy restarts the hub), and a replay
stops at ``RECONCILE_BUDGET_S`` and says so.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from django.http import JsonResponse

from hub_core import atomic as _atomic
from hub_core import task_completion

from . import hub_app
from .hub_write import writer

log = logging.getLogger("hub.deploy_close")

SWEEP_INTERVAL_S = 300
SWEEP_LIMIT = 10
RECONCILE_DEAD_S = 120
RECONCILE_BUDGET_S = 600
_LAST = {"at": 0.0}
_SWEEP_LOCK = threading.Lock()
_DEPLOY_REF = re.compile(r"\(([^()\s]+)\)\.?\s*$")


def _deploy_id_of(row: dict) -> str:
    """The deploy record a ``deployed`` checkpoint names (its note ends ``(<deploy id>).``)."""
    m = _DEPLOY_REF.search(str(row.get("note") or ""))
    return m.group(1) if m else ""


def close_if_deployed(tid: str) -> dict:
    """Close an unattended task that ALREADY carries a ``deployed`` checkpoint, now -- unless its
    own trail says the work is not finished (refused, with the quote, onto the task). Never
    raises."""
    try:
        task = (hub_app.current_state().get("entities") or {}).get(tid)
        if not task or task.get("type") != "task" or not task_completion.is_unattended(task):
            return {}
        if str(task.get("status") or "") not in task_completion.OPEN_STATES:
            return {}
        row = task_completion.deployed_row(task)
        if row is None:
            return {}
        commit = str(row.get("sha") or "")
        deploy_id = _deploy_id_of(row)
        deployed_sha = deploy_id.rsplit(":", 1)[-1] if deploy_id else commit
        return close_one(task, commit, deployed_sha, deploy_id or "deploy")
    except Exception:                                        # noqa: BLE001 - never break a caller
        log.warning("close_if_deployed failed on %s", tid, exc_info=True)
        return {}


def close_one(task: dict, commit: str, deployed_sha: str, deploy_id: str) -> dict:
    """The close itself, shared by the deploy record and every retry: refuse on the task's own
    word, else the hub's finish (refused while anyone else holds the lease). Every refusal is
    written onto the task; none is silent."""
    from . import hub_write
    tid = task["id"]
    unfinished = task_completion.unfinished_by_its_own_word(task)
    if unfinished:
        why = 'its own checkpoint says the work is not finished: "%s"' % unfinished[:150]
        hub_write._record_auto_close(tid, "refused", why, commit, deploy_id)
        return {"closed": False, "why": why}
    why = hub_write._hub_finish(tid, commit, deployed_sha, deploy_id)
    if why and why != "gone or already done":
        hub_write._record_auto_close(tid, "refused", why, commit, deploy_id)
        return {"closed": False, "why": why}
    return {"closed": not why, "why": ""}


def sweep(force: bool = False, now: float | None = None, limit: int = SWEEP_LIMIT) -> dict:
    """Retry the close of every unattended, not-done task already stepped ``deployed``."""
    now = time.time() if now is None else now
    if not force and now - _LAST["at"] < SWEEP_INTERVAL_S:
        return {"skipped": "throttled"}
    _LAST["at"] = now
    closed, refused = [], {}
    try:
        tasks = hub_app.current_state().get("by_type", {}).get("task", [])
    except Exception:                                        # noqa: BLE001
        return {"skipped": "unreadable"}
    for task in task_completion.retry_candidates(tasks):
        if len(closed) + len(refused) >= limit:
            break
        out = close_if_deployed(task["id"])
        if out.get("closed"):
            closed.append(task["id"])
        elif out.get("why"):
            refused[task["id"]] = out["why"]
    if closed or refused:
        log.info("deploy-close sweep: closed %s, refused %s", closed, refused)
    return {"closed": closed, "refused": refused}


def sweep_async(force: bool = False) -> bool:
    """Every entrance's call: never does the work on the calling thread. Checks the throttle
    (``force`` skips it -- a lease was just freed) and starts ONE daemon thread; a pass already
    running means this call does nothing. Returns whether it started one."""
    if not force and time.time() - _LAST["at"] < SWEEP_INTERVAL_S:
        return False
    if not _SWEEP_LOCK.acquire(blocking=False):
        return False

    def _run():
        try:
            sweep(force=True)
        except Exception:                                    # noqa: BLE001
            log.warning("deploy-close sweep failed", exc_info=True)
        finally:
            _SWEEP_LOCK.release()
    try:
        threading.Thread(target=_run, name="deploy-close-sweep", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        _SWEEP_LOCK.release()
        return False
    return True


# ---------------------------------------------------------------------------- reconcile

def _reconcile_path():
    return hub_app.HUB_DIR / "deploy-reconcile.json"


def reconcile_state() -> dict:
    try:
        import json
        value = json.loads(_reconcile_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"state": "idle"}
    except (OSError, ValueError):
        return {"state": "idle"}


def _reconcile_write(state: dict) -> None:
    try:
        _reconcile_path().parent.mkdir(parents=True, exist_ok=True)
        _atomic.write_json(_reconcile_path(), state)
    except Exception:                                        # noqa: BLE001
        log.warning("could not write the reconcile state", exc_info=True)


def reconcile(progress=None, budget_s: float = RECONCILE_BUDGET_S) -> dict:
    """Replay the newest deploy record through the close matcher, then retry every stepped
    close. Idempotent: a stepped task is only retried, never stepped twice."""
    from . import hub_write
    t0 = time.time()
    state = hub_app.current_state()
    deploys = state.get("by_type", {}).get("deploy", [])
    newest = task_completion.newest_deploy(deploys)
    out = {"deploy": (newest or {}).get("id"), "sha": str((newest or {}).get("sha") or "")[:12]}
    if newest:
        out["replay"] = hub_write.close_deployed_tasks(newest["id"], str(newest["sha"]))
    if progress is not None:
        progress({"step": "replayed newest deploy", "elapsed_s": int(time.time() - t0)})
    closed, refused = [], {}
    tasks = hub_app.current_state().get("by_type", {}).get("task", [])
    todo = task_completion.retry_candidates(tasks)
    for n, task in enumerate(todo, 1):
        if time.time() - t0 > budget_s:
            out["stopped"] = {"after_s": int(time.time() - t0), "done": n - 1, "of": len(todo),
                              "why": "time budget; run it again to continue"}
            break
        res = close_if_deployed(task["id"])
        if res.get("closed"):
            closed.append(task["id"])
        elif res.get("why"):
            refused[task["id"]] = res["why"]
        if progress is not None:
            progress({"step": "retrying stepped closes", "done": n, "of": len(todo),
                      "elapsed_s": int(time.time() - t0)})
    out.update(retried=len(todo), closed=closed, refused=refused)
    return out


@writer(scope="deploy:write")
def reconcile_view(request, b):
    """POST /hub/api/deploy/reconcile -- start a replay in a thread (202), or answer the one
    already running (200). It can only close what a verified deploy record already proves live,
    under the same rules as the record itself, so the deploy scope is enough."""
    cur = reconcile_state()
    beat = float(cur.get("progress_at") or cur.get("started_at") or 0)
    if cur.get("state") == "running" and time.time() - beat < RECONCILE_DEAD_S:
        return JsonResponse({"data": cur}, status=200)
    state = {"state": "running", "started_at": time.time(),
             "by": str(b.get("agent") or getattr(getattr(request, "hub_auth", None), "subject", "")
                       or "agent")[:80]}
    if cur.get("state") == "running":
        state["recovered_from"] = {"started_at": cur.get("started_at"),
                                   "why": "its file had not moved for %ds: that process died"
                                          % RECONCILE_DEAD_S}
    _reconcile_write(state)

    def _progress(p):
        _reconcile_write(dict(state, progress=p, progress_at=time.time()))

    def _run():
        out = dict(state)
        try:
            out.update(state="done", result=reconcile(progress=_progress))
        except Exception as exc:                             # noqa: BLE001
            out.update(state="failed", error=type(exc).__name__)
            hub_app.record_error("hub.deploy-close", "deploy reconcile failed: %s"
                                 % type(exc).__name__, severity="error",
                                 context={"component": "deploy-close"})
        out["finished_at"] = time.time()
        _reconcile_write(out)

    threading.Thread(target=_run, name="deploy-reconcile", daemon=True).start()
    return JsonResponse({"data": state}, status=202)


def reconcile_json(request):
    """GET /hub/deploy-reconcile.json -- the last replay: state, progress, and what it closed,
    retried or was refused (the reason is also written on each task)."""
    return JsonResponse({"data": reconcile_state()})
