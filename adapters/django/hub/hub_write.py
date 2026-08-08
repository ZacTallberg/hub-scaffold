"""Hub typed WRITE API. Token-gated (X-Write-Token header), OCC + idempotent,
validated-before-append. The agent's discover->claim->implement->record->verify loop runs over
these. NOT session/login gated (the agent uses a header token). Fail-closed if no token configured.
"""
import hashlib
import hmac
import json
import os
import subprocess
from functools import wraps

from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from hub_core import ids, validate
from hub_core.process_lock import ProcessFileLock
from hub_core.store import ConflictError

from . import hub_app


def _token_ok(request) -> bool:
    want = hub_app._dj_setting("HUB_WRITE_TOKEN") or os.environ.get("HUB_WRITE_TOKEN")
    if not want:
        return False  # fail-closed: writes disabled until a token is configured
    # header only (NOT ?token= — query params leak into access logs/referers); constant-time compare.
    got = request.headers.get("X-Write-Token") or ""
    return bool(got) and hmac.compare_digest(got, want)


def _body(request):
    try:
        return json.loads((request.body or b"").decode("utf-8") or "{}")
    except Exception:
        return None


def writer(fn):
    @csrf_exempt
    @wraps(fn)
    def w(request, *a, **k):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not _token_ok(request):
            return JsonResponse({"errors": [{"code": "forbidden", "msg": "missing/invalid X-Write-Token"}]}, status=403)
        b = _body(request)
        if b is None:
            return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
        return fn(request, b, *a, **k)
    # Marker asserted on every general mutation route; the launch mint has its own narrow marker.
    w._hub_token_gated = True
    return w


