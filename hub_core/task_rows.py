"""A task row that says what is HAPPENING to it, not only what its status word is.

A consuming app (or a person) looking at "todo" cannot tell a task nobody has touched from one a
worker is on right now, one whose commit is pushed and waiting for its pipeline, one handed back
after a run ran out of time, or one already live in production. Every one of those facts is on
the board already — the lease, the run ledger, the typed checkpoints, the error stream — and this
module joins them onto the row, once, for every reader:

* ``holder``      who holds the lease: agent, console, and whether that console is still live
                  (``abandoned`` when it is not — the orphaned-lease state)
* ``responder``   the latest run on the task: waiting / working / finished, with its outcome
* ``pushed``      the commit a worker recorded (typed ``pushed`` checkpoint first; a sha in a
                  note only as a fallback, labelled ``source: note``, and never hex glued to an
                  id — "task-0042abc" is not a commit)
* ``handed_back`` how many times the scheduler handed it back, and when last
* ``deployed``    the build a verified deploy found its commit live in
* ``ci_problem``  an unacked error-stream row joined on the task's OWN recorded sha or pipeline
                  — never on project alone, which blames every task in a project for one red job

``feed()`` is the per-project task list a consuming app polls (open + recently finished) with a
validator whose tag moves only when something a reader shows moves: version, priority, push,
deploy, hand-back, holder, responder.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from . import checkpoints, task_health

FINISHED_WINDOW_S = 14 * 86400
LIVE_RUN = task_health.LIVE_RUN_STATES
_NOTE_SHA = re.compile(r"(?<![0-9A-Za-z_:/#.-])(?=[0-9a-f]{7,40}(?![0-9A-Za-z_-]))"
                       r"[0-9a-f]*[a-f][0-9a-f]*(?![0-9A-Za-z_-])")


def note_sha(text) -> str:
    """A commit sha written in prose, or ''. Needs a hex letter (a plain number is a figure) and
    refuses hex glued to an identifier, path or anchor."""
    m = _NOTE_SHA.search(str(text or ""))
    return m.group(0) if m else ""


def note_shas(text) -> list:
    """Every distinct commit sha written in prose, in order (the same rule as note_sha)."""
    out = []
    for m in _NOTE_SHA.finditer(str(text or "")):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def pushed(task: dict) -> dict | None:
    typed = checkpoints.latest(task, "pushed")
    if typed and typed.get("sha"):
        out = {"sha": typed["sha"], "source": "step", "at": typed.get("note_at") or ""}
        for key in ("pipeline_id", "pipeline_url"):
            if typed.get(key):
                out[key] = typed[key]
        return out
    for s in reversed(task.get("plan") or []):
        if isinstance(s, dict) and s.get("kind") not in ("deployed",) and not s.get("lifecycle"):
            if s.get("sha"):
                return {"sha": s["sha"], "source": "step", "at": s.get("note_at") or ""}
            sha = note_sha(s.get("note"))
            if sha:
                return {"sha": sha, "source": "note", "at": s.get("note_at") or ""}
    return None


def handed_back(task: dict) -> dict | None:
    """Runs that ended with the task unfinished (the launcher's ``handed_back`` rows, which its
    run cap counts), plus -- reported apart, never counted as runs -- the times the hub released
    an expired lease (``lease_released``)."""
    plan = [s for s in task.get("plan") or [] if isinstance(s, dict)]
    rows = [s for s in plan if s.get("kind") == "handed_back"]
    released = [s for s in plan if s.get("kind") == "lease_released"]
    if not rows and not released:
        return None
    return {"times": sum(int(s.get("times") or 1) for s in rows),
            "lease_released": sum(int(s.get("times") or 1) for s in released),
            "at": max(str(s.get("note_at") or "") for s in rows + released)}


def deployed(task: dict) -> dict | None:
    row = checkpoints.latest(task, "deployed")
    if not row:
        return None
    return {"sha": row.get("sha") or "", "at": row.get("note_at") or "",
            "note": str(row.get("note") or "")[:200]}


def holder(lease: dict | None, live_sessions: set, now: float) -> dict | None:
    if not lease or float(lease.get("expires") or 0) <= now:
        return None
    sid = str(lease.get("session") or "")[:8]
    live = bool(sid) and sid in live_sessions
    return {"agent": lease.get("agent"), "session": sid, "live": live,
            "abandoned": bool(sid) and not live,
            "held_s": int(now - float(lease.get("claimed") or now)),
            "expires_in_s": max(0, int(float(lease.get("expires") or 0) - now))}


def responder(runs: list) -> dict | None:
    if not runs:
        return None
    run = max(runs, key=lambda r: (str((r.get("provenance") or {}).get("updated_at") or ""),
                                   str(r.get("id") or "")))
    status = str(run.get("status") or "")
    state = ("waiting" if status == "input_required" else
             "working" if status in LIVE_RUN else "finished")
    out = {"run": run.get("id"), "state": state, "status": status, "owner": run.get("owner")}
    if state == "finished":
        out["outcome"] = status
    return out


def ci_problem(task: dict, error_rows: list) -> dict | None:
    """An unacked error row whose context names THIS task's recorded commit or pipeline."""
    push = pushed(task) or {}
    sha = str(push.get("sha") or "").lower() if push.get("source") == "step" else ""
    pipeline = str(push.get("pipeline_id") or "")
    if not sha and not pipeline:
        return None
    for row in error_rows or []:
        if row.get("acked"):
            continue
        ctx = row.get("context") or {}
        rsha = str(ctx.get("sha") or "").lower()
        rpipe = str(ctx.get("pipeline_id") or ctx.get("pipeline") or "")
        if (sha and rsha and (rsha.startswith(sha) or sha.startswith(rsha))) or \
                (pipeline and rpipe == pipeline):
            return {"fingerprint": row.get("fingerprint"), "message": str(row.get("message") or "")[:160],
                    "source": row.get("source"), "ts": row.get("ts")}
    return None


