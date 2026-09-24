"""A verified deploy closes the tasks whose OWN recorded commits it made live.

A worker that pushes, steps its task with the commit (``step --sha``, a typed ``pushed``
checkpoint) and then runs out of time while the pipeline is still queued cannot finish
honestly — a push is not a ship until it is verified live — and nobody is left to finish when it
does go live. The deploy record closes that loop: it names the sha the front door now serves,
and every open task whose structured checkpoint commit is CONTAINED in that build gets exactly
one ``deployed`` checkpoint. An UNATTENDED task is then finished by the hub with the deploy
record as evidence, through the same completion path every other ``done`` takes; a person's
task is only stepped — finishing it is theirs.

What makes this safe, each rule paid for once:

* Only STRUCTURED commits count (``plan[].sha`` on a non-``deployed`` checkpoint). Notes and
  evidence links can describe another task's work; inferring authorship from prose once marked
  a task deployed on somebody else's commit.
* A ``todo`` task is considered only when it recorded a commit — a handed-back task is the
  normal path to closure, but an untouched backlog item must never close on someone's deploy.
* Containment is ANCESTRY, not a range. A push whose own pipeline was cancelled as superseded
  reaches production inside a later build that has no record naming it. The question is "is
  this commit an ancestor of what is serving", asked of the VCS through an adapter seam.
* Stepped once per commit: a commit stays an ancestor of every later build, and without this
  every deploy appended another step to the same task.
* A question the VCS could not answer is REPORTED (``unchecked``), never read as "no": when the
  ancestry seam is unavailable only a task naming the served sha exactly can close, and the
  deploy response says so.
* A refused close is written onto the task (``auto_close``), because a task waiting on an
  automatic close that cannot fire has to say so.

Pure: the adapter supplies ``is_ancestor(sha, deployed) -> bool | None`` (None = could not ask)
and performs the writes.
"""

from __future__ import annotations

import re

_HEX = re.compile(r"^[0-9a-f]{7,64}$")
OPEN_STATES = ("in_progress", "todo", "blocked")


def structured_shas(task: dict) -> list:
    """Commits the task recorded via typed checkpoints, never the hub's own ``deployed`` rows."""
    out = []
    for s in task.get("plan") or []:
        if isinstance(s, dict) and s.get("kind") != "deployed":
            sha = str(s.get("sha") or "").strip().lower()
            if _HEX.match(sha) and sha not in out:
                out.append(sha)
    return out


def already_stepped(task: dict, sha: str) -> bool:
    want = str(sha or "").strip().lower()
    if not want:
        return False
    for s in task.get("plan") or []:
        if isinstance(s, dict) and s.get("kind") == "deployed":
            had = str(s.get("sha") or "").strip().lower()
            if had and (had.startswith(want) or want.startswith(had)):
                return True
    return False


def commit_that_shipped(task: dict, deployed: str, is_ancestor=None, unchecked: list | None = None) -> str:
    """The first commit this task recorded that the deployed build contains, else ''."""
    deployed = str(deployed or "").strip().lower()
    for token in sorted(structured_shas(task), key=len, reverse=True):
        if deployed.startswith(token) or token.startswith(deployed):
            return token
        if is_ancestor is None:
            continue
        verdict = is_ancestor(token, deployed)
        if verdict is None:
            if unchecked is not None:
                unchecked.append(token)
            continue
        if verdict:
            return token
    return ""


def candidates(tasks) -> list:
    """Open tasks eligible to be closed by a deploy: in progress/blocked, or todo WITH a
    structured commit. Decisions are never closed by a build."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("type", "task") != "task":
            continue
        status = str(t.get("status") or "")
        if status not in OPEN_STATES:
            continue
        if str(t.get("work_kind") or "").lower() == "decision":
            continue
        if status == "todo" and not structured_shas(t):
            continue
        out.append(t)
    return out


def deployed_step(sha: str, deployed: str, deploy_id: str, at: str) -> dict:
    """The one typed checkpoint a deploy writes: kind ``deployed``, the commit and the build."""
    note = "Deployed and verified: %s is live in build %s (%s)." % (sha[:12], deployed[:12], deploy_id)
    return {"step": "Deployed %s" % sha[:12], "done": True, "note": note, "note_at": at, "kind": "deployed",
            "sha": sha[:40]}


def plan_matches(tasks, deployed: str, is_ancestor=None) -> tuple:
    """``(matches, unchecked)``: each match is ``(task, commit)`` for a task whose recorded
    commit the build contains and which has not yet been stepped for it."""
    unchecked: list = []
    matches = []
    for t in candidates(tasks):
        hit = commit_that_shipped(t, deployed, is_ancestor, unchecked)
        if hit and not already_stepped(t, hit):
            matches.append((t, hit))
    return matches, sorted(set(unchecked))


def is_unattended(task: dict) -> bool:
    return (task or {}).get("unattended") in (True, 1, "1", "true")