def _evidence_problem(ev):
    """Return None if the evidence string dereferences to something real, else the reason it
    doesn't. Accepted forms: http(s) URL (status <400), a commit sha in this repo, or an existing
    file path resolved from BASE_DIR. This proves existence, not confinement: the general write
    token is already command-execution-grade. 'done' evidence that cannot resolve is decoration."""
    import re
    import urllib.request

    ev = (ev or "").strip()
    if not ev:
        return "empty"
    if ev.startswith(("http://", "https://")):
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(ev, method=method,
                                             headers={"User-Agent": "Mozilla/5.0 (hub-evidence)"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status < 400:
                        return None
            except Exception as e:
                err = str(e)[:120]
        return f"URL did not resolve (<400): {err}"
    if re.fullmatch(r"[0-9a-f]{7,40}", ev):
        try:
            r = subprocess.run(["git", "-C", str(hub_app.BASE_DIR), "cat-file", "-e", ev + "^{commit}"],
                               capture_output=True, timeout=10)
            return None if r.returncode == 0 else "not a commit in this repo"
        except Exception as e:
            return str(e)[:120]
    try:
        if (hub_app.BASE_DIR / ev).exists():
            return None
    except OSError:
        pass
    return "not a resolvable URL, commit sha, or existing path from BASE_DIR"


def _append_with_store(s, type_, eid, payload, *, expected_version, agent, idem, etype):
    """Validate the MERGED entity, then append. Returns (response_dict, http_status)."""
    state = hub_app.current_state(s)
    existing = state["entities"].get(eid, {})
    # OCC: updating an existing entity REQUIRES expected_version (else concurrent writes lose).
    # None is allowed only on first-create. (store.py also skips its head check on None.)
    if existing and expected_version is None:
        return ({"errors": [{"code": "precondition_required",
            "msg": "expected_version required to update an existing entity (optimistic concurrency)",
            "current": existing.get("version")}]}, 428)
    merged = {**existing, **payload, "id": eid, "type": type_}
    merged["version"] = (existing.get("version", 0) + 1) if existing else 1
    errs = validate(merged, type_, hub_app.registry())
    if errs:
        return ({"errors": [{"code": "schema", "msg": e} for e in errs]}, 422)
    try:
        ev = s.append(aggregate=eid, type=etype, payload=payload, expected_version=expected_version,
                      agent_id=agent, git_sha=hub_app._git_head(), idem_key=idem)
    except ConflictError as c:
        return ({"errors": [{"code": "conflict", "expected": c.expected, "current": c.current}]}, 409)
    return ({"data": {"id": eid, "version": ev["result_version"], "event": ev["event_id"]}}, 200)


def _append(type_, eid, payload, *, expected_version, agent, idem, etype):
    """Append using a request-owned store and always release its database handle."""
    s = hub_app.store()
    try:
        return _append_with_store(s, type_, eid, payload, expected_version=expected_version,
                                  agent=agent, idem=idem, etype=etype)
    finally:
        s.close()


@writer
def task(request, b):
    agent = b.get("agent", "agent")
    is_create = not b.get("id")
    # FALSE-GREEN GUARD: 'done' is a terminal transition granted ONLY by complete()
    # (evidence + verification_command + recomputed-audit gated). The generic upsert must
    # never mint a 'done' — that was the bypass an adversarial audit found.
    if (b.get("status") or "").lower() == "done":
        return JsonResponse({"errors": [{"code": "use_complete",
            "msg": "status 'done' must go through POST /hub/api/complete (evidence + verify + audit gated)"}]},
            status=409)
    if is_create:
        state = hub_app.current_state()
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "task")
        b.setdefault("status", "todo")
    else:
        eid = b["id"]
    payload = {k: v for k, v in b.items() if k not in ("agent", "expected_version", "idem_key")}
    payload["type"] = "task"
    resp, status = _append("task", eid, payload, expected_version=b.get("expected_version"), agent=agent,
                           idem=b.get("idem_key"), etype="task.created" if is_create else "task.updated")
    return JsonResponse(resp, status=status)


@writer
def complete(request, b):
    eid, token, agent = b.get("id"), b.get("token"), b.get("agent", "agent")
    if not isinstance(eid, str) or not eid.strip():
        return JsonResponse({"errors": [{"code": "missing_id"}]}, status=400)
    # Doctrine: exactly one agent OWNS a task before completing it. Require a held, valid lease
    # (claim first) — not just "not held by someone else".
    cur = hub_app._read_lease(eid)
    if not cur:
        return JsonResponse({"errors": [{"code": "must_claim", "msg": "claim the task first (POST /hub/api/claim)"}]}, status=409)
    if not hub_app.lease_valid(eid, token):
        return JsonResponse({"errors": [{"code": "lease", "msg": "claimed by another agent / stale token"}]}, status=409)
    evidence = b.get("evidence_uri")
    if isinstance(evidence, str):
        evidence = [evidence]
    accept = b.get("accept_note")
    if (not isinstance(accept, str) or not accept.strip() or not isinstance(evidence, list) or
            not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence)):
        return JsonResponse({"errors": [{"code": "need_evidence",
            "msg": "non-empty accept_note + >=1 non-empty string evidence_uri required"}]}, status=422)
    # HUB_DONE_STRICTNESS is the flow-vs-proof dial (settings; default "tracked"):
    #   "tracked" — done always carries WHO/WHAT/EVIDENCE (lease + accept_note + evidence), but
    #               evidence may be anything non-empty (auth-walled ticket links are fine) and a
    #               verification_command is optional (still RUNS when present).
    #   "strict"  — evidence must dereference and a verification_command is required. For
    #               environments where completions cannot be taken on trust (e.g. autonomous
    #               agents — the mode this hub's origin system runs).
    strict = str(hub_app._dj_setting("HUB_DONE_STRICTNESS", "tracked")).lower() == "strict"
    if strict:
        # FALSE-GREEN GUARD: evidence must DEREFERENCE — a string nothing can resolve is not evidence.
        bad = {}
        for e in evidence:
            problem = _evidence_problem(e)
            if problem:
                bad[str(e)[:200]] = problem
        if bad:
            return JsonResponse({"errors": [{"code": "evidence_unresolvable",
                "msg": "every evidence_uri must dereference (URL <400 / commit in repo / existing path from BASE_DIR)",
                "bad": bad}]}, status=422)
    ent = hub_app.current_state().get("entities", {}).get(eid)
    if not ent:
        return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
    verified_version = ent.get("version")
    if b.get("expected_version") is not None and b.get("expected_version") != verified_version:
        return JsonResponse({"errors": [{"code": "conflict", "current_version": verified_version}]}, status=409)
    # THE HUB NEVER EXECUTES THE VERIFICATION COMMAND. It used to: `subprocess.run(vc, shell=True)`
    # right here, on completion. `verification_command` is caller-authored text, so that made the
    # write token equivalent to arbitrary shell on the machine serving this hub — a remote code
    # execution path reachable by anyone who could write a task. Removed under the RCE ruling.
    #
    # THE RECEIPT GATE replaces it, and is strictly stronger evidence: the WORKER runs the command
    # out-of-band and submits a typed receipt of what happened. The hub validates the receipt it is
    # handed; it never becomes the thing that runs untrusted strings.
    #
    #   verification_run: {command, exit_code, output_sha256, ran_by}
    #
    # Bound so a receipt cannot be borrowed or faked into place: `command` must match the task's own
    # verification_command, `exit_code` must be 0, and `ran_by` must be the completing agent.
    verification_receipt = []
    vc = ent.get("verification_command")
    if strict and not vc:
        return JsonResponse({"errors": [{"code": "need_verification_command",
            "msg": "done requires a verification_command on the task; set it (POST /hub/api/task) before completing"}]},
            status=422)
    if vc:
        run = b.get("verification_run") or {}
        if not isinstance(run, dict) or not run:
            return JsonResponse({"errors": [{"code": "need_verification_run",
                "msg": "done requires a typed verification_run receipt {command, exit_code, "
                       "output_sha256, ran_by}. Run the task's verification_command YOURSELF and "
                       "submit what happened — the hub does not run it for you (it would be "
                       "executing caller-supplied text on the server)."}]}, status=422)
        problems = []
        if " ".join(str(run.get("command") or "").split()) != " ".join(vc.split()):
            problems.append("verification_run.command must be the task's own verification_command "
                            f"({vc!r}), not {run.get('command')!r}")
        if run.get("exit_code") != 0:
            problems.append(f"exit_code is {run.get('exit_code')!r}; only 0 grants done")
        if run.get("ran_by") and b.get("agent") and run["ran_by"] != b.get("agent"):
            problems.append(f"ran_by {run['ran_by']!r} is not the completing agent {b.get('agent')!r}")
        if problems:
            return JsonResponse({"errors": [{"code": "bad_verification_run",
                                             "problems": problems}]}, status=422)
        verification_receipt = [run]
    # FALSE-GREEN GUARD: recompute the audit server-side at completion time and refuse to grant
    # 'done' while the hub itself is in an unsound state (critical violations: broken chain, schema
    # corruption). coherence:repo (pre-deploy) is excluded — it is resolved by deploying, not by a task.
    audit = hub_app.run_audit()
    blocking = [v for v in audit.get("violations", []) if v.get("severity") == "critical"]
    if blocking:
        return JsonResponse({"errors": [{"code": "audit_unsound", "msg": "hub audit has CRITICAL violations; resolve before completing",
            "violations": [{"id": v.get("id"), "observed": v.get("observed")} for v in blocking[:5]]}]}, status=422)
    payload = {"type": "task", "status": "done", "verified_by": b.get("verified_by") or [accept],
               "evidence_uri": evidence}
    # The receipt is what makes this completion falsifiable later — it rides the appended event.
    if verification_receipt:
        payload["verification_run"] = verification_receipt
    # Fence the final append against an expiry/reclaim race. Verification can take minutes, so the
    # global lease lock is deliberately acquired only for this short commit section. The original
    # entity version binds the result to exactly the task definition that was verified.
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        if not hub_app.lease_valid(eid, token):
            return JsonResponse({"errors": [{"code": "lease", "msg": "lease expired or was reclaimed during verification"}]}, status=409)
        resp, status = _append("task", eid, payload, expected_version=verified_version, agent=agent,
                               idem=b.get("idem_key"), etype="task.transitioned")
        if status == 200:
            hub_app.release_lease(eid, token)
    return JsonResponse(resp, status=status)


