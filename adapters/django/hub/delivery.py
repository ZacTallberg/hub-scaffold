"""Where completed work actually is, from durable release evidence.

``done``, ``landed``, ``deployed``, and ``live`` remain separate claims.  Production delivery does
not depend on a ``.git`` directory: a post-canary deploy record names the exact shipped SHA, the
exact SHA observed at the front door, and the task ids that release closes.  When those SHAs match,
the running artifact's pre-build stamp can prove the named tasks live with no ancestry subprocess.

Git ancestry remains useful in a source checkout and for legacy deploy records that predate
``tasks_closed``.  It is optional corroboration, never a prerequisite for production truth.
"""
import subprocess

from . import hub_app


_ABSENT_DEFAULT = {
    "landing": "no repository or exact release closure establishes landing in this context",
    "release": "no coherent deploy closure names this task",
    "live": "the running artifact has no comparable pre-build SHA",
    "verified": "no substantive completion result and evidence are recorded on this task",
}


def _git(*args):
    """Run a Git command in the repo root, returning ``None`` when it cannot answer."""
    try:
        out = subprocess.run(("git",) + args, cwd=str(hub_app.WORK_ROOT), capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _repo_present():
    return _git("rev-parse", "--git-dir") is not None


def repository_available():
    """Cheap hot-path hint for whether optional ancestry enrichment is worth dispatching."""
    return (hub_app.WORK_ROOT / ".git").exists()


def _integration_ref():
    return hub_app._dj_setting("HUB_INTEGRATION_REF", None) or "HEAD"


def _commits_of(task):
    return [str(c).strip() for c in ((task.get("provenance") or {}).get("commits") or [])
            if str(c).strip()]


def _completion_proof(task):
    """Return the task's accepted-operation proof and, when absent, the exact reason.

    Every legitimate ``done`` transition already requires a substantive ``verified_by`` result
    plus evidence. That is the proof for ordinary work. A task opts into one additional boundary
    only by declaring ``verification_command``; then its matching exit-0 transient receipt is also
    required. Absence of a test command is therefore never rendered as an ordinary task failing
    verification.
    """
    results = task.get("verified_by")
    evidence = task.get("evidence_uri")
    has_results = isinstance(results, list) and bool(results) and all(
        isinstance(value, str) and value.strip() for value in results)
    has_evidence = isinstance(evidence, list) and bool(evidence) and all(
        isinstance(value, str) and value.strip() for value in evidence)
    if not has_results or not has_evidence:
        return False, _ABSENT_DEFAULT["verified"]

    command = str(task.get("verification_command") or "").strip()
    if not command:
        return True, None
    runs = task.get("verification_run") or []
    if isinstance(runs, dict):
        runs = [runs]
    passed = any(isinstance(run, dict) and run.get("exit_code") == 0
                 and str(run.get("command") or "") == command for run in runs)
    if passed:
        return True, None
    return False, "declared critical probe has no matching exit-0 transient receipt"


def _sha(value):
    return hub_app._normalize_build_sha(value) or ""


def _is_ancestor(commit, ref):
    """True/False when Git answered, ``None`` when the commit or repository is unavailable."""
    if _git("cat-file", "-e", "%s^{commit}" % commit) is None:
        return None
    try:
        out = subprocess.run(("git", "merge-base", "--is-ancestor", commit, ref),
                             cwd=str(hub_app.WORK_ROOT), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None


def _release_rows(state):
    """Normalize deploy records without promoting an incoherent canary to release evidence."""
    rows = []
    for deploy in state.get("by_type", {}).get("deploy", []):
        tasks = deploy.get("tasks_closed")
        explicit = isinstance(tasks, list)
        shipped, observed = _sha(deploy.get("sha")), _sha(deploy.get("served_sha"))
        rows.append({
            "id": deploy.get("id"),
            "at": deploy.get("at"),
            "sha": shipped,
            "served_sha": observed,
            "tasks": set(str(task) for task in tasks) if explicit else set(),
            "explicit": explicit,
            "coherent": bool(explicit and shipped and observed and shipped == observed),
        })
    return rows


def _live_identity(served=None):
    """Identify the code answering this request without putting Git on the live path."""
    artifact = _sha(hub_app._running_sha())
    caller = _sha(served)
    conflict = bool(artifact and caller and artifact != caller)
    running = artifact or caller
    if artifact:
        source = "artifact build stamp"
    elif caller:
        source = "caller-supplied ?served fallback"
    else:
        source = None
    return {"sha": running or None, "artifact_sha": artifact or None,
            "caller_sha": caller or None, "source": source, "conflict": conflict}


def _headline(record, ref):
    if record["landed"] is False:
        return "unlanded", ("recorded commit is not an ancestor of %s — done on the board, "
                            "not on the integration branch" % ref)
    if record["live"] is True:
        return "live", "named by the deploy closure carried by this running artifact"
    if record["deployed"] is True:
        return "deployed", "named by an exact post-canary deploy closure"
    if record["landed"] is True:
        return "landed", "on the integration branch"
    if record["deployed"] is False:
        reason = "not named by any coherent deploy closure"
    else:
        reason = None
    if record["verified"]:
        return "verified", reason or _ABSENT_DEFAULT["landing"]
    proof_reason = record.get("proof_reason") or _ABSENT_DEFAULT["verified"]
    if record["commits"]:
        return "unverified", proof_reason
    return "unverified", proof_reason or reason or (
        "no commit recorded on this task, so downstream ancestry cannot be asked")


def _finish(records, identity, ref, release_rows):
    unlanded = [task for task, row in records.items() if row["landed"] is False]
    available = [task for task, row in records.items() if row["live"] is True]
    for record in records.values():
        record["state"], record["state_reason"] = _headline(record, ref)

    any_explicit = any(row["explicit"] for row in release_rows)
    any_coherent = any(row["coherent"] for row in release_rows)
    landing_complete = bool(records) and all(r["landed"] is not None for r in records.values())
    release_complete = bool(records) and all(r["deployed"] is not None for r in records.values())
    live_complete = bool(records) and all(r["live"] is not None for r in records.values())
    notes = {
        "verified": None,
        "landing": None if landing_complete else _ABSENT_DEFAULT["landing"],
        "release": ("deploy records exist but none has matching sha/served_sha"
                    if any_explicit and not any_coherent else
                    (None if release_complete else _ABSENT_DEFAULT["release"])),
        "live": ("caller-observed SHA conflicts with this artifact's build stamp"
                 if identity["conflict"] else
                 (None if live_complete else _ABSENT_DEFAULT["live"])),
    }
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
        "records": records, "counts": counts,
        "measured": {"verified": True, "landing": landing_complete,
                     "release": release_complete, "live": live_complete},
        "notes": notes, "unlanded": unlanded, "available": available,
        # A coherent deploy's served_sha is the durable receipt from the independent canary.
        "live_attested": bool(identity["sha"] and not identity["conflict"] and any(
            row["coherent"] and row["sha"] == identity["sha"] for row in release_rows)),
        "live_identity": identity,
        "integration_ref": ref,
    }


def direct_block(state, served=None):
    """Fast production projection using only entities plus the artifact build stamp."""
    tasks = state.get("by_type", {}).get("task", [])
    releases = _release_rows(state)
    identity = _live_identity(served)
    coherent = [row for row in releases if row["coherent"]]
    # An explicit but failed canary is not a release and cannot turn unknown delivery into a
    # measured negative. Once one coherent closure exists, absence from every carried-task set is
    # meaningful because modern closures name the complete already-done set in that release.
    explicit_protocol = bool(coherent)
    records = {}

    for task in tasks:
        if (task.get("status") or "").lower() != "done":
            continue
        tid = task["id"]
        verified, proof_reason = _completion_proof(task)
        commits = _commits_of(task)
        closures = [row for row in coherent if tid in row["tasks"]]
        deployed = True if closures else (False if explicit_protocol else None)
        landed = True if deployed is True else None  # a coherent downstream closure implies landing
        if not identity["sha"]:
            live = None
        elif identity["conflict"]:
            live = False
        elif closures:
            live = any(row["sha"] == identity["sha"] for row in closures)
        else:
            live = False if explicit_protocol else None
        records[tid] = {
            "verified": verified, "proof_reason": proof_reason,
            "landed": landed, "deployed": deployed,
            "live": live, "commits": commits,
            "release_sha": (max(closures, key=lambda row: str(row.get("at") or ""))["sha"]
                            if closures else None),
        }
    return _finish(records, identity, _integration_ref(), releases)


def block(state, served=None):
    """Delivery projection enriched with optional Git ancestry for source/legacy contexts."""
    result = direct_block(state, served=served)
    if not _repo_present():
        return result

    ref = _integration_ref()
    deploys = state.get("by_type", {}).get("deploy", [])
    deploy_shas = [_sha(row.get("sha")) for row in deploys if _sha(row.get("sha"))]
    releases = _release_rows(state)
    tasks = {task["id"]: task for task in state.get("by_type", {}).get("task", [])}
    identity = result["live_identity"]

    for tid, record in result["records"].items():
        commits = _commits_of(tasks.get(tid, {}))
        if commits and record["landed"] is not True:
            answers = [_is_ancestor(commit, ref) for commit in commits]
            if any(answer is True for answer in answers):
                record["landed"] = True
            elif answers and all(answer is False for answer in answers):
                record["landed"] = False

        # Legacy records had no tasks_closed. Preserve their ancestry-based answer without
        # overriding a modern explicit closure's measured negative.
        if record["deployed"] is None and record["landed"] is True and deploy_shas:
            answers = [_is_ancestor(commit, sha) for commit in commits for sha in deploy_shas]
            if any(answer is True for answer in answers):
                record["deployed"] = True
            elif answers and all(answer is False for answer in answers):
                record["deployed"] = False
        if (record["live"] is None and record["deployed"] is True and identity["sha"] and commits
                and not identity["conflict"]):
            answers = [_is_ancestor(commit, identity["sha"]) for commit in commits]
            if any(answer is True for answer in answers):
                record["live"] = True
            elif answers and all(answer is False for answer in answers):
                record["live"] = False

    return _finish(result["records"], identity, ref, releases)