def annotate(tasks, *, leases=None, runs=None, live_sessions=None, error_rows=None,
             now: float | None = None) -> list:
    """Return new rows (inputs untouched) carrying the joined facts plus the work-only progress."""
    now = time.time() if now is None else now
    lease_by_task = {str(l.get("task") or ""): l for l in leases or [] if isinstance(l, dict)}
    runs_by_task = {}
    for r in runs or []:
        if isinstance(r, dict) and r.get("task"):
            runs_by_task.setdefault(str(r["task"]), []).append(r)
    live = {str(s)[:8] for s in live_sessions or [] if s}
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        row = dict(t)
        tid = str(t.get("id") or "")
        counts = checkpoints.progress(t)
        row.update({
            "holder": holder(lease_by_task.get(tid), live, now),
            "responder": responder(runs_by_task.get(tid) or []),
            "pushed": pushed(t), "handed_back": handed_back(t), "deployed": deployed(t),
            "ci_problem": ci_problem(t, error_rows),
            "progress": {"done": counts["done"], "total": counts["total"],
                         "not_counted": counts["lifecycle"] + counts["placeholders"]},
        })
        out.append(row)
    return out


def _finished_recently(task, now) -> bool:
    if task.get("status") not in ("done", "dropped"):
        return False
    prov = task.get("provenance") or {}
    age = task_health.age_s(prov.get("updated_at") or prov.get("created_at"), now)
    return age is not None and age <= FINISHED_WINDOW_S


def feed(rows, project: str, now: float | None = None) -> dict:
    """``{open, finished, etag}`` for one project's tasks (``task.project`` equals the slug)."""
    now = time.time() if now is None else now
    slug = str(project or "").strip().lower()
    mine = [r for r in rows or [] if str(r.get("project") or "").strip().lower() == slug]
    open_rows = [r for r in mine if r.get("status") not in ("done", "dropped")]
    finished = [r for r in mine if _finished_recently(r, now)]
    open_rows.sort(key=lambda r: (str(r.get("priority") or "P9"), str(r.get("id"))))
    finished.sort(key=lambda r: str((r.get("provenance") or {}).get("updated_at") or ""),
                  reverse=True)
    basis = [[r.get("id"), r.get("version"), r.get("priority"), r.get("status"),
              (r.get("pushed") or {}).get("sha"), (r.get("deployed") or {}).get("sha"),
              (r.get("handed_back") or {}).get("times"),
              (r.get("holder") or {}).get("agent"), (r.get("holder") or {}).get("live"),
              (r.get("responder") or {}).get("state")] for r in open_rows + finished]
    tag = hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return {"project": slug, "open": open_rows, "finished": finished, "etag": tag,
            "counts": {"open": len(open_rows), "finished_14d": len(finished)}}