@writer
def adr(request, b):
    agent = b.get("agent", "agent")
    state = hub_app.current_state()
    if not b.get("id"):
        nums = [a.get("number", 0) for a in state["by_type"].get("adr", [])]
        num = (max(nums) + 1) if nums else 1
        eid = ids.make_id(hub_app.PROJECT_KEY, "adr", f"{num:04d}")
        b.setdefault("number", num)
    else:
        eid = b["id"]
        # Doctrine: an Accepted ADR is IMMUTABLE — context/decision can't be rewritten; evolve via
        # amendments_md or supersession only. Block edits to the frozen prose post-accept.
        prev = state["entities"].get(eid)
        if prev and prev.get("status") in ("accepted", "superseded", "deprecated"):
            if any(k in b and b[k] != prev.get(k) for k in ("context_md", "decision_md")):
                return JsonResponse({"errors": [{"code": "adr_immutable",
                    "msg": "accepted ADR context/decision is immutable — add amendments_md or supersede instead"}]},
                    status=409)
    payload = {k: v for k, v in b.items() if k not in ("agent", "expected_version", "idem_key")}
    payload["type"] = "adr"
    resp, status = _append("adr", eid, payload, expected_version=b.get("expected_version"), agent=agent,
                           idem=b.get("idem_key"), etype="adr.upserted")
    return JsonResponse(resp, status=status)


