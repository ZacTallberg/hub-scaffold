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
from django.views.decorators.csrf import csrf_exempt

from hub_core import ids, validate
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
    w._hub_token_gated = True   # marker the route-guard audit adapter asserts on every /hub/api/ route
    return w


def _evidence_problem(ev):
    """Return None if the evidence string dereferences to something real, else the reason it
    doesn't. Accepted forms: http(s) URL (status <400), a commit sha in this repo, or an existing
    repo-relative file path. 'done' evidence that nothing can resolve is decoration, not evidence."""
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
    return "not a resolvable URL, commit sha, or existing repo path"


def _append(type_, eid, payload, *, expected_version, agent, idem, etype):
    """Validate the MERGED entity, then append. Returns (response_dict, http_status)."""
    s = hub_app.store()
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
    if not eid:
        return JsonResponse({"errors": [{"code": "missing_id"}]}, status=400)
    # Doctrine: exactly one agent OWNS a task before completing it. Require a held, valid lease
    # (claim first) — not just "not held by someone else".
    cur = hub_app._read_lease(eid)
    if not cur:
        return JsonResponse({"errors": [{"code": "must_claim", "msg": "claim the task first (POST /hub/api/claim)"}]}, status=409)
    if not hub_app.lease_valid(eid, token):
        return JsonResponse({"errors": [{"code": "lease", "msg": "claimed by another agent / stale token"}]}, status=409)
    evidence = b.get("evidence_uri") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    accept = b.get("accept_note")
    if not accept or not evidence:
        return JsonResponse({"errors": [{"code": "need_evidence", "msg": "accept_note + >=1 evidence_uri required"}]}, status=422)
    # FALSE-GREEN GUARD: evidence must DEREFERENCE — a string nothing can resolve is not evidence.
    bad = {}
    for e in evidence:
        problem = _evidence_problem(e)
        if problem:
            bad[str(e)[:200]] = problem
    if bad:
        return JsonResponse({"errors": [{"code": "evidence_unresolvable",
            "msg": "every evidence_uri must dereference (URL <400 / commit in repo / existing repo path)",
            "bad": bad}]}, status=422)
    ent = hub_app.current_state().get("entities", {}).get(eid)
    if not ent:
        return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
    # FALSE-GREEN GUARD: an unverifiable task cannot be completed — the server runs the task's own
    # verification_command; absence is a 422, not a free pass (that skip was the audit's residual gap).
    vc = ent.get("verification_command")
    if not vc:
        return JsonResponse({"errors": [{"code": "need_verification_command",
            "msg": "done requires a verification_command on the task; set it (POST /hub/api/task) before completing"}]},
            status=422)
    if vc:
        try:
            r = subprocess.run(vc, shell=True, cwd=str(hub_app.BASE_DIR), capture_output=True, text=True, timeout=300)
        except Exception as e:
            return JsonResponse({"errors": [{"code": "verify_error", "msg": str(e)}]}, status=422)
        if r.returncode != 0:
            return JsonResponse({"errors": [{"code": "verify_failed", "exit": r.returncode, "stderr": (r.stderr or "")[-500:]}]}, status=422)
    # FALSE-GREEN GUARD: recompute the audit server-side at completion time and refuse to grant
    # 'done' while the hub itself is in an unsound state (critical violations: broken chain, schema
    # corruption). coherence:repo (pre-deploy) is excluded — it is resolved by deploying, not by a task.
    audit = hub_app.run_audit()
    blocking = [v for v in audit.get("violations", []) if v.get("severity") == "critical"]
    if blocking:
        return JsonResponse({"errors": [{"code": "audit_unsound", "msg": "hub audit has CRITICAL violations; resolve before completing",
            "violations": [{"id": v.get("id"), "observed": v.get("observed")} for v in blocking[:5]]}]}, status=422)
    payload = {"type": "task", "status": "done", "verified_by": b.get("verified_by") or [accept], "evidence_uri": evidence}
    # OCC: the held lease already guarantees sole ownership, so default expected_version to the
    # version we just read (still catches a concurrent write between read and append).
    exp = b.get("expected_version") if b.get("expected_version") is not None else ent.get("version")
    resp, status = _append("task", eid, payload, expected_version=exp, agent=agent,
                           idem=b.get("idem_key"), etype="task.transitioned")
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
    ev = hub_app.store().append(
        aggregate=f"{hub_app.PROJECT_KEY}:decision:{idem[-12:]}", type="decision.logged",
        payload={"topic": topic, "choice": choice, "rationale": b.get("rationale"),
                 "invalidates": b.get("invalidates", []), "refs": b.get("refs", [])},
        expected_version=None, agent_id=agent, git_sha=hub_app._git_head(), idem_key=idem)
    return JsonResponse({"data": {"event": ev["event_id"]}})


@writer
def claim(request, b):
    eid, agent = b.get("id"), b.get("agent")
    if not eid or not agent:
        return JsonResponse({"errors": [{"code": "need_id_agent"}]}, status=400)
    res = hub_app.claim(eid, agent, ttl_s=int(b.get("ttl_s", 900)))
    return JsonResponse(res, status=200 if res["ok"] else 409)


@writer
def heartbeat(request, b):
    res = hub_app.heartbeat(b.get("id"), b.get("token"), ttl_s=int(b.get("ttl_s", 900)))
    return JsonResponse(res, status=200 if res["ok"] else 409)
