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
* A refused close is RETRIED, not final. The common refusal is a live lease -- the run is still
  watching the same pipeline -- and it goes away moments later when the run hands the task back.
  A task already stepped ``deployed`` but not done is offered to the close again on every later
  deploy, on the hand-back that releases its lease, and by a throttled sweep, so the close lands
  when the obstacle leaves instead of when a person notices (on the origin system, thirty such
  tasks carried "deployed and verified" for hours to days before a person closed them).
* A task that says, in its own checkpoint, that it is NOT finished is never closed by a build
  (``unfinished_by_its_own_word``): a live commit is not a finished task. The refusal quotes it.
* A sha written in a checkpoint's PROSE counts only when a verified deploy record served exactly
  that commit -- the one fact that says it is a real, shipped commit and not a word in a note.

Pure: the adapter supplies ``is_ancestor(sha, deployed) -> bool | None`` (None = could not ask)
and performs the writes.
"""

from __future__ import annotations

import re

_HEX = re.compile(r"^[0-9a-f]{7,64}$")
OPEN_STATES = ("in_progress", "todo", "blocked")


def structured_shas(task: dict, served=None) -> list:
    """Commits the task recorded via typed checkpoints, never the hub's own ``deployed`` rows.

    ``served`` (the shas verified deploy records name as what they served) admits ONE more
    source: a sha a work checkpoint's note names, when a verified deploy served exactly it."""
    out = []
    for s in task.get("plan") or []:
        if isinstance(s, dict) and s.get("kind") != "deployed":
            sha = str(s.get("sha") or "").strip().lower()
            if _HEX.match(sha) and sha not in out:
                out.append(sha)
    if served:
        served = [str(x).lower() for x in served if x]
        for token in note_shas(task):
            if token not in out and any(full.startswith(token) for full in served):
                out.append(token)
    return out


_NOTE_SHA = re.compile(r"(?<![-\w:/.#])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![-\w])")
_LIFECYCLE = frozenset({"handed_back", "lease_released", "reaped", "launcher_timeout"})


def _work_rows(task: dict) -> list:
    """Checkpoints a worker wrote about the work: never a lifecycle row, never the hub's own
    ``deployed`` receipt."""
    return [s for s in task.get("plan") or []
            if isinstance(s, dict) and s.get("kind") != "deployed" and not s.get("lifecycle")
            and s.get("kind") not in _LIFECYCLE]


def note_shas(task: dict) -> list:
    """Shas a WORK checkpoint's prose names. A candidate only: ``structured_shas`` decides whether
    one counts."""
    out = []
    for s in _work_rows(task):
        for tok in _NOTE_SHA.findall(str(s.get("note") or "").lower()):
            if tok not in out:
                out.append(tok)
    return out


#: A checkpoint that says, in so many words, that the work is NOT finished. Fail direction: a
#: match can only STOP an automatic close, never cause one.
_UNFINISHED = (re.compile(r"\bNOT (?:YET )?(?:DONE|FINISHED|COMPLETE)\b"),
               re.compile(r"(?i)\b(?:active|open) on purpose\b"),
               re.compile(r"(?i)\bdeliberately (?:left |kept )?(?:active|open)\b"))


def unfinished_by_its_own_word(task: dict) -> str:
    """The newest WORK checkpoint that declares the task unfinished, quoted; else ''."""
    for s in reversed(_work_rows(task)):
        note = str(s.get("note") or "")
        for rx in _UNFINISHED:
            m = rx.search(note)
            if m:
                return note[max(0, m.start() - 40):m.end() + 100].strip()
    return ""


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


def commit_that_shipped(task: dict, deployed: str, is_ancestor=None, unchecked: list | None = None,
                        served=None) -> str:
    """The first commit this task recorded that the deployed build contains, else ''."""
    deployed = str(deployed or "").strip().lower()
    for token in sorted(structured_shas(task, served), key=len, reverse=True):
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


def candidates(tasks, served=None) -> list:
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
        if status == "todo" and not structured_shas(t, served):
            continue
        out.append(t)
    return out


def deployed_step(sha: str, deployed: str, deploy_id: str, at: str) -> dict:
    """The one typed checkpoint a deploy writes: kind ``deployed``, the commit and the build."""
    note = "Deployed and verified: %s is live in build %s (%s)." % (sha[:12], deployed[:12], deploy_id)
    return {"step": "Deployed %s" % sha[:12], "done": True, "note": note, "note_at": at, "kind": "deployed",
            "sha": sha[:40]}


def plan_matches(tasks, deployed: str, is_ancestor=None, served=None) -> tuple:
    """``(matches, unchecked)``: each match is ``(task, commit, retry)`` for a task whose recorded
    commit the build contains. ``retry`` is True when the task was already stepped for that
    commit (an earlier close was refused): it is not stepped again, only offered to the close."""
    unchecked: list = []
    matches = []
    for t in candidates(tasks, served):
        hit = commit_that_shipped(t, deployed, is_ancestor, unchecked, served)
        if not hit:
            continue
        if already_stepped(t, hit):
            if is_unattended(t):
                matches.append((t, hit, True))
            continue
        matches.append((t, hit, False))
    return matches, sorted(set(unchecked))


def deployed_row(task: dict) -> dict | None:
    """The newest ``deployed`` checkpoint on a task (the proof a close can be retried on)."""
    for s in reversed(task.get("plan") or []):
        if isinstance(s, dict) and s.get("kind") == "deployed" and s.get("sha"):
            return s
    return None


def retry_candidates(tasks) -> list:
    """Unattended, open tasks already stepped ``deployed`` whose close has not landed, and whose
    refusal was not their own word (that one is a person's now)."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("type", "task") != "task" or not is_unattended(t):
            continue
        if str(t.get("status") or "") not in OPEN_STATES or deployed_row(t) is None:
            continue
        ac = t.get("auto_close") if isinstance(t.get("auto_close"), dict) else {}
        if "not finished" in str(ac.get("why") or ""):
            continue
        out.append(t)
    return out


def newest_deploy(deploys) -> dict | None:
    """The newest deploy record (by ``at``) -- the one a reconcile replays."""
    rows = [d for d in deploys or [] if isinstance(d, dict) and d.get("sha")]
    return max(rows, key=lambda d: str(d.get("at") or ""), default=None)


def is_unattended(task: dict) -> bool:
    return (task or {}).get("unattended") in (True, 1, "1", "true")

