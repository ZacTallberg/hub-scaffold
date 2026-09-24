"""The promotion lane's routes: hold, promote, abandon, and the public queue (hub_core.held).

Every write goes through the ordinary validated append (schema, OCC, realtime publication); the
fetchability check asks the Hub's commit resolver (hub_app.commit_resolver), whose "could not
ask" is never read as "not pushed".
"""
from __future__ import annotations

import time

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from hub_core import held as _held
from hub_core import ids

from . import hub_app
from .hub_write import _append, writer


def _eid(repo: str, sha: str) -> str:
    return ids.make_id(hub_app.PROJECT_KEY, "held", _held.local_id(repo, sha))


def _refuse(code: str, msg: str, status: int = 422):
    return JsonResponse({"errors": [{"code": code, "msg": msg}]}, status=status)


def _project_key(resolver, repo: str) -> str:
    """The configured project a repo path names: the whole path, or its last segment."""
    full = repo.lower()
    last = full.rsplit("/", 1)[-1]
    known = set(resolver.projects())
    return full if full in known else (last if last in known else last)


@writer(scope="held:write")
def hold(request, b):
    """POST /hub/api/held -- record a commit held back from live, refusing one nobody can fetch.

    Body: repo, sha, reason, rebuild; optional branch, from_gap, title, attested (the client ran
    ``git branch -r --contains`` in the repository), unpushed_reason + local_path (the recorded
    exception for a commit on no remote)."""
    fields, error = _held.validate_hold(b)
    if error:
        return _refuse(*error)
    resolver = hub_app.commit_resolver()
    confirmed, searched = resolver.has(fields["sha"], _project_key(resolver, fields["repo"]))
    attested = b.get("attested") is True
    unpushed_reason = str(b.get("unpushed_reason") or "").strip()
    reachable, error = _held.reachability(attested, confirmed, unpushed_reason)
    if error:
        code, msg = error
        return JsonResponse({"errors": [{"code": code, "msg": msg, "searched": searched}]},
                            status=422)
    repo, sha = fields["repo"], fields["sha"]
    eid = _eid(repo, sha)
    existing = (hub_app.current_state().get("entities") or {}).get(eid)
    short = repo.rsplit("/", 1)[-1]
    payload = {"type": "held", "repo": repo, "sha": sha, "status": "held",
               "reason": fields["reason"], "rebuild": fields["rebuild"],
               "reachable": reachable,
               "title": str(b.get("title") or "").strip()
                        or "%s %s held: %s" % (short, sha[:12], fields["reason"][:80])}
    branch = str(b.get("branch") or "").strip()
    if branch:
        payload["branch"] = branch[:120]
    if fields["from_gap"]:
        payload["from_gap"] = fields["from_gap"]
    if reachable == "unpushed":
        payload["unpushed_reason"] = unpushed_reason[:600]
        payload["local_path"] = str(b.get("local_path") or "").strip()[:300]
        payload["title"] = "%s %s held ON ONE DISK ONLY: %s" % (short, sha[:12],
                                                                fields["reason"][:70])
    resp, status = _append("held", eid, payload,
                           expected_version=(existing or {}).get("version"),
                           agent=b.get("agent") or "agent", idem=b.get("idem_key"),
                           etype="held.recorded")
    if status < 400:
        resp.setdefault("data", {}).update({"reachable": reachable, "searched": searched})
    return JsonResponse(resp, status=status)


def _close(b, status_value, extra, etype):
    repo = str(b.get("repo") or "").strip()
    sha = str(b.get("sha") or "").strip().lower()
    if not _held.REPO_RE.match(repo) or not _held.SHA_RE.match(sha):
        return _refuse("bad_repo_or_sha", "repo (project path) and sha (7-40 hex) are required")
    eid = _eid(repo, sha)
    existing = (hub_app.current_state().get("entities") or {}).get(eid)
    if not existing:
        return _refuse("not_held", "nothing is held for %s in %s" % (sha[:12], repo), 404)
    if existing.get("status") != "held":
        return _refuse("not_open", "%s is already %s" % (sha[:12], existing.get("status")), 409)
    payload = {"type": "held", "status": status_value, **extra}
    resp, status = _append("held", eid, payload, expected_version=existing.get("version"),
                           agent=b.get("agent") or "agent", idem=b.get("idem_key"), etype=etype)
    return JsonResponse(resp, status=status)


@writer(scope="held:write")
def promote(request, b):
    """POST /hub/api/held/promote -- the rebuild ran, here is the proof, it may go live.

    This pushes nothing. It closes the record that kept the work visible, so the queue empties
    only for work that was actually promoted."""
    evidence = str(b.get("evidence") or "").strip()
    if not evidence:
        return _refuse("need_evidence", "a promotion is a claim about production: give the "
                                        "pipeline, sha or URL of the rebuild that ran")
    return _close(b, "promoted", {"promoted_evidence": evidence,
                                  "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               time.gmtime()),
                                  "promoted_note": str(b.get("note") or "").strip()[:600]},
                  "held.promoted")


@writer(scope="held:write")
def abandon(request, b):
    """POST /hub/api/held/abandon -- close a hold without promoting it, stating why."""
    reason = str(b.get("reason") or "").strip()
    if not reason:
        return _refuse("need_reason", "abandoning held work is a decision; state its reason")
    return _close(b, "abandoned", {"abandoned_reason": reason[:600]}, "held.abandoned")


@require_GET
def held_json(request):
    """The queue: what is held, how long it has stood, its urgency, and what each one needs
    before it can go live. Oldest first, because the oldest is the one rotting."""
    now = time.time()
    state = hub_app.current_state()
    open_rows = _held.queue(state, now)
    repo = (request.GET.get("repo") or "").strip()
    if repo:
        open_rows = [r for r in open_rows
                     if r["repo"] == repo or str(r["repo"]).endswith("/" + repo)]
    for row in open_rows:
        row["detail"] = _held.detail(row)
    closed = [e for e in _held.rows(state) if e.get("status") != "held"]
    return JsonResponse({"data": open_rows, "count": len(open_rows),
                         "metadata": {"promoted": sum(1 for e in closed
                                                      if e.get("status") == "promoted"),
                                      "abandoned": sum(1 for e in closed
                                                       if e.get("status") == "abandoned"),
                                      "oldest_s": open_rows[0]["age_s"] if open_rows else 0,
                                      "generated": int(now)}})