@writer
def capability(request, b):
    agent = b.get("agent", "agent")
    name = b.get("name")
    if not name:
        return JsonResponse({"errors": [{"code": "need_name"}]}, status=400)
    local = b.get("local") or "".join(c if c.isalnum() or c in "._-" else "-" for c in name.lower())
    eid = ids.make_id(hub_app.PROJECT_KEY, "cap", local)
    payload = {k: v for k, v in b.items() if k not in ("agent", "expected_version", "idem_key", "local")}
    payload["type"] = "cap"
    resp, status = _append("cap", eid, payload, expected_version=b.get("expected_version"), agent=agent,
                           idem=b.get("idem_key"), etype="capability.registered")
    return JsonResponse(resp, status=status)


@writer
def decision(request, b):
    agent = b.get("agent", "agent")
    topic, choice = b.get("topic"), b.get("choice")
    if not topic or not choice:
        return JsonResponse({"errors": [{"code": "need_topic_choice"}]}, status=400)
    idem = "decision:" + hashlib.sha256((topic + choice).encode("utf-8")).hexdigest()[:16]
    s = hub_app.store()
    try:
        ev = s.append(
            aggregate=f"{hub_app.PROJECT_KEY}:decision:{idem[-12:]}", type="decision.logged",
            payload={"topic": topic, "choice": choice, "rationale": b.get("rationale"),
                     "invalidates": b.get("invalidates", []), "refs": b.get("refs", [])},
            expected_version=None, agent_id=agent, git_sha=hub_app._git_head(), idem_key=idem)
    finally:
        s.close()
    return JsonResponse({"data": {"event": ev["event_id"]}})


@writer
def claim(request, b):
    eid, agent = b.get("id"), b.get("agent")
    if (not isinstance(eid, str) or not eid.strip() or
            not isinstance(agent, str) or not agent.strip() or len(agent) > 256):
        return JsonResponse({"errors": [{"code": "need_id_agent"}]}, status=400)
    try:
        ttl = int(b.get("ttl_s", 900))
    except (TypeError, ValueError):
        ttl = 0
    if ttl < 1 or ttl > 86400:
        return JsonResponse({"errors": [{"code": "bad_ttl", "msg": "ttl_s must be 1..86400"}]}, status=422)
    # Serialize lease acquisition and the projected status transition as one claim flow. The
    # underlying helpers use this same re-entrant process/file lock, so another server process
    # cannot observe a newly granted lease and still race a second todo->in_progress transition.
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        state = hub_app.current_state()
        ent = state.get("entities", {}).get(eid)
        if not ent or ent.get("type") != "task":
            return JsonResponse({"errors": [{"code": "not_found", "msg": "task does not exist"}]}, status=404)
        status = ent.get("status")
        flags = state.get("flags", {}).get(eid, {})
        if flags.get("deps_unmet"):
            return JsonResponse({"errors": [{"code": "deps_blocked", "msg": "task dependencies are not done",
                                             "deps_unmet": flags.get("deps_unmet")}]}, status=409)
        if status not in ("todo", "in_progress"):
            return JsonResponse({"errors": [{"code": "not_claimable",
                                             "msg": "only todo or in_progress tasks can be claimed"}]}, status=409)
        res = hub_app.claim(eid, agent, ttl_s=ttl)
        if not res["ok"]:
            return JsonResponse(res, status=409)
        if status != "in_progress":
            transition, transition_status = _append(
                "task", eid, {"type": "task", "status": "in_progress"},
                expected_version=ent.get("version"), agent=agent,
                idem=b.get("idem_key"), etype="task.transitioned",
            )
            if transition_status != 200:
                hub_app.release_lease(eid, res["token"])
                return JsonResponse(transition, status=transition_status)
            res["version"] = transition["data"]["version"]
        else:
            res["version"] = ent.get("version")
    return JsonResponse(res, status=200 if res["ok"] else 409)


