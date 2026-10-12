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
* A task that says, in its NEWEST work checkpoint, that it is NOT finished is never closed by a
  build (``unfinished_by_its_own_word``): a live commit is not a finished task. The refusal
  quotes it. Only the newest checkpoint speaks for the task -- an early "NOT DONE" written
  before the work that finished it must not refuse every close for the task's whole life -- and
  the client's own echo of a refused finish is the gate's words, never the task's.
* A deploy proves the bytes are on the host, never that the request was met. An unattended
  task closes from its deploy only once a work checkpoint says what was SEEN on the real surface
  after the change (``not_seen``); on the origin system two requests were closed "deployed and
  verified" the moment their commit went live while neither run had opened the page, and the
  page was still wrong.
* A sha written in a checkpoint's PROSE counts only when the task recorded no commit of its own
  AND a verified deploy record served exactly that commit. A note that says "rebased on main
  <base>" names the BASE: with the task's own push recorded beside it, the served base once
  closed the task minutes before its own commit was live.

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
    source, and only for a task that recorded no commit of its own: a sha a work checkpoint's
    note names, when a verified deploy served exactly it."""
    out = []
    for s in task.get("plan") or []:
        if isinstance(s, dict) and s.get("kind") != "deployed":
            sha = str(s.get("sha") or "").strip().lower()
            if _HEX.match(sha) and sha not in out:
                out.append(sha)
    if served and not out:
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
#: match can only STOP an automatic close, never cause one. Case-insensitive: a worker writes
#: "not merged" as often as "NOT MERGED". Beyond "not done", the shapes that closed tasks falsely
#: on the origin system: the work never reached the main line ("NOT PUSHED", "branch-only", "not
#: on main"), the recorded commit is somebody else's, or the task is parked on a person's yes.
_UNFINISHED = (re.compile(r"(?i)\bnot (?:yet )?(?:done|finished|complete)\b"),
               re.compile(r"(?i)\b(?:active|open) on purpose\b"),
               re.compile(r"(?i)\bdeliberately (?:left |kept )?(?:active|open)\b"),
               re.compile(r"(?i)\b(?:not|never|nothing) (?:yet |been |was )?(?:pushed|merged|"
                          r"shipped|deployed|landed|live)\b"),
               re.compile(r"(?i)\bnot (?:yet )?(?:on|in|into|reached) (?:origin/)?(?:main|master)\b"),
               re.compile(r"(?i)\bbranch[- ]only\b"),
               re.compile(r"(?i)\bnot (?:this|our|the task'?s?|its) (?:task'?s? )?"
                          r"(?:work|change|commit)\b"),
               re.compile(r"(?i)\b(?:wait(?:s|ing)?|await(?:s|ing)?|pending|blocked|parked|held)"
                          r"\s+(?:on|for)\s+(?:the\s+)?operator\b"),
               re.compile(r"(?i)\b(?:needs?|requires?|wait(?:s|ing)? for|await(?:s|ing)?|pending)"
                          r"\b[^.\n]{0,40}?\boperator(?:'s)?\s+(?:yes|approval|go-?ahead|sign-?off|"
                          r"ok|decision)\b"))

#: The client's own copy of a REFUSED finish, written back onto the task so the board shows why
#: it is still open (hub_core.client ``_note_refused_finish``).
REFUSAL_ECHO_STEP = "finish REFUSED"


def refusal_echo(step) -> bool:
    """Is this row the client's echo of a refused finish? It is the gate's words, not the task
    saying anything about its work: read as the task's word, its "so this task is NOT done"
    refused every later close by itself -- one refusal made the next finish refuse on the
    refusal."""
    return isinstance(step, dict) and (str(step.get("kind") or "") == "finish_refused"
                                       or str(step.get("step") or "").startswith(REFUSAL_ECHO_STEP))


def unfinished_by_its_own_word(task: dict) -> str:
    """The NEWEST work checkpoint, quoted, when it declares the task unfinished; else ''.

    Only the newest one counts: what the task says NOW. Scanning every checkpoint and stopping at
    the first match let one early "NOT DONE" -- written before the work that finished it -- refuse
    every automatic close for the life of the task. "Newest" is by ``note_at`` (``step --step N``
    rewrites an earlier row in place, so plan position is not time), position breaking ties and
    standing in for rows with no timestamp. A row with no note says nothing; the client's echo of
    a refused finish is skipped."""
    newest, newest_key = None, None
    for i, s in enumerate(task.get("plan") or []):
        if not isinstance(s, dict) or s.get("kind") == "deployed" or s.get("lifecycle") \
                or s.get("kind") in _LIFECYCLE or refusal_echo(s):
            continue
        if not str(s.get("note") or "").strip():
            continue
        key = (str(s.get("note_at") or ""), i)
        if newest_key is None or key > newest_key:
            newest, newest_key = s, key
    if newest is None:
        return ""
    note = str(newest.get("note") or "")
    for rx in _UNFINISHED:
        m = rx.search(note)
        if m:
            return note[max(0, m.start() - 40):m.end() + 100].strip()
    return ""


#: The marker a worker's checkpoint carries when it LOOKED at the change on the real surface,
#: the way the requester meets it (hub_core/unattended/RESPONDER.md tells every run to write it).
_SEEN = re.compile(r"\bSEEN:\s*\S")


def not_seen(task: dict) -> str:
    """Why a task may not be closed by its deploy alone: '' when a work checkpoint says what was
    SEEN on the real surface after the change; else the reason, phrased as the next run's job.

    A deploy proves the commit is on the host; it never proved the request was met. Without a
    look the close waits, the refusal is written on the task, and the next run resumes it with
    the surface as its first job. Deploy receipts and lifecycle rows never count as a look."""
    for s in _work_rows(task):
        if refusal_echo(s):
            continue
        if _SEEN.search(str(s.get("note") or "")):
            return ""
    return ("no checkpoint says what was SEEN on the real surface after the change: a deploy "
            "proves the commit is live, not that the request was met (open it, do what the "
            "requester did, and step \"SEEN: <what is on screen and in the saved state>\")")


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
    """Unattended, open tasks already stepped ``deployed`` whose close has not landed, unless
    their own words STILL say the work is unfinished (that one is a person's now).

    The task's words are re-read every pass, never the stored refusal: a later checkpoint
    supersedes them, and a quote that was only the client's refusal echo never counted. Trusting
    the stored "not finished" skipped such a task for ever."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("type", "task") != "task" or not is_unattended(t):
            continue
        if str(t.get("status") or "") not in OPEN_STATES or deployed_row(t) is None:
            continue
        ac = t.get("auto_close") if isinstance(t.get("auto_close"), dict) else {}
        if "not finished" in str(ac.get("why") or "") and unfinished_by_its_own_word(t):
            continue
        out.append(t)
    return out


def _iso_epoch(value) -> float:
    import datetime as _dt
    try:
        at = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    return at.timestamp()


def newest_recorded_push(task: dict) -> dict | None:
    """The task's newest STRUCTURED push (a ``pushed`` checkpoint carrying a sha), dated from
    when that sha was FIRST recorded (``first_at``). Never the hub's ``deployed`` receipt or a
    lifecycle row. Newest by time, position breaking ties: ``step --step N`` rewrites an earlier
    row in place, so the last row is not necessarily the last thing said. A push happens once;
    re-recording it (a verify pass told to step what it found) does not make it newer."""
    best, best_key, pushes = None, None, []
    for i, s in enumerate(task.get("plan") or []):
        if not isinstance(s, dict) or s.get("kind") != "pushed" or s.get("lifecycle"):
            continue
        if not _HEX.match(str(s.get("sha") or "").strip().lower()):
            continue
        pushes.append(s)
        key = (_iso_epoch(s.get("note_at")), i)
        if best_key is None or key > best_key:
            best, best_key = s, key
    if best is None:
        return None
    sha = str(best.get("sha")).strip().lower()
    same = [s for s in pushes if str(s.get("sha")).strip().lower() == sha and _iso_epoch(s.get("note_at"))]
    first = min(same, key=lambda s: _iso_epoch(s.get("note_at")), default=best)
    return dict(best, first_at=first.get("note_at") or best.get("note_at"))


def late_pushes(tasks, deploys) -> list:
    """``[(task, deploy record)]``: a deploy record that serves EXACTLY a task's newest recorded
    push but was written BEFORE that push was recorded, so its close never saw the push.

    The deploy record matches tasks once, when it lands, and the retry sweep only retries tasks
    already stepped ``deployed``. A push recorded seconds after its own deploy record therefore
    sat unstamped until the next release. Cheap to pick (no ancestry question: an exact served
    sha and a record older than the push); the caller replays the record through the ordinary
    matcher, so every rule still applies."""
    records = {}
    for d in deploys or []:
        if not isinstance(d, dict):
            continue
        sha = str(d.get("sha") or "").strip().lower()
        if sha and _deploy_at(d) >= _deploy_at(records.get(sha) or {}):
            records[sha] = d
    if not records:
        return []
    out = []
    for t in candidates(tasks):
        push = newest_recorded_push(t)
        if push is None:
            continue
        psha = str(push["sha"]).strip().lower()
        if already_stepped(t, psha):
            continue
        d = next((rec for sha, rec in records.items()
                  if sha.startswith(psha) or psha.startswith(sha)), None)
        pushed_at = _iso_epoch(push.get("first_at"))
        if d is None or not pushed_at or _deploy_at(d) > pushed_at:
            continue                     # a record written after the push already saw it
        out.append((t, d))
    return out


def _deploy_at(d: dict) -> float:
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    return _iso_epoch(d.get("at") or prov.get("created_at") or "")


def newest_deploy(deploys) -> dict | None:
    """The newest deploy record (by ``at``) -- the one a reconcile replays."""
    rows = [d for d in deploys or [] if isinstance(d, dict) and d.get("sha")]
    return max(rows, key=lambda d: str(d.get("at") or ""), default=None)


def is_unattended(task: dict) -> bool:
    return (task or {}).get("unattended") in (True, 1, "1", "true")

