"""What every live console is doing, bound to a PROJECT and to the task it holds — whether or not
anyone typed `start`.

Presence knows "worker-1 has a console in /src/budget-app"; nothing turned that into
"budget-app is being worked on with no task", so work done in a repository nobody filed a task
for was invisible while it happened. This module is that binding. It never writes the ledger (a
per-prompt ledger event would be folded on every read, forever); it is a PROJECTION over
presence + leases + tasks. Framework-free.

Two rules, both paid for on an origin system:

* A CLAIM ASSERTS RESPONSIBILITY AND IS NEVER INFERRED FROM A DIRECTORY. A console holds a task
  only when THAT console made the claim (the lease records the claiming session). Marking every
  console of an agent as holding whatever that agent holds about the matching project showed a
  console that merely cd'd into a repository to read a file as holding somebody else's task — a
  confident board that is wrong. A lease written before sessions were recorded is attributed only
  when exactly one console of that agent could own it; with several, the honest answer is "the
  hub does not know".
* A CLAIM THIS CONSOLE MADE IS CERTAIN WHATEVER DIRECTORY IT STANDS IN. The project rule only
  orders several such claims; requiring the task to be ABOUT the console's project made `start`
  succeed and the roster say "no task" in the same minute.
"""

from __future__ import annotations

import re
from pathlib import PurePath

_TITLE_PREFIX = re.compile(r"^\s*([A-Za-z0-9_.-]{2,60})\s*:")


def _norm(s) -> str:
    return str(s or "").strip().lower().replace("_", "-")


def project_of(cwd: str = "", repo: str = "", app: str = "") -> str:
    """The project a console is standing in: the declared app, else the repository remote's
    leaf (".../budget-app.git" -> budget-app), else the working directory's leaf."""
    if app:
        return _norm(app)
    if repo:
        leaf = str(repo).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        leaf = leaf[:-4] if leaf.endswith(".git") else leaf
        if leaf:
            return _norm(leaf)
    if cwd:
        try:
            return _norm(PurePath(str(cwd).replace("\\", "/")).name)
        except (TypeError, ValueError):
            return ""
    return ""


def task_project(task: dict) -> str:
    """The project a task is about, from the `project: title` convention, else ''."""
    m = _TITLE_PREFIX.match(str((task or {}).get("title") or ""))
    return _norm(m.group(1)) if m else ""


def _mentions(task: dict, project: str) -> bool:
    if not project:
        return False
    hay = _norm(" ".join(str((task or {}).get(k) or "") for k in ("title", "acceptance")))
    return re.search(r"(?<![a-z0-9-])" + re.escape(project) + r"(?![a-z0-9-])", hay) is not None


def match_task(project: str, candidates: list, by_id: dict) -> str:
    """The candidate task ABOUT this project: exact title-prefix match first, then a whole-word
    mention in title/acceptance. '' when none is."""
    project = _norm(project)
    if not project:
        return ""
    for tid in candidates:
        if task_project(by_id.get(tid) or {}) == project:
            return tid
    for tid in candidates:
        if _mentions(by_id.get(tid) or {}, project):
            return tid
    return ""


def rows(live_sessions: list, leases: list, tasks: list) -> list:
    """Every live console with the project it is in and the task THIS console holds.

    `live_sessions` are presence.live_sessions() rows (the uniform session shape); `leases` are
    the live lease records (agent, task, session); `tasks` the task entities. Each output row is
    the session row plus project, has_task, task_id, task_title — the same key set on every row.
    `has_task: false` rows are the work a board otherwise loses."""
    by_id = {t.get("id"): t for t in (tasks or []) if isinstance(t, dict) and t.get("id")}
    held = {}
    for lease in leases or []:
        agent = _norm(lease.get("agent"))
        if agent and lease.get("task"):
            held.setdefault(agent, []).append((lease["task"], str(lease.get("session") or "")[:8]))
    out, by_agent = [], {}
    for s in live_sessions or []:
        row = dict(s)
        row["project"] = project_of(s.get("cwd"), s.get("repo"), s.get("app"))
        out.append(row)
        by_agent.setdefault(_norm(s.get("agent")), []).append(row)
    for agent, consoles in by_agent.items():
        candidates = held.get(agent, [])
        for row in consoles:
            sid = str(row.get("session") or "")[:8]
            mine = [tid for tid, lsid in candidates if lsid and lsid == sid]
            tid = match_task(row["project"], mine, by_id) or (mine[0] if mine else "")
            if not tid:
                legacy = [t for t, lsid in candidates if not lsid]
                if legacy and len(consoles) == 1:
                    tid = match_task(row["project"], legacy, by_id) or legacy[0]
            row["has_task"] = bool(tid)
            row["task_id"] = tid
            row["task_title"] = str((by_id.get(tid) or {}).get("title") or "")[:160]
    return out


def summary(activity_rows: list) -> dict:
    """The denominator a reader needs: consoles, how many hold a task, which projects are being
    worked with no task at all."""
    untasked = sorted({r.get("project") for r in activity_rows or []
                       if r.get("project") and not r.get("has_task")})
    return {"consoles": len(activity_rows or []),
            "with_task": sum(1 for r in activity_rows or [] if r.get("has_task")),
            "without_task": sum(1 for r in activity_rows or [] if not r.get("has_task")),
            "projects": sorted({r.get("project") for r in activity_rows or [] if r.get("project")}),
            "projects_without_task": untasked}


def no_task_for(activity_rows: list, session: str) -> list:
    """The projects THIS console is in without a task — the one-line nudge a client prints."""
    sid = str(session or "")[:8]
    if not sid:
        return []
    return [r for r in activity_rows or [] if str(r.get("session") or "")[:8] == sid
            and not r.get("has_task") and r.get("project")]


def headline(consoles: list) -> dict | None:
    """The console an agent card should headline: the freshest WORKING console with a real
    focus, else the freshest console with one. A card that headlines an idle window's stale
    focus reads as what the person is doing now, and is not."""
    ranked = sorted((c for c in consoles or [] if c.get("focus")),
                    key=lambda c: (c.get("state") != "working",
                                   c.get("age_s") if c.get("age_s") is not None else 10 ** 9))
    return ranked[0] if ranked else None
