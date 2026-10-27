"""A verified deploy closes the tasks whose OWN recorded commits it made live.

A worker that pushes, steps its task with the commit (``step --sha``, a typed ``pushed``
checkpoint) and then runs out of time while the pipeline is still queued cannot finish
honestly — a push is not a ship until it is verified live — and nobody is left to finish when it
does go live. The deploy record closes that loop: it names the sha the front door now serves,
and every open task whose structured checkpoint commit is CONTAINED in that build gets exactly
one ``deployed`` checkpoint. A deploy proves the commit is LIVE, not that the request was met,
so that is all it does: the task closes -- unattended or a person's, through the same
completion path every other ``done`` takes -- once a ``verified`` row with result=pass names the
deployed commit, written after the receipt and after the newest ``rejected`` row, with no
failure standing after it (see "outcomes" below). Until then the task says it is awaiting
verification.

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
* A deploy proves the bytes are on the host, never that the request was met. A task closes from
  its deploy only once a ``verified`` pass of the DEPLOYED commit was written after the deploy
  (``verification_after_deploy``); on the origin system requests were closed "deployed and
  verified" the moment their commit went live while nobody had opened the page, and a "SEEN:"
  line anywhere in the plan was not enough either -- one written while reproducing, BEFORE the
  fix, satisfied it.
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
    """Open tasks stepped ``deployed`` (after their newest rejection) whose close has not landed,
    that a passing verification has since checked, unless their own words STILL say the work is
    unfinished (that one is a person's now). A task still waiting on its verification is not
    retried: there is nothing new for the close to read until one lands, and the outcome write
    closes it then.

    The task's words are re-read every pass, never the stored refusal: a later checkpoint
    supersedes them, and a quote that was only the client's refusal echo never counted. Trusting
    the stored "not finished" skipped such a task for ever."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("type", "task") != "task":
            continue
        if str(t.get("status") or "") not in OPEN_STATES or deployed_after_floor(t) is None:
            continue
        if outcome_after_deploy(t) != "verified":
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



# ---------------------------------------------------------------- outcomes: done is the machine check
#
# A deploy proves a commit is LIVE, not that the request was met. On the instance this was lifted
# from, tasks closed on a deploy of another task's commit, on their rebase base, while the cells a
# person asked for were still blank, and while the post-deploy job was red -- every one "deployed
# and verified". So a deploy only STEPS a task (`deployed`); the task closes once a `verified` row
# with result=pass names that deployed commit, written AFTER the deployed receipt and after the
# newest rejection, and no `verified` failure stands after it. A person should never have to
# confirm: the check is a machine reading of the deployed build (a post-deploy probe, or a worker's
# `verify` with the artifact it read), and the per-app rate the board shows is that reading.


def _iso_epoch(value) -> float:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _sha_eq(a, b) -> bool:
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _positions(task: dict) -> list:
    """``[(row, (epoch, index))]``: WHEN each row was written, its plan position breaking a tie.
    Receipts are stamped to the second, so a deploy, its verification and a rejection can share
    one; the row written later is the one further down the plan (receipts are only appended)."""
    return [(s, (_iso_epoch(s.get("note_at")), i))
            for i, s in enumerate((task or {}).get("plan") or []) if isinstance(s, dict)]


def _rejection_pos(task: dict) -> tuple:
    """WHEN the newest `rejected` row was written. A rejection reopens the SAME task, and every
    receipt written before it described the attempt the requester just said does not work: only
    rows written after it count."""
    return max([p for s, p in _positions(task) if s.get("kind") == "rejected"] or [(0.0, -1)])


def deployed_after_floor(task: dict, sha: str = "") -> dict | None:
    """The newest `deployed` row (for `sha` when given) written after the newest rejection."""
    floor = _rejection_pos(task)
    for s, pos in reversed(_positions(task)):
        if (s.get("kind") == "deployed" and s.get("sha")
                and (not sha or _sha_eq(s.get("sha"), sha)) and pos > floor):
            return s
    return None


_FAILED_PREFIX = "a verification after the deploy FAILED"


def verification_after_deploy(task: dict, sha: str, deployed: dict, is_ancestor=None) -> str:
    """'' when a `verified` pass names `sha` (or a build that contains it), written after the
    `deployed` receipt and after the newest rejection, and no unanswered failure stands; else why
    not. A failure is superseded only by a LATER pass on the same build or one that contains it
    (a fix that landed in a later commit, verified on the build carrying it); a failure newer than
    every pass still refuses. ``is_ancestor(a, b) -> bool | None`` is the adapter's VCS seam; None
    means only exact shas match."""
    sha = str(sha or "").strip().lower()
    rows = _positions(task)
    dep_pos = next((p for s, p in rows if s is deployed),
                   (_iso_epoch((deployed or {}).get("note_at")), -1))
    after = max(dep_pos, _rejection_pos(task))

    def covers(vsha: str, target: str) -> bool:
        if _sha_eq(vsha, target):
            return True
        if is_ancestor is None or not _HEX.match(vsha or "") or not _HEX.match(target or ""):
            return False
        return bool(is_ancestor(target, vsha))

    passes, fails = [], []
    for s, at in rows:
        if s.get("kind") != "verified" or at <= after:
            continue
        vsha = str(s.get("sha") or "").strip().lower()
        if not covers(vsha, sha):
            continue
        (passes if str(s.get("result") or "").lower() == "pass" else fails).append((at, vsha, s))
    for f_at, f_sha, f_row in sorted(fails, key=lambda x: x[0], reverse=True):
        if not any(p_at > f_at and covers(p_sha, f_sha) for p_at, p_sha, _ in passes):
            return (_FAILED_PREFIX + " (%s): %s"
                    % (f_row.get("note_at"),
                       str(f_row.get("note") or f_row.get("artifact") or "")[:120]))
    if not passes:
        return ("no verification of the deployed %s after the deploy: a deploy proves the commit is "
                "live, not that the request was met (open it as the requester would, then record "
                "`verify <task> --sha %s --artifact <what you read> --note \"SEEN: ...\"`)"
                % (sha[:12], sha[:12]))
    return ""


def verification_row(task: dict, sha: str) -> dict | None:
    """The newest passing `verified` row naming `sha`, else None (what a close cites)."""
    for s in reversed((task or {}).get("plan") or []):
        if (isinstance(s, dict) and s.get("kind") == "verified" and _sha_eq(s.get("sha"), sha)
                and str(s.get("result") or "").lower() == "pass"):
            return s
    return None


def outcome_after_deploy(task: dict) -> str:
    """'verified' | 'failed' | '' -- what the machine check said about a task's deployed build,
    read from the task's own rows only (no VCS call): the newest `deployed` row after the newest
    rejection, judged by the same rule the close uses -- a failure stands unless a later pass on
    the same build answered it ('failed'); a pass with no standing failure is 'verified'; nothing
    checked after the deploy is ''."""
    dep = deployed_after_floor(task)
    if dep is None:
        return ""
    why = verification_after_deploy(task, str(dep.get("sha") or ""), dep)
    if not why:
        return "verified"
    return "failed" if why.startswith(_FAILED_PREFIX) else ""


_APP_PREFIX = re.compile(r"^([a-z0-9][a-z0-9-]{1,60}):")


def task_app(task: dict) -> str:
    """The app (or component) a task is about, for the per-app outcome rate: its `project`, else
    the title's ``slug:`` prefix, else ''."""
    project = str((task or {}).get("project") or "").strip().lower()
    if project:
        return project
    title = str((task or {}).get("title") or "").strip().lower()
    # A problem's mirror task is titled "Fix <app>: <message>" (hub_core.fix_tasks): the app is
    # the word after "Fix", never "fix" itself -- read as a slug, every mirror named no app.
    if title.startswith("fix "):
        title = title[4:]
    m = _APP_PREFIX.match(title)
    return m.group(1) if m else ""


def verified_live_by_app(tasks) -> dict:
    """``{app: {app, closed, verified_live, failed}}`` over EVERY closed task: "N of M closed
    requests verified live, F failed". The rest of M closed with no machine check of the deployed
    build at all. Callers pass the FULL collection -- a head of it is a truncated denominator."""
    out: dict = {}
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("status") != "done":
            continue
        app = task_app(t)
        if not app:
            continue
        row = out.setdefault(app, {"app": app, "closed": 0, "verified_live": 0, "failed": 0})
        row["closed"] += 1
        state = outcome_after_deploy(t)
        if state == "verified":
            row["verified_live"] += 1
        elif state == "failed":
            row["failed"] += 1
    return out


def outcome_row(kind: str, *, sha: str = "", result: str = "", artifact: str = "", note: str = "",
                by: str = "", facts=None, at: str = "") -> dict:
    """The one shape an outcome row takes. Raises ValueError with a sentence a caller can act on."""
    if kind not in ("verified", "rejected"):
        raise ValueError("kind must be verified or rejected")
    sha = str(sha or "").strip().lower()
    if sha and not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("sha must be a 7..40 character hexadecimal commit id")
    note = str(note or "").strip()
    row = {"done": True, "kind": kind, "note": note, "note_at": at, "by": str(by or "")[:120]}
    if kind == "verified":
        result = str(result or "").strip().lower()
        if result not in ("pass", "fail"):
            raise ValueError("a verification's result is pass or fail")
        if not sha:
            raise ValueError("a verification names the deployed commit it checked (--sha)")
        if not str(artifact or "").strip():
            raise ValueError("a verification names what it READ (--artifact: a URL, a screenshot, "
                             "a probe's output) -- a verdict with nothing behind it is not a check")
        row.update(step=("Verified %s: %s" % (sha[:12], result))[:80], sha=sha, result=result)
    else:
        if not note:
            raise ValueError("a rejection says what is still wrong (--note)")
        row["step"] = ("Rejected: " + note.splitlines()[0])[:80]
        if sha:
            row["sha"] = sha
    if str(artifact or "").strip():
        row["artifact"] = str(artifact).strip()[:500]
    if isinstance(facts, dict) and facts:
        row["facts"] = facts
    return row
