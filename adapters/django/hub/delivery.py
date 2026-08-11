"""Where a task's WORK actually is — from recorded evidence only.

`done` is a claim about a RECEIPT. *Landed* is a claim about the integration branch. *Deployed* is
a claim about a release record. *Live* is a claim about the running artifact. They are four
different questions, and collapsing them into one green word is how a board reports success for
work that is still sitting on a worktree branch.

So no state here is read off a status label. Each leg has exactly ONE evidence source:

    verified   an exit-0 verification_run receipt on the task
    landed     a recorded commit that is an ANCESTOR of the integration branch
    deployed   a deploy entity whose recorded sha carries that commit
    live       the served sha this request was handed matches a deploy that carries it

THE CENTRAL RULE: a leg that cannot be measured in this context reports UNMEASURED — never False,
and never quietly True. Inside a shipped container there is no `.git`, so the ancestry probe cannot
run; the honest answer is "nobody asked", and a `landed` count computed as done-minus-unlanded
would silently promote every task whose ancestry was never checked into a positive claim about the
integration branch. Counts here are therefore COUNTED from what measured true, never subtracted,
and the never-measured remainder gets its own number so the two halves of any ratio describe the
same set.

`live` additionally distinguishes ATTESTED from claimed: only the caller can probe the running box,
so `?served=<sha>` is a caller's assertion. It is honoured, because the caller is the only one who
can see the box — but it is never silent. An unattested live reading says so.
"""
import subprocess

from . import hub_app

# The absence VOCABULARY: what each unmeasured leg means, so a record is never absent-with-no-
# reason. A field that cannot say WHY it is unknown reads as a bug in the board rather than a
# question nobody could ask.
_ABSENT_DEFAULT = {
    "landing": "no repository in this context — commit ancestry could not be checked here",
    "release": "no deploy record carries a comparable sha",
    "live": "the running artifact was not probed, so no task can be proven live here",
    "verified": "no verification_run receipt recorded on this task",
}
# Worst-to-best, so a record's headline state is the FURTHEST leg it can actually prove.
_STATE_ORDER = {"unrecorded": 0, "unverified": 1, "verified": 2, "landed": 3,
                "deployed": 4, "live": 5}


def _git(*args):
    """Run a git command in the repo root. Returns None when git or the repo is absent — which is
    a measurement that could not be taken, not a negative result."""
    try:
        out = subprocess.run(("git",) + args, cwd=str(hub_app.BASE_DIR), capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None  # absorbs: no git binary / no repo / timeout — the leg is unmeasurable
    return out.stdout.strip() if out.returncode == 0 else None


def _repo_present():
    return _git("rev-parse", "--git-dir") is not None


def _integration_ref():
    """The branch a commit must reach to count as landed. HEAD is the honest default: an adopter's
    integration branch is theirs to name, and guessing 'main' would report red on a repo that
    calls it something else."""
    return hub_app._dj_setting("HUB_INTEGRATION_REF", None) or "HEAD"


def _commits_of(task):
    return [c for c in ((task.get("provenance") or {}).get("commits") or []) if str(c).strip()]


def _receipt_ok(task):
    runs = task.get("verification_run") or []
    if isinstance(runs, dict):
        runs = [runs]
    return any(isinstance(r, dict) and r.get("exit_code") == 0 for r in runs)


def _is_ancestor(commit, ref):
    """True/False when the question could be asked, None when it could not (unknown commit, no
    repo). A shallow clone that does not CONTAIN the commit cannot answer it either, and that is
    an unmeasured leg rather than an unlanded commit."""
    if _git("cat-file", "-e", "%s^{commit}" % commit) is None:
        return None
    try:
        out = subprocess.run(("git", "merge-base", "--is-ancestor", commit, ref),
                             cwd=str(hub_app.BASE_DIR), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None  # absorbs: probe could not run — unmeasurable, never "not landed"
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None  # any other exit is git failing to answer, not a negative answer


def block(state, served=None):
    """The delivery projection for the whole board.

    {records: {task_id: record}, counts, measured: {leg: bool}, notes, unlanded: [...],
     available: [...], live_attested: bool, live_identity: {...}}"""
    tasks = state.get("by_type", {}).get("task", [])
    deploys = state.get("by_type", {}).get("deploy", [])
    repo = _repo_present()
    ref = _integration_ref()

    # A deploy record is comparable only if it names a sha we can relate commits to.
    deploy_shas = [str(d.get("sha") or "").strip() for d in deploys if str(d.get("sha") or "").strip()]
    served_sha = str(served or "").strip()

    measured = {"verified": True,          # a receipt is on the entity; always askable
                "landing": bool(repo),
                "release": bool(deploy_shas),
                "live": bool(served_sha and deploy_shas and repo)}
    notes = {leg: (None if ok else _ABSENT_DEFAULT[leg]) for leg, ok in measured.items()}
    if repo and not deploy_shas:
        notes["release"] = "no deploy has been recorded on this board yet"

    records, unlanded, available = {}, [], []
    for t in tasks:
        if (t.get("status") or "").lower() != "done":
            continue
        tid = t["id"]
        commits = _commits_of(t)
        verified = _receipt_ok(t)

        landed = None
        if repo and commits:
            answers = [_is_ancestor(c, ref) for c in commits]
            if any(a is True for a in answers):
                landed = True
            elif all(a is False for a in answers):
                landed = False
            # a mix of False and None stays None: one uncheckable commit means the question was
            # not fully asked, and "partially unlanded" is not a claim this can make
        deployed = None
        if landed is True and deploy_shas:
            deployed = any(_is_ancestor(c, sha) is True for c in commits for sha in deploy_shas)
        live = None
        if deployed is True and served_sha:
            live = any(_is_ancestor(c, served_sha) is True for c in commits)

        if landed is False:
            state_name, reason = "unlanded", (
                "recorded commit is not an ancestor of %s — done on the board, not on the "
                "integration branch" % ref)
        elif live is True:
            state_name, reason = "live", "carried by the served artifact"
        elif deployed is True:
            state_name, reason = "deployed", "carried by a recorded release"
        elif landed is True:
            state_name, reason = "landed", "on the integration branch"
        elif verified:
            state_name, reason = "verified", (notes["landing"] or "landing not established")
        elif commits:
            state_name, reason = "unverified", _ABSENT_DEFAULT["verified"]
        else:
            state_name, reason = ("unverified" if not verified else "verified"), (
                "no commit recorded on this task, so nothing downstream of the receipt can be asked")

        records[tid] = {"state": state_name, "state_reason": reason, "verified": verified,
                        "landed": landed, "deployed": deployed, "live": live, "commits": commits}
        if landed is False:
            unlanded.append(tid)
        if live is True:
            available.append(tid)

    counts = {
        "done": len(records),
        "verified": sum(1 for r in records.values() if r["verified"]),
        "landed": sum(1 for r in records.values() if r["landed"] is True),
        "landed_unmeasured": sum(1 for r in records.values() if r["landed"] is None),
        "unlanded": len(unlanded),
        "deployed": sum(1 for r in records.values() if r["deployed"] is True),
        "live": len(available),
    }
    return {
        "records": records, "counts": counts, "measured": measured, "notes": notes,
        "unlanded": unlanded, "available": available,
        # A live reading rests on a sha the CALLER supplied; only the caller can probe the box.
        # Allowed, never silent.
        "live_attested": False,
        "live_identity": {"source": "caller-supplied ?served" if served_sha else None,
                          "sha": served_sha or None},
        "integration_ref": ref,
    }