@writer
def heartbeat(request, b):
    if (not isinstance(b.get("id"), str) or not b.get("id").strip() or
            not isinstance(b.get("token"), str) or not b.get("token").strip()):
        return JsonResponse({"errors": [{"code": "need_id_token"}]}, status=400)
    try:
        ttl = int(b.get("ttl_s", 900))
    except (TypeError, ValueError):
        ttl = 0
    if ttl < 1 or ttl > 86400:
        return JsonResponse({"errors": [{"code": "bad_ttl", "msg": "ttl_s must be 1..86400"}]}, status=422)
    res = hub_app.heartbeat(b.get("id"), b.get("token"), ttl_s=ttl)
    return JsonResponse(res, status=200 if res["ok"] else 409)


@csrf_protect
def launch_grant(request):
    """Mint one narrow browser capability without exposing the general Hub write token.

    Same-origin CSRF protection prevents a foreign page from reading or minting a usable grant.
    The grant is short-lived, signed, and bound to action/task/count; the workstation must still
    consume it at the token-gated issuing Hub before any process starts.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not hub_app.worker_launch_enabled():
        return JsonResponse({"errors": [{"code": "launch_disabled",
                                         "msg": "worker launch is not enabled on this Hub"}]}, status=404)
    b = _body(request)
    if b is None:
        return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
    from django.urls import reverse
    from hub_core import launch_grant as grants

    action = b.get("action") or "start"
    task_id = b.get("task") or ""
    if not isinstance(task_id, str) or len(task_id) > 256:
        return JsonResponse({"errors": [{"code": "bad_grant_request",
                                         "msg": "task must be a string of at most 256 characters"}]}, status=422)
    try:
        count = int(b.get("count") or 1)
        ttl = int(hub_app._dj_setting("HUB_WORKER_GRANT_TTL_S", grants.DEFAULT_TTL_S))
        configured = str(hub_app._dj_setting("HUB_WORKER_LAUNCH_ISSUER_URL", "") or "").strip()
        issuer = configured or request.build_absolute_uri(reverse("hub:consume-launch-grant"))
        with grants.using_hub_dir(hub_app.HUB_DIR):
            grant = grants.mint(action=action, task=task_id, count=count, ttl_s=ttl, issuer=issuer)
    except (TypeError, ValueError) as exc:
        return JsonResponse({"errors": [{"code": "bad_grant_request", "msg": str(exc)}]}, status=422)
    except OSError:
        return JsonResponse({"errors": [{"code": "launch_unavailable",
                                         "msg": "grant store unavailable"}]}, status=503)
    return JsonResponse({"data": {"grant": grants.encode(grant), "expires": grant["expires"],
                                  "count": grant["count"], "task": grant["task"]}})


# Marker consumed by the computed route audit.  This is the sole non-writer /hub/api capability.
launch_grant._hub_origin_gated = True


@writer
def consume_launch_grant(request, b):
    """Validate and atomically burn a grant at the Hub that issued it."""
    if b.get("consume") is None:
        return JsonResponse({"errors": [{"code": "missing_grant", "msg": "consume is required"}]},
                            status=400)
    from hub_core import launch_grant as grants

    try:
        count = int(b.get("count") or 1)
    except (TypeError, ValueError) as exc:
        return JsonResponse({"errors": [{"code": "bad_grant_request", "msg": str(exc)}]}, status=422)
    with grants.using_hub_dir(hub_app.HUB_DIR):
        ok, detail = grants.consume(b.get("consume"), action=b.get("action") or "start",
                                    task=b.get("task") or "", count=count)
    if not ok:
        return JsonResponse({"errors": [{"code": "launch_refused", "msg": str(detail)}]}, status=403)
    return JsonResponse({"data": {"authorized": True, "count": int(detail)}})
