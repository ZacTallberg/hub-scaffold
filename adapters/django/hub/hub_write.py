"""Hub typed WRITE API. Token-gated (X-Write-Token header), OCC + idempotent,
validated-before-append. The agent's discover->claim->implement->record->verify loop runs over
these. NOT session/login gated (the agent uses a header token). Fail-closed if no token configured.
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps

from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from hub_core import agent_auth, collision, flow, ids, offer, schedule, secretscan, validate
from hub_core.process_lock import LockBusy, ProcessFileLock
from hub_core.store import ConflictError

from . import hub_app


def _lock_refusal(fn):
    """A claims-lock wait that runs out answers 503 WITH ITS DIAGNOSIS, never a bare 500.

    The lock's own timeout names the holder pid (alive or dead), the lock file's age and which
    lock ran out (hub_core.process_lock.LockBusy); a caller that receives it can tell a slow live
    holder from a dead one and retry sensibly. The full message (with the server path) goes to
    the operational stream; the caller sees the path-free form."""
    @wraps(fn)
    def w(request, b):
        try:
            return fn(request, b)
        except LockBusy as exc:
            try:
                hub_app.record_error("hub.claims", str(exc), severity="warning",
                                     code="lock_busy",
                                     context={"component": "hub-write",
                                              "path": request.path_info})
            except Exception:                                # noqa: BLE001 - never mask the refusal
                pass
            response = JsonResponse({"errors": [{"code": "lock_busy", "msg": exc.public,
                                                 "retry_after_s": 2}]}, status=503)
            response["Retry-After"] = "2"
            return response
    return w


_AUTH = ContextVar("hub_write_auth", default=None)


def _record_refusal(request, code, message):
    """Put a 4xx auth refusal into the operational stream. A worker whose credential was
    revoked or rotated out otherwise retries forever while the board shows nothing but a
    seat going quietly stale — indistinguishable from someone stepping away. Fail-soft:
    telemetry must never be the reason a refusal turns into a 500."""
    try:
        hub_app.record_error(
            "hub.auth", message, severity="warning", code=code,
            context={"component": "hub-write", "path": request.path_info,
                     "method": request.method,
                     "agent": (request.headers.get("X-Hub-Agent") or "")[:120],
                     "machine": (request.headers.get("X-Hub-Machine") or "")[:120]})
    except Exception:                                        # noqa: BLE001
        pass


def _as_bool(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _authenticate(request):
    """Authenticate one immutable subject. Invalid agent auth never falls through to root."""
    agent_token = request.headers.get("X-Agent-Token") or ""
    if agent_token:
        try:
            return agent_auth.CredentialRegistry(hub_app.HUB_DIR).authenticate(agent_token), None
        except agent_auth.CredentialError as exc:
            return None, str(exc)

    configured_compat = hub_app._dj_setting("HUB_SHARED_TOKEN_COMPAT", None)
    if configured_compat is None:
        configured_compat = os.environ.get("HUB_SHARED_TOKEN_COMPAT")
    compat = _as_bool(configured_compat, True)
    want = hub_app._dj_setting("HUB_WRITE_TOKEN") or os.environ.get("HUB_WRITE_TOKEN")
    got = request.headers.get("X-Write-Token") or ""
    if compat and want and got and hmac.compare_digest(got, want):
        return agent_auth.shared_root_context(), None
    return None, "missing/invalid X-Agent-Token (shared-root compatibility unavailable or refused)"


def _body(request):
    try:
        return json.loads((request.body or b"").decode("utf-8") or "{}")
    except Exception:
        return None


def writer(fn=None, *, scope=None):
    if fn is None:
        return lambda view: writer(view, scope=scope)

    @csrf_exempt
    @wraps(fn)
    def w(request, *a, **k):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        auth, problem = _authenticate(request)
        if not auth:
            _record_refusal(request, "auth_refused", "a write was refused: " + str(problem))
            return JsonResponse({"errors": [{"code": "forbidden", "msg": problem}]}, status=403)
        if not auth.allows(scope):
            _record_refusal(request, "insufficient_scope",
                            "a write was refused: subject %r lacks scope %r" % (auth.subject, scope))
            return JsonResponse({"errors": [{"code": "insufficient_scope", "required": scope,
                                              "subject": auth.subject}]}, status=403)
        b = _body(request)
        if not isinstance(b, dict):
            return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
        # SECRET-SHAPE REFUSAL, at the one choke point that covers every current and future
        # writer. The ledger is append-only and hash-chained — a secret written into it can
        # never be removed without destroying the tamper-evidence — and the highest-volume
        # risky behaviour on an agent board is pasting failing command output into a question
        # or note. Refuse at the door, in milliseconds, with instructions.
        shape = secretscan.secret_problem(b)
        if shape:
            return JsonResponse({"errors": [{"code": "secret_shaped_payload",
                "msg": f"refused: the payload looks like it contains {shape}. The hub ledger "
                       f"is append-only and hash-chained — a secret written here can never be "
                       f"removed. Redact it and retry. If a real secret already reached the "
                       f"ledger, ROTATE it; editing the chain is not possible."}]}, status=422)
        requested_agent = b.get("agent")
        if auth.mode == "scoped-agent":
            if requested_agent not in (None, "", auth.subject):
                # State only what is ESTABLISHED: the credential resolved, so it is valid and
                # bound; the payload named someone else. Two readings, most common first --
                # never "your identity is stale, re-enroll", which sends a working seat to
                # rebuild itself and still cannot do what it meant.
                return JsonResponse({"errors": [{"code": "identity",
                    "msg": ("this credential is VALID and bound to %r, but the payload declares "
                            "agent %r. `agent` is WHO IS WRITING, never who the write is about. "
                            "(1) If you meant the RECIPIENT of a task, use POST /hub/api/hand "
                            "with to=%r (client: `hand <task> --to %s`); (2) if this seat is "
                            "configured with the wrong agent name, fix that configuration. "
                            "Writes that omit `agent` land normally."
                            % (auth.subject, requested_agent, requested_agent, requested_agent)),
                    "subject": auth.subject, "declared": requested_agent}]}, status=403)
            b["agent"] = auth.subject
        else:
            # Compatibility keeps legacy seat labels only as labels. The event and lease actor is
            # always the visibly bounded shared-root subject.
            pass
        # OBSERVED PRESENCE rides the authenticated write seam: every write refreshes the
        # caller's seat from the optional X-Hub-Machine/-Session/-Cwd/-Focus headers, so the
        # board knows who is on it without anyone filing a report — and an unauthenticated
        # caller can never forge a seat. The label the write carries (or, for a scoped
        # credential, its immutable subject) names the seat. Fail-soft by construction.
        seat = b.get("agent") if isinstance(b.get("agent"), str) and b.get("agent") else auth.subject
        hub_app.observe_presence(seat, request.headers)
        request.hub_auth = auth
        marker = _AUTH.set(auth)
        try:
            response = fn(request, b, *a, **k)
        finally:
            _AUTH.reset(marker)
        response["X-Hub-Auth-Subject"] = auth.subject
        response["X-Hub-Auth-Mode"] = auth.mode
        response["X-Hub-Credential-Id"] = auth.credential_id
        return response
    # Marker asserted on every general mutation route; the launch mint has its own narrow marker.
    w._hub_token_gated = True
    w._hub_required_scope = scope
    return w


def _event_identity(fallback_agent):
    auth = _AUTH.get()
    if not auth:
        return {"agent_id": fallback_agent, "session_id": None, "actor_kind": "agent"}
    return {"agent_id": auth.subject, "session_id": auth.credential_id,
            "actor_kind": auth.actor_kind}


def _evidence_problem(ev, project: str = "", resolver=None):
    """Return None if the evidence string dereferences to something real, else the reason it
    doesn't. Accepted forms: http(s) URL (status <400), a commit sha in this Hub's repository OR
    in the task's own project (hub_core.commits), or an existing file path resolved from
    WORK_ROOT. This proves existence, not confinement: strict URL evidence is fetched from the
    Hub service account's network. 'done' evidence that cannot resolve is decoration.

    THE TASK'S OWN PROJECT COUNTS. A task about another project ends with a commit in THAT
    project's repository, and its worker finishes with that sha. Resolving a bare sha against the
    Hub's own repository only refused every such completion as "not a commit in this repo" -- and
    an unattended worker has nobody to re-send it. And "could not be asked" (no checkout, no
    resolver) is said as such, never as "not a commit"."""
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
            resolver = resolver or hub_app.commit_resolver()
            found, searched = resolver.has(ev, project)
        except Exception as e:                               # noqa: BLE001
            return str(e)[:120]
        if found:
            return None
        where = "; ".join(searched)
        if found is None:
            return ("could not establish whether %s is a commit (searched: %s). Configure "
                    "HUB_PROJECT_REPOS / HUB_COMMIT_RESOLVER for the task's project, or give a "
                    "URL that dereferences" % (ev[:12], where))
        return "not a commit in %s" % where
    try:
        if (hub_app.WORK_ROOT / ev).exists():
            return None
    except OSError:
        pass
    return "not a resolvable URL, commit sha, or existing path from WORK_ROOT"


def _unattended_hop(request, b) -> int:
    """How many unattended hops deep THIS write is: 0 for a person or an attended session.

    An unattended launcher marks its run (the client sends ``X-Hub-Unattended: 1`` and the
    ``X-Hub-Hop`` its launcher set, from HUB_UNATTENDED / HUB_RESPONDER_HOP); a payload ``hop``
    is honoured too. The DEEPEST value wins, so a hop-2 run whose model also typed a bare marker
    can never re-open the chain it was meant to end (hub_core.offer)."""
    hops = []
    for raw in (b.get("hop"), request.headers.get("X-Hub-Hop")):
        try:
            hops.append(max(0, min(9, int(raw))))
        except (TypeError, ValueError):
            pass
    flagged = str(request.headers.get("X-Hub-Unattended") or "").strip().lower() in ("1", "true")
    hop = max(hops or [0])
    return max(hop, 1) if flagged else hop


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
        before = s.latest_cursor().get("seq", 0)
        ev = s.append(aggregate=eid, type=etype, payload=payload, expected_version=expected_version,
                      git_sha=hub_app._git_head(), idem_key=idem, **_event_identity(agent))
    except ConflictError as c:
        return ({"errors": [{"code": "conflict", "expected": c.expected, "current": c.current}]}, 409)
    if ev.get("seq", 0) > before:
        hub_app.publish_event(ev)
    return ({"data": {"id": eid, "version": ev["result_version"], "event": ev["event_id"]}}, 200)


def _append(type_, eid, payload, *, expected_version, agent, idem, etype):
    """Append using a request-owned store and always release its database handle."""
    s = hub_app.store()
    try:
        return _append_with_store(s, type_, eid, payload, expected_version=expected_version,
                                  agent=agent, idem=idem, etype=etype)
    finally:
        s.close()


@writer(scope="task:write")
def task(request, b):
    agent = b.get("agent", "agent")
    is_create = not b.get("id")
    # 'done' is a terminal transition granted ONLY by complete() so it always has a lease-owned
    # result and evidence. The generic upsert must
    # never mint a 'done' — that was the bypass an adversarial audit found.
    if (b.get("status") or "").lower() == "done":
        return JsonResponse({"errors": [{"code": "use_complete",
            "msg": "status 'done' must go through POST /hub/api/complete (lease + result + evidence)"}]},
            status=409)
    if (b.get("status") or "").lower() == "in_progress":
        return JsonResponse({"errors": [{"code": "use_claim",
            "msg": "status 'in_progress' is granted only by a successful fenced claim"}]},
            status=409)
    twins = []
    if is_create:
        state = hub_app.current_state()
        # MINT-TIME TWIN DETECTION. Two seats bitten by the same incident file the same unit
        # minutes apart and nothing on the write path compares them. Titles were never going to
        # catch it — what a pair of duplicates actually SHARE is the surfaces they touch, which is
        # what hub_core.collision compares. This WARNS and never refuses: a false positive that
        # blocks a legitimate mint is worse than a duplicate the operator can see and fold.
        twins = collision.mint_collisions(b, state)
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "task")
        b.setdefault("status", "todo")
    else:
        eid = b["id"]
        existing = hub_app.current_state().get("entities", {}).get(eid)
        lease = hub_app._read_lease(eid)
        claimed = bool(lease and lease.get("expires", 0) > time.time())
        if existing and (existing.get("status") == "in_progress" or claimed):
            if not hub_app.lease_authorized(eid, b.get("token"), request.hub_auth.subject,
                                            request.hub_auth.credential_id):
                return JsonResponse({"errors": [{"code": "lease",
                    "msg": "mutating claimed work requires its current fenced lease and subject"}]},
                    status=409)
    payload = {k: v for k, v in b.items()
               if k not in ("agent", "expected_version", "idem_key", "token")}
    payload["type"] = "task"
    if is_create:
        # A task an UNATTENDED run raises carries its depth, so no unattended caller is ever
        # offered a chain deeper than one hop (hub_core.offer). Stamped once, at birth.
        hop = _unattended_hop(request, b)
        if hop:
            payload["hop"] = hop
        else:
            payload.pop("hop", None)
    elif "hop" in payload:
        # Depth only ever grows: an update may never make an escalation look attended.
        try:
            prior = int((existing or {}).get("hop") or 0)
            if int(payload["hop"]) <= prior:
                payload.pop("hop")
        except (TypeError, ValueError):
            payload.pop("hop")
    resp, status = _append("task", eid, payload, expected_version=b.get("expected_version"), agent=agent,
                           idem=b.get("idem_key"), etype="task.created" if is_create else "task.updated")
    if twins and status < 400:
        resp["warnings"] = [{"code": "possible_twin",
                             "msg": "this task shares surfaces with existing work; fold if duplicate",
                             "candidates": twins}]
    return JsonResponse(resp, status=status)


@writer(scope="task:complete")
@_lock_refusal
def complete(request, b):
    eid, token, agent = b.get("id"), b.get("token"), b.get("agent", "agent")
    if not isinstance(eid, str) or not eid.strip():
        return JsonResponse({"errors": [{"code": "missing_id"}]}, status=400)
    # Doctrine: exactly one agent OWNS a task before completing it. Require a held, valid lease
    # (claim first) — not just "not held by someone else".
    cur = hub_app._read_lease(eid)
    if not cur:
        return JsonResponse({"errors": [{"code": "must_claim", "msg": "claim the task first (POST /hub/api/claim)"}]}, status=409)
    if not hub_app.lease_authorized(eid, token, request.hub_auth.subject,
                                    request.hub_auth.credential_id):
        return JsonResponse({"errors": [{"code": "lease", "msg": "claimed by another agent / stale token"}]}, status=409)
    if cur.get("agent") != agent:
        return JsonResponse({"errors": [{"code": "identity",
            "msg": "completing agent must be the lease owner", "lease_agent": cur.get("agent")}]}, status=409)
    evidence = b.get("evidence_uri")
    if isinstance(evidence, str):
        evidence = [evidence]
    accept = b.get("accept_note")
    if (not isinstance(accept, str) or not accept.strip() or not isinstance(evidence, list) or
            not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence)):
        return JsonResponse({"errors": [{"code": "need_evidence",
            "msg": "non-empty accept_note + >=1 non-empty string evidence_uri required"}]}, status=422)
    # HUB_DONE_STRICTNESS is only an evidence-resolution dial (settings; default "tracked"):
    #   "tracked" — done always carries WHO/WHAT/EVIDENCE (lease + accept_note + evidence), but
    #               evidence may be anything non-empty (auth-walled ticket links are fine) and a
    #               verification_command is optional; when present, the worker must submit its
    #               typed exit-0 receipt (the Hub never runs it).
    #   "strict"  — evidence must dereference. It never manufactures a test requirement.
    strict = str(hub_app._dj_setting("HUB_DONE_STRICTNESS", "tracked")).lower() == "strict"
    ent = hub_app.current_state().get("entities", {}).get(eid)
    if not ent:
        return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
    if strict:
        # FALSE-GREEN GUARD: evidence must DEREFERENCE — a string nothing can resolve is not evidence.
        # A commit in the task's OWN project dereferences too (hub_core.commits).
        resolver = hub_app.commit_resolver()
        project = resolver.project_of(ent)
        bad = {}
        for e in evidence:
            problem = _evidence_problem(e, project, resolver)
            if problem:
                bad[str(e)[:200]] = problem
        if bad:
            return JsonResponse({"errors": [{"code": "evidence_unresolvable",
                "msg": "every evidence_uri must dereference (URL <400 / commit in this Hub's "
                       "repository or the task's project / existing path from WORK_ROOT)",
                "project": project or None, "bad": bad}]}, status=422)
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
    # A bare suite runner is not a proof of THIS task: a suite is green whenever the repo is
    # healthy, whether or not the work happened, and accepting one teaches every completion to
    # pay the whole battery's price. The command must name the artifact this task changed.
    if vc and re.match(r"^(?:\S*python[\w.]*\s+)?(?:-m\s+)?(?:pytest|unittest(?:\s+discover)?)\b[^&|;]*$",
                       vc.strip(), re.I):
        return JsonResponse({"errors": [{"code": "verification_command_is_a_suite",
            "msg": "a bare suite runner proves the repo, not this task — name a command whose "
                   "subject is the artifact this task changed"}]}, status=422)
    if vc:
        run = b.get("verification_run") or {}
        if not isinstance(run, dict) or not run:
            return JsonResponse({"errors": [{"code": "need_verification_run",
                "msg": "done requires a typed verification_run receipt {command, exit_code, "
                       "output_sha256, ran_by}. Run the task's verification_command YOURSELF and "
                       "submit what happened — the hub does not run it for you (it would be "
                       "executing caller-supplied text on the server)."}]}, status=422)
        problems = []
        if str(run.get("command") or "") != str(vc):
            problems.append("verification_run.command must be the task's own verification_command "
                            f"({vc!r}), not {run.get('command')!r}")
        if run.get("exit_code") != 0:
            problems.append(f"exit_code is {run.get('exit_code')!r}; only 0 grants done")
        if run.get("ran_by") != cur.get("agent"):
            problems.append(f"ran_by {run.get('ran_by')!r} is not the lease owner {cur.get('agent')!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(run.get("output_sha256") or "")):
            problems.append("output_sha256 must be a 64-character lowercase hexadecimal digest")
        if problems:
            return JsonResponse({"errors": [{"code": "bad_verification_run",
                                             "problems": problems}]}, status=422)
        verification_receipt = [run]
    payload = {"type": "task", "status": "done", "verified_by": b.get("verified_by") or [accept],
               "evidence_uri": evidence}
    # The receipt is what makes this completion falsifiable later — it rides the appended event.
    if verification_receipt:
        payload["verification_run"] = verification_receipt
    # Fence the final append against an expiry/reclaim race. Verification can take minutes, so the
    # global lease lock is deliberately acquired only for this short commit section. The original
    # entity version binds the result to exactly the task definition that was verified.
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        if not hub_app.lease_authorized(eid, token, request.hub_auth.subject,
                                        request.hub_auth.credential_id):
            return JsonResponse({"errors": [{"code": "lease", "msg": "lease expired or was reclaimed during verification"}]}, status=409)
        resp, status = _append("task", eid, payload, expected_version=verified_version, agent=agent,
                               idem=b.get("idem_key"), etype="task.transitioned")
        if status == 200:
            hub_app.release_lease(eid, token)
    return JsonResponse(resp, status=status)


@writer(scope="adr:write")
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


@writer(scope="capability:write")
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


@writer(scope="credential:manage")
def agent_credential(request, b):
    """Issue, inspect, or revoke host-local agent credentials.

    The bearer token appears exactly once in an issue response. Registry reads never expose its
    digest, and identity is rotated by issuing a new credential rather than editing a subject.
    """
    action = str(b.get("action") or "").strip().lower()
    registry = agent_auth.CredentialRegistry(hub_app.HUB_DIR)
    try:
        if action == "issue":
            token, record = registry.issue(
                b.get("subject"), b.get("scopes"), b.get("ttl_s"),
                issued_by=request.hub_auth.subject,
            )
            response = JsonResponse({"data": {"credential": record, "token": token}}, status=201)
            response["Cache-Control"] = "no-store"
            return response
        if action == "revoke":
            record = registry.revoke(b.get("credential_id"),
                                     revoked_by=request.hub_auth.subject)
            return JsonResponse({"data": {"credential": record}})
        if action == "list":
            return JsonResponse({"data": {"credentials": registry.list_public()}})
    except agent_auth.CredentialError as exc:
        return JsonResponse({"errors": [{"code": "credential", "msg": str(exc)}]}, status=422)
    return JsonResponse({"errors": [{"code": "bad_credential_action",
                                      "msg": "action must be issue, revoke, or list"}]}, status=422)


def _slug(text, fallback):
    s = "".join(c if c.isalnum() or c in "._-" else "-" for c in str(text or "").lower())
    s = re.sub(r"-{2,}", "-", s).strip("-._")[:48].strip("-._")
    if s and s[0].isalnum():
        return s
    # A title that is entirely punctuation (or opens with it) used to fall back to the bare
    # type name — which COLLIDES every such entity into one id per type — or mint an id the
    # grammar refuses (an unhandled 500). A stable digest keeps the id deterministic: the
    # same title always resolves to the same entity, which is what create-or-update needs.
    if str(text or "").strip():
        return "x" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]
    return fallback


def _simple_writer(type_, etype, *, name_field, numeric=False, natural_key=None):
    """The upsert every remaining entity type needs, in one shape.

    The scaffold shipped `gap`, `feat`, `note` and `deploy` SCHEMAS with no writer at all: an
    agent could read those collections and validate them, and had no way to create one through
    the API. Four documented entity types were effectively write-only-by-hand.

    IDENTITY IS DERIVED, NOT RANDOM, wherever the content supports it. A slug type (feat, note)
    mints from its own name, and a type with a natural key (deploy's sha) mints from that — so a
    retried POST UPDATES the entity instead of minting a twin, which is the whole of idempotency
    here and needs no separate key. Numeric types without a natural key fall back to the
    high-water allocator, and a caller who wants exactly-once there passes `idem_key`.
    """
    @writer(scope=type_ + ":write")
    def view(request, b):
        agent = b.get("agent", "agent")
        eid = b.get("id")
        if not eid:
            if not str(b.get(name_field) or "").strip():
                return JsonResponse({"errors": [{"code": "need_" + name_field,
                    "msg": f"{name_field} is required to create a {type_}"}]}, status=400)
            state = hub_app.current_state()
            if natural_key and str(b.get(natural_key) or "").strip():
                eid = ids.make_id(hub_app.PROJECT_KEY, type_,
                                  _slug(b[natural_key], type_))
            elif numeric:
                eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, type_)
            else:
                eid = ids.make_id(hub_app.PROJECT_KEY, type_,
                                  b.get("local") or _slug(b[name_field], type_))
        payload = {k: v for k, v in b.items()
                   if k not in ("agent", "expected_version", "idem_key", "local")}
        payload["type"] = type_
        resp, status = _append(type_, eid, payload, expected_version=b.get("expected_version"),
                               agent=agent, idem=b.get("idem_key"), etype=etype)
        return JsonResponse(resp, status=status)
    view.__name__ = type_
    return view


# `deploy` keys on its SHA: one release, one record. Without that a retried deploy notification
# mints a second record for the same build, and the coherence block then has two answers to
# "what is running".
gap = _simple_writer("gap", "gap.created", name_field="title", numeric=True)
feat = _simple_writer("feat", "feat.upserted", name_field="name")
note = _simple_writer("note", "note.created", name_field="title")


@writer(scope="deploy:write")
def deploy(request, b):
    """Record one immutable, post-canary release closure.

    ``sha`` is what was built, ``served_sha`` is what the independent front-door canary observed,
    and ``tasks_closed`` is the explicit set this release carries.  Exact equality makes delivery
    truth computable inside a production image without Git.  A retry of the same proof is a no-op;
    changing the proof for an existing SHA is forbidden rather than rewritten into history.
    """
    accepted_fields = {"sha", "served_sha", "tasks_closed", "at", "build", "method",
                       "audit_ok", "agent", "idem_key"}
    unknown_fields = sorted(set(b) - accepted_fields)
    if unknown_fields:
        return JsonResponse({"errors": [{"code": "bad_deploy_fields",
            "msg": "deploy records are create-only and accept only documented release fields",
            "fields": unknown_fields}]}, status=422)

    sha = str(b.get("sha") or "").strip().lower()
    served_sha = str(b.get("served_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return JsonResponse({"errors": [{"code": "bad_sha",
            "msg": "sha must be a 7..64 character hexadecimal Git identity"}]}, status=422)
    if served_sha != sha:
        return JsonResponse({"errors": [{"code": "release_not_observed",
            "msg": "served_sha must exactly equal sha after the front-door canary passes",
            "sha": sha, "served_sha": served_sha or None}]}, status=422)

    tasks_closed = b.get("tasks_closed")
    if not isinstance(tasks_closed, list) or any(
            not isinstance(task_id, str) or not task_id.strip() for task_id in tasks_closed):
        return JsonResponse({"errors": [{"code": "need_tasks_closed",
            "msg": "tasks_closed must be an explicit array of task ids (empty is allowed)"}]},
            status=422)
    normalized_tasks = sorted(task_id.strip() for task_id in tasks_closed)
    if len(normalized_tasks) != len(set(normalized_tasks)):
        return JsonResponse({"errors": [{"code": "duplicate_task_closure",
            "msg": "tasks_closed must not contain duplicate ids"}]}, status=422)

    state = hub_app.current_state()
    entities = state.get("entities", {})
    invalid = []
    for task_id in normalized_tasks:
        task = entities.get(task_id)
        if not task or task.get("type") != "task":
            invalid.append({"id": task_id, "reason": "not a task on this board"})
        elif task.get("status") != "done":
            invalid.append({"id": task_id, "reason": "task is not done"})
    if invalid:
        return JsonResponse({"errors": [{"code": "invalid_task_closure", "tasks": invalid}]},
                            status=422)

    # The immutable SHA is already a valid id local. Keep all of it: truncating a SHA-256 identity
    # would make two distinct releases capable of addressing the same immutable entity.
    eid = ids.make_id(hub_app.PROJECT_KEY, "deploy", sha)
    payload = {k: v for k, v in b.items()
               if k not in ("agent", "expected_version", "idem_key", "local", "id")}
    payload.update({"type": "deploy", "sha": sha, "served_sha": sha,
                    "tasks_closed": normalized_tasks})
    existing = entities.get(eid)
    if existing:
        immutable_fields = ("type", "sha", "served_sha", "tasks_closed", "at", "build",
                            "method", "audit_ok")
        comparable_existing = dict(existing)
        if isinstance(comparable_existing.get("tasks_closed"), list):
            comparable_existing["tasks_closed"] = sorted(comparable_existing["tasks_closed"])
        same_proof = all(comparable_existing.get(field) == payload.get(field)
                         for field in immutable_fields)
        if same_proof:
            return JsonResponse({"data": {"id": eid, "version": existing.get("version"),
                                           "idempotent": True}})
        return JsonResponse({"errors": [{"code": "immutable_deploy",
            "msg": "a deploy record is immutable; publish a different SHA for a different release",
            "id": eid, "version": existing.get("version")}]}, status=409)

    # expected_version=0 closes the concurrent-create race. A second writer either replays its
    # idem key or loses with OCC; it can retry and receive the idempotent record above.
    resp, status = _append("deploy", eid, payload, expected_version=0,
                           agent=b.get("agent", "agent"), idem=b.get("idem_key"),
                           etype="deploy.created")
    return JsonResponse(resp, status=status)


@writer(scope="decision:write")
def decision(request, b):
    agent = b.get("agent", "agent")
    topic, choice = b.get("topic"), b.get("choice")
    if not topic or not choice:
        return JsonResponse({"errors": [{"code": "need_topic_choice"}]}, status=400)
    idem = "decision:" + hashlib.sha256((topic + choice).encode("utf-8")).hexdigest()[:16]
    s = hub_app.store()
    try:
        before = s.latest_cursor().get("seq", 0)
        ev = s.append(
            aggregate=f"{hub_app.PROJECT_KEY}:decision:{idem[-12:]}", type="decision.logged",
            payload={"topic": topic, "choice": choice, "rationale": b.get("rationale"),
                     "invalidates": b.get("invalidates", []), "refs": b.get("refs", [])},
            expected_version=None, git_sha=hub_app._git_head(), idem_key=idem,
            **_event_identity(agent))
        if ev.get("seq", 0) > before:
            hub_app.publish_event(ev)
    finally:
        s.close()
    return JsonResponse({"data": {"event": ev["event_id"]}})


@writer(scope="task:claim")
@_lock_refusal
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
        live = hub_app.leases()
        # A lease whose console is provably GONE past its grace holds nothing (hub_core.liveness):
        # it neither blocks this task nor counts against its agent or the WIP ceiling. hub_app.claim
        # applies the same verdict and records whom the new lease took over from.
        if any(lease.get("session") for lease in live):
            roster_ = hub_app.roster()
            live = [lease for lease in live
                    if not (lease.get("session") and lease.get("session") !=
                            (request.headers.get("X-Hub-Session") or "") and
                            hub_app.lease_verdict(lease, roster_)["released"])]
        existing = next((lease for lease in live if lease.get("task") == eid), None)
        same_lease = bool(
            existing and existing.get("agent") == agent and
            (existing.get("auth_subject") in (None, request.hub_auth.subject)) and
            (existing.get("credential_id") in (None, request.hub_auth.credential_id))
        )
        other = next((lease for lease in live
                      if lease.get("agent") == agent and lease.get("task") != eid), None)
        if other:
            return JsonResponse({"errors": [{"code": "one_active", "task": other.get("task"),
                                             "msg": "agent already owns an active task"}]}, status=409)
        if not same_lease and hub_app.wip_status(len(live))["saturated"]:
            return JsonResponse({"errors": [{"code": "board_saturated",
                                             "msg": "configured WIP ceiling reached"}]}, status=429)
        verdict = flow.classify(ent, flags, existing)
        if not same_lease and not verdict["available"]:
            error = {"code": verdict["state"], "msg": verdict["reason"]}
            if verdict["state"] == "leased" and existing:
                # Say WHEN it frees: a gone holder's lease is released once its grace runs out,
                # a live or unprovable one only by its clock.
                v = hub_app.lease_verdict(existing)
                frees = (v["frees_in_s"] if v["state"] == "gone" else
                         max(0, int(float(existing.get("expires") or 0) - time.time())))
                error.update({"holder_session": existing.get("session"),
                              "holder_state": v["state"], "gone_s": v["gone_s"],
                              "frees_in_s": frees})
                error["msg"] += ("; its console %s is GONE (not seen for %ds) and the claim "
                                 "frees itself in %ds" % (existing.get("session"), v["gone_s"],
                                                          frees)
                                 if v["state"] == "gone" else
                                 "; the lease frees itself in %ds unless renewed" % frees)
            return JsonResponse({"errors": [error]}, status=409)
        res = hub_app.claim(eid, agent, ttl_s=ttl,
                            auth_subject=request.hub_auth.subject,
                            credential_id=request.hub_auth.credential_id,
                            actor_kind=request.hub_auth.actor_kind,
                            session=request.headers.get("X-Hub-Session") or "",
                            machine=request.headers.get("X-Hub-Machine") or "")
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


@writer(scope="task:release")
@_lock_refusal
def release(request, b):
    eid, token = b.get("id"), b.get("token")
    if not isinstance(eid, str) or not eid.strip() or not isinstance(token, str) or not token.strip():
        return JsonResponse({"errors": [{"code": "need_id_token"}]}, status=400)
    lease = next((row for row in hub_app.leases() if row.get("task") == eid), None)
    if not lease or lease.get("token") != token:
        return JsonResponse({"errors": [{"code": "lease_mismatch"}]}, status=409)
    if not hub_app.lease_authorized(eid, token, request.hub_auth.subject,
                                    request.hub_auth.credential_id):
        return JsonResponse({"errors": [{"code": "lease_subject_mismatch"}]}, status=409)
    agent = b.get("agent")
    if agent and agent != lease.get("agent"):
        return JsonResponse({"errors": [{"code": "lease_agent_mismatch"}]}, status=409)
    hub_app.release_lease(eid, token)
    return JsonResponse({"ok": True, "task": eid, "stale_reclaim": True})


def _bounded_setting(name, default, low, high):
    try:
        value = int(hub_app._dj_setting(name, os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _schedule_failure_retry(task_id, not_before):
    """Wake connected readers at the exact durable-backoff boundary, without polling."""
    try:
        from . import realtime
        when = datetime.fromisoformat(str(not_before).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        realtime.schedule(hub_app.HUB_DIR, "task-ready:" + task_id, when.timestamp(),
                          {"kind": "task.ready", "task": task_id},
                          channel=hub_app.PROJECT_KEY)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Hub task-ready timer could not be scheduled")


@writer(scope="task:fail")
@_lock_refusal
def fail(request, b):
    """Atomically turn one real failed attempt into bounded, specialist-routable repair work."""
    eid, token, agent = b.get("id"), b.get("token"), b.get("agent", "agent")
    signature = str(b.get("signature") or "").strip()
    note = str(b.get("note") or "").strip()
    kind = str(b.get("kind") or "execution").strip()
    if (not isinstance(eid, str) or not eid.strip() or not isinstance(token, str) or
            not token.strip() or not signature or len(signature) > 256 or not note or
            len(note) > 4000 or not kind or len(kind) > 128):
        return JsonResponse({"errors": [{"code": "need_failure",
            "msg": "id, token, signature (<=256), note (<=4000), and kind (<=128) are required"}]},
            status=400)
    evidence = b.get("evidence_uri") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if (not isinstance(evidence, list) or len(evidence) > 32 or
            any(not isinstance(item, str) or not item.strip() for item in evidence)):
        return JsonResponse({"errors": [{"code": "bad_failure_evidence"}]}, status=422)
    consequential = b.get("consequential", False)
    if not isinstance(consequential, bool):
        return JsonResponse({"errors": [{"code": "bad_consequential",
                                         "msg": "consequential must be boolean"}]}, status=422)

    failure_key = "fail:" + hashlib.sha256(
        (eid + "\0" + token + "\0" + signature).encode("utf-8")).hexdigest()[:32]
    store = hub_app.store()
    try:
        replay = next((event for event in store.events(eid)
                       if event.get("idem_key") == failure_key), None)
        if replay:
            hub_app.release_lease(eid, token)
            payload = replay.get("payload") or {}
            return JsonResponse({"ok": True, "idempotent": True, "task": eid,
                                 "repair_task": payload.get("repair_task"),
                                 "failure_repeats": payload.get("failure_repeats"),
                                 "circuit_open": bool(payload.get("poison_blocked"))})

        with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
            lease = hub_app._read_lease(eid)
            if not lease:
                return JsonResponse({"errors": [{"code": "must_claim"}]}, status=409)
            if not hub_app.lease_authorized(eid, token, request.hub_auth.subject,
                                            request.hub_auth.credential_id):
                return JsonResponse({"errors": [{"code": "lease",
                    "msg": "failure must come from the current fenced lease owner"}]}, status=409)
            if lease.get("agent") != agent:
                return JsonResponse({"errors": [{"code": "identity",
                                                  "lease_agent": lease.get("agent")}]}, status=409)

            state = hub_app.current_state(store)
            ent = state.get("entities", {}).get(eid)
            if not ent or ent.get("type") != "task":
                return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
            if ent.get("status") != "in_progress":
                return JsonResponse({"errors": [{"code": "not_in_progress"}]}, status=409)

            total = int(ent.get("failure_count") or 0) + 1
            repeats = (int(ent.get("failure_repeats") or 0) + 1
                       if ent.get("failure_signature") == signature else 1)
            threshold = _bounded_setting("HUB_FAILURE_CIRCUIT_THRESHOLD", 3, 1, 100)
            base = _bounded_setting("HUB_FAILURE_BACKOFF_BASE_S", 30, 1, 86400)
            ceiling = _bounded_setting("HUB_FAILURE_BACKOFF_MAX_S", 3600, base, 86400)
            backoff = min(ceiling, base * (2 ** min(repeats - 1, 20)))
            circuit = repeats >= threshold
            now = datetime.now(timezone.utc)
            at = now.isoformat().replace("+00:00", "Z")
            not_before = (now + timedelta(seconds=backoff)).isoformat().replace("+00:00", "Z")

            repair_local = "repair-" + hashlib.sha256(
                (eid + "\0" + signature).encode("utf-8")).hexdigest()[:16]
            repair_id = ids.make_id(hub_app.PROJECT_KEY, "task", repair_local)
            repair = state.get("entities", {}).get(repair_id)
            if repair and repair.get("status") in ("done", "dropped", "shadow"):
                repair_id = ids.make_id(hub_app.PROJECT_KEY, "task",
                                        repair_local + "-r" + str(total))
                repair = state.get("entities", {}).get(repair_id)

            last_failure = {"signature": signature, "note": note, "at": at, "kind": kind,
                            "consequential": consequential, "evidence_uri": evidence,
                            "agent": agent}
            source_payload = {
                "type": "task", "status": "todo", "failure_count": total,
                "failure_repeats": repeats, "failure_signature": signature,
                "last_failure": last_failure, "retry_backoff_s": backoff,
                "not_before": not_before, "repair_task": repair_id,
                "poison_blocked": circuit,
                "poison_reason": (f"failure signature repeated {repeats} times; "
                                  f"repair through {repair_id} before retry") if circuit else "",
                "operator_attention": consequential,
            }
            source_merged = {**ent, **source_payload, "version": ent.get("version", 0) + 1}
            source_errors = validate(source_merged, "task", hub_app.registry())
            if source_errors:
                return JsonResponse({"errors": [{"code": "schema", "msg": error}
                                                  for error in source_errors]}, status=422)

            auth = _event_identity(agent)
            operations = [{
                "aggregate": eid, "type": "task.failed", "payload": source_payload,
                "expected_version": ent.get("version"), "git_sha": hub_app._git_head(),
                "idem_key": failure_key, **auth,
            }]
            repair_created = not repair
            if repair_created:
                required = ["hub.repair"] + (["hub.operator"] if consequential else [])
                repair_payload = {
                    "type": "task", "title": "Repair: " + note.splitlines()[0][:180],
                    "status": "todo", "priority": "P0" if consequential else "P1",
                    "phase": "Repair lane", "work_kind": "product",
                    "acceptance": ("Identify and land the cause-specific repair, preserve the "
                                   "failed-attempt evidence, clear the source circuit when safe, "
                                   "and make the source task ready for a fresh fenced claim."),
                    "touches": list(ent.get("touches") or []), "surfaced_by": eid,
                    "repair_for": eid,
                    "repair_role": "operator-repair" if consequential else "repair-specialist",
                    "operator_attention": consequential,
                    "routing": {"required_capabilities": required,
                                "risk": "high" if consequential else "moderate"},
                    "source": "failure:" + eid,
                }
                repair_merged = {"id": repair_id, **repair_payload, "version": 1}
                repair_errors = validate(repair_merged, "task", hub_app.registry())
                if repair_errors:
                    return JsonResponse({"errors": [{"code": "schema", "msg": error}
                                                      for error in repair_errors]}, status=422)
                operations.append({
                    "aggregate": repair_id, "type": "task.created", "payload": repair_payload,
                    "expected_version": 0, "git_sha": hub_app._git_head(),
                    "idem_key": "repair:" + repair_local, **auth,
                })

            try:
                events = store.append_batch(operations)
            except ConflictError as conflict:
                return JsonResponse({"errors": [{"code": "conflict",
                    "expected": conflict.expected, "current": conflict.current}]}, status=409)
            lease_released = hub_app.release_lease(eid, token)

        # One wake after the full batch is durable; the cumulative patch contains every event.
        if events:
            hub_app.publish_event(events[-1])
        _schedule_failure_retry(eid, not_before)
        return JsonResponse({"ok": True, "task": eid, "repair_task": repair_id,
                             "repair_created": repair_created, "failure_count": total,
                             "failure_repeats": repeats, "backoff_s": backoff,
                             "not_before": not_before, "circuit_open": circuit,
                             "lease_released": lease_released,
                             "events": [event["event_id"] for event in events]})
    finally:
        store.close()


@writer(scope="task:claim")
@_lock_refusal
def take(request, b):
    agent = b.get("agent")
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 256:
        return JsonResponse({"errors": [{"code": "need_agent"}]}, status=400)
    try:
        ttl = int(b.get("ttl_s", 900))
    except (TypeError, ValueError):
        ttl = 0
    if ttl < 1 or ttl > 86400:
        return JsonResponse({"errors": [{"code": "bad_ttl"}]}, status=422)
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        state, live = hub_app.current_state(), hub_app.leases()
        owned = next((row for row in live if row.get("agent") == agent), None)
        if owned:
            return JsonResponse({"errors": [{"code": "one_active", "task": owned.get("task")}]}, status=409)
        if hub_app.wip_status(len(live))["saturated"]:
            return JsonResponse({"errors": [{"code": "board_saturated"}]}, status=429)
        lease_ids = {row.get("task") for row in live}
        candidates, withheld = [], {}
        # The same offer rule the readiness rail applies (hub_core.offer): given to somebody
        # else by name, only another machine can do it, or an unattended run's escalation that
        # is a person's. Counted by reason so an empty pull says WHY it is empty.
        machine = str(b.get("machine") or request.headers.get("X-Hub-Machine") or "")
        unattended = (b.get("unattended") is True or str(
            request.headers.get("X-Hub-Unattended") or "").strip().lower() in ("1", "true"))
        now_s = time.time()
        for task in state.get("entities", {}).values():
            if task.get("type") != "task" or task.get("id") in lease_ids:
                continue
            verdict = flow.classify(task, state.get("flags", {}).get(task.get("id"), {}), None)
            if not verdict["available"]:
                continue
            why = offer.withheld(task, agent=agent, machine=machine, unattended=unattended,
                                 now=now_s)
            if why:
                key = why.split(":")[0].split(" (")[0]
                withheld[key] = withheld.get(key, 0) + 1
                continue
            candidates.append(task)
        if not candidates:
            return JsonResponse({"errors": [{"code": "no_ready_task",
                                             "withheld": withheld}]}, status=409)
        busy_touches = set()
        entities = state.get("entities", {})
        for lease in live:
            busy_touches.update(schedule.normalized_touches(
                entities.get(lease.get("task"), {})))
        try:
            routed, routing = schedule.route_ready(
                candidates,
                flags=state.get("flags", {}),
                busy_touches=busy_touches,
                worker_profile=b.get("worker"),
            )
        except ValueError as exc:
            return JsonResponse({"errors": [{"code": "bad_worker_profile", "msg": str(exc)}]},
                                status=422)
        if not routed:
            return JsonResponse({"errors": [{"code": "no_compatible_task",
                                              "msg": "ready work exists but none matches this worker"}],
                                 "routing": routing}, status=409)
        task = routed[0]
        eid = task["id"]
        res = hub_app.claim(eid, agent, ttl_s=ttl,
                            auth_subject=request.hub_auth.subject,
                            credential_id=request.hub_auth.credential_id,
                            actor_kind=request.hub_auth.actor_kind,
                            session=request.headers.get("X-Hub-Session") or "",
                            machine=request.headers.get("X-Hub-Machine") or "")
        if not res["ok"]:
            return JsonResponse(res, status=409)
        if task.get("status") != "in_progress":
            transition, code = _append("task", eid, {"type": "task", "status": "in_progress"},
                                       expected_version=task.get("version"), agent=agent,
                                       idem=b.get("idem_key"), etype="task.transitioned")
            if code != 200:
                hub_app.release_lease(eid, res["token"])
                return JsonResponse(transition, status=code)
            res["version"] = transition["data"]["version"]
        res["task"] = task
        res["routing"] = routing
    return JsonResponse(res)


def known_agents():
    """``(names, readable)``: every agent this board knows -- issued credential subjects, seats
    presence has seen, and lease holders.

    An EMPTY or unreadable roster proves nothing (a board that answered nothing looks like one
    with no members), so a caller treats ``readable=False`` or an empty set as "cannot check",
    never as "no such agent". Widening a check until it passes is how a typo becomes work that
    is offered to nobody; refusing on an unreadable roster is how assignment stops working the
    day a sidecar is unreadable."""
    names, readable = set(), True
    try:
        names.update(str(row.get("subject") or "").strip().lower()
                     for row in agent_auth.CredentialRegistry(hub_app.HUB_DIR).list_public())
    except Exception:                                        # noqa: BLE001
        readable = False
    try:
        names.update(str(k).strip().lower() for k in (hub_app.read_presence() or {}))
    except Exception:                                        # noqa: BLE001
        readable = False
    try:
        names.update(str(row.get("agent") or "").strip().lower()
                     for row in hub_app.leases(include_expired=True))
    except Exception:                                        # noqa: BLE001
        readable = False
    names.discard("")
    return names, readable


@writer(scope="task:claim")
def item_claim(request, b):
    """POST /hub/api/item-claim {item, machine, [release]} -> 200 granted | 409 held elsewhere.

    One responder per NON-task item (a question, an error fingerprint) across every machine
    (hub_core.item_claims). Behind the explicit @writer gate like every other write -- never
    reliant on a middleware alone -- which also binds `agent` to the authenticated credential
    instead of trusting the payload. Same authority class as claiming a task, so the same scope.
    A caller that cannot reach this route decides for itself whether to proceed; the route is
    strict."""
    from hub_core import item_claims
    item = str(b.get("item") or "").strip()
    machine = str(b.get("machine") or request.headers.get("X-Hub-Machine") or "").strip()
    if not item or not machine:
        return JsonResponse({"errors": [{"code": "need_item_and_machine",
            "msg": "item (a question id or error fingerprint) and machine are required"}]},
            status=400)
    session = str(b.get("session") or request.headers.get("X-Hub-Session") or "").strip()
    try:
        roster = hub_app.roster()
    except Exception:                                        # noqa: BLE001 - unprovable, not gone
        roster = None
    try:
        granted, row = item_claims.claim(hub_app.HUB_DIR, item, machine,
                                         b.get("agent") or request.hub_auth.subject,
                                         release=b.get("release") is True, session=session,
                                         roster=roster, grace_s=hub_app.gone_grace_s())
    except ValueError as exc:
        return JsonResponse({"errors": [{"code": "bad_claim", "msg": str(exc)}]}, status=400)
    if not granted:
        who = row.get("machine") or "another machine"
        if row.get("session"):
            who += " (console %s)" % row["session"]
        if row.get("holder_state") == "gone":
            msg = ("%s is GONE (not seen for %ds); the claim frees itself in %ds -- claim again "
                   "then" % (who, row.get("gone_s") or 0, row.get("frees_in_s") or 0))
        else:
            msg = ("%s claimed it %ds ago; it releases in %ds unless renewed"
                   % (who, row.get("age_s") or 0, row.get("releases_in_s") or 0))
        return JsonResponse({"errors": [{"code": "claimed_elsewhere", "msg": msg}],
            "data": {"holder": row.get("machine"), "holder_session": row.get("session"),
                     "agent": row.get("agent"), "age_s": row.get("age_s"),
                     "releases_in_s": row.get("releases_in_s"),
                     "holder_state": row.get("holder_state"), "gone_s": row.get("gone_s"),
                     "frees_in_s": (row.get("frees_in_s") if row.get("holder_state") == "gone"
                                    else row.get("releases_in_s"))}},
            status=409)
    hub_app._publish_realtime("item.claimed", item=item, machine=row.get("machine"),
                              released=bool(row.get("released")))
    return JsonResponse({"data": {"granted": True, **row}})


@writer(scope="task:assign")
def hand(request, b):
    """GIVE a task to a named agent (or clear the assignment with ``to: ""``).

    The payload's ``agent`` is always WHO IS WRITING; the recipient is ``to``. Conflating the two
    is the failure this route exists to end: an assignment verb that put the recipient in the
    identity field was refused on every call, so assignment lived in prose only a person read.

    Until the recipient claims it the task carries ``assigned_to``: the board shows them as its
    owner, their inbox (and its long-poll) carries it, and atomic pull never hands it to anybody
    else. The recipient is checked against the known roster; an unreadable roster is "cannot
    check" (the assignment lands with a warning), never "no such agent"."""
    eid = b.get("id")
    to = b.get("to")
    if not isinstance(eid, str) or not eid.strip() or not isinstance(to, str):
        return JsonResponse({"errors": [{"code": "need_id_to",
            "msg": "id and to (the recipient agent; empty string clears) are required"}]},
            status=400)
    to = to.strip().lower()
    if to and not _valid_agent_name(to):
        return JsonResponse({"errors": [{"code": "bad_recipient",
            "msg": "to must be a lowercase agent name (letters, digits, . _ -)"}]}, status=422)
    state = hub_app.current_state()
    ent = state.get("entities", {}).get(eid)
    if not ent or ent.get("type") != "task":
        return JsonResponse({"errors": [{"code": "not_found", "msg": "task does not exist"}]},
                            status=404)
    if ent.get("status") in ("done", "dropped", "shadow"):
        return JsonResponse({"errors": [{"code": "terminal",
            "msg": "task is %s; there is nothing left to hand" % ent.get("status")}]}, status=409)
    lease = hub_app._read_lease(eid)
    if lease and lease.get("expires", 0) > time.time():
        return JsonResponse({"errors": [{"code": "held",
            "msg": "%s is holding this task right now; a lease beats an assignment -- ask them "
                   "to release it first" % (lease.get("agent") or "a worker"),
            "held_by": lease.get("agent")}]}, status=409)
    warnings = []
    if to:
        known, readable = known_agents()
        if readable and known and to not in known:
            return JsonResponse({"errors": [{"code": "unknown_agent",
                "msg": "no agent named %r is known to this board; a task given to a name nobody "
                       "answers to is offered to no one" % to,
                "known": sorted(known)[:50]}]}, status=422)
        if not readable or not known:
            warnings.append({"code": "recipient_unchecked",
                             "msg": "the roster could not be read, so %r was not checked" % to})
    expected = b.get("expected_version", ent.get("version"))
    delta = {"type": "task", "assigned_to": to}
    if isinstance(b.get("machine"), str):
        # MACHINE AFFINITY rides the hand-off when the recipient's work only exists on one
        # machine; an empty string clears it (hub_core.offer).
        machine = b["machine"].strip().lower()[:120]
        if machine and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", machine):
            return JsonResponse({"errors": [{"code": "bad_machine",
                "msg": "machine is a lowercase host name (letters, digits, . _ -)"}]},
                status=422)
        delta["machine"] = machine
    resp, status = _append("task", eid, delta,
                           expected_version=expected, agent=b.get("agent") or "operator",
                           idem=b.get("idem_key"), etype="task.updated")
    if status < 400:
        resp.setdefault("data", {})["assigned_to"] = to or None
        if warnings:
            resp["warnings"] = warnings
    return JsonResponse(resp, status=status)


@writer(scope="task:heartbeat")
@_lock_refusal
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
    res = hub_app.heartbeat(b.get("id"), b.get("token"), ttl_s=ttl,
                            auth_subject=request.hub_auth.subject,
                            credential_id=request.hub_auth.credential_id,
                            actor_kind=request.hub_auth.actor_kind)
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


@writer(scope="launch:consume")
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


# ── The ask/answer loop: a blocked worker's question actually reaches the operator ──

_ASK_NOISE = {
    "the", "and", "for", "are", "but", "not", "you", "your", "our", "with", "that", "this",
    "from", "have", "has", "was", "were", "why", "how", "what", "when", "who", "should",
    "would", "could", "can", "does", "did", "any", "all", "its", "there", "their",
}
_ASK_DUP_BAR = 0.5          # Jaccard over significant tokens


def _ask_tokens(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", str(text or "").lower())
            if w not in _ASK_NOISE}


def _ask_overlap(one, other):
    a, b = _ask_tokens(one), _ask_tokens(other)
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _already_covered(state, text):
    """Surfaces the answer could already be on: questions (open or answered) and notes tagged
    as shared knowledge. FAILS OPEN, deliberately and in both directions: any error returns
    nothing and the ask proceeds — a worker blocked on a real question must never be silenced
    by a search outage. A duplicate is cheap and visible; a lost question is neither."""
    hits = []
    try:
        for eid, ent in (state.get("entities") or {}).items():
            if not isinstance(ent, dict) or ent.get("type") != "note":
                continue
            tags = {str(t).lower() for t in (ent.get("tags") or [])}
            if "question" in tags:
                score = max(_ask_overlap(text, ent.get("title")),
                            _ask_overlap(text, ent.get("body_md")))
                kind = "open" if "open" in tags else "answered"
            elif tags & {"pattern", "memory", "solution"}:
                score = _ask_overlap(text, ent.get("title"))
                kind = "crystallized"
            else:
                continue
            if score >= _ASK_DUP_BAR:
                hits.append({"score": round(score, 2), "id": eid, "kind": kind,
                             "title": str(ent.get("title") or "")[:120]})
    except Exception:                                        # noqa: BLE001 - fail open
        return []
    # One id, one row: a question both open on the board AND crystallized matches on two
    # surfaces and would print twice, overstating the case against the asker.
    hits.sort(key=lambda h: h["score"], reverse=True)
    best, seen = [], set()
    for hit in hits:
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        best.append(hit)
    return best[:5]


_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _valid_agent_name(name) -> bool:
    # fullmatch, NOT match: `$` also matches immediately before a trailing newline, so a name
    # with one passes the shape check and then fails deeper as an id error — a 500 instead of
    # a 422. Anchor the end for real.
    return bool(isinstance(name, str) and _AGENT_NAME.fullmatch(name))


@writer(scope="ask:write")
def ask(request, b):
    """A worker's question to the operator, as a first-class board entity that DELIVERS.

    The note lands tagged `question`+`open`, carries its asker in a stable `asker` field
    (never derived from provenance, which answering REWRITES — deriving the asker from the
    last writer mis-addresses every re-answered reply), wakes connected cockpits through the
    push plane the moment it exists, and is returned by /hub/inbox/wait for whoever answers.
    Idempotent on the exact wording (the id is a digest of it), and guarded against
    restatements: a question the board already has — open, answered, or crystallized — is
    refused with the matching ids instead of minting another copy, because "search before
    you ask" is a rule, and a rule that depends on every future caller remembering it is not
    a control. `anyway: true` is the escape hatch for a question that genuinely is different."""
    agent = b.get("agent") or ""
    if not _valid_agent_name(agent):
        return JsonResponse({"errors": [{"code": "need_agent",
            "msg": "ask requires a valid lowercase agent name (the asker) in the payload"}]},
            status=422)
    text = str(b.get("question") or "").strip()
    if not text:
        return JsonResponse({"errors": [{"code": "need_question"}]}, status=400)
    state = hub_app.current_state()
    if not b.get("anyway"):
        dups = _already_covered(state, text)
        if dups:
            return JsonResponse({"errors": [{"code": "duplicate_question",
                "msg": "the board already has this question — read the matches first; if "
                       "yours is genuinely different, retry with anyway=true; if it is the "
                       "same one still unanswered, add to that thread instead of filing "
                       "another copy",
                "matches": dups}]}, status=409)
    local = "q-%s-%s" % (_slug(agent, "agent"),
                         hashlib.sha256(text.encode("utf-8")).hexdigest()[:8])
    eid = ids.make_id(hub_app.PROJECT_KEY, "note", local)
    existing = state["entities"].get(eid)
    payload = {"type": "note", "category": "context", "title": text[:300],
               "asker": agent, "status": "standing", "tags": ["question", "open"],
               "body_md": str(b.get("context") or "")}
    hop = _unattended_hop(request, b)
    if hop:
        payload["hop"] = hop
    related = [t for t in (b.get("relates_to") or []) if isinstance(t, str) and ":" in t]
    if related:
        payload["relates_to"] = related
    resp, status = _append("note", eid, payload,
                           expected_version=existing.get("version") if existing else None,
                           agent=agent, idem=b.get("idem_key"), etype="note.created")
    return JsonResponse(resp, status=status)


@writer(scope="directive:write")
def answer(request, b):
    """Close a question: reply to the asker AND retire the open question, as ONE verb.

    A half-done answer is worse than either half alone — a reply sent with the question
    still open means the board's question count only ever grows; a question closed with no
    reply means the asker learns nothing. The reply is a directive targeted at the asker
    (so it is ADDRESSED: the asker's inbox wait returns it, and their ack closes delivery),
    idempotent per question — re-answering updates the one answer directive in place, never
    mints a second copy delivered who-knows-where. Requires the `directive:write` scope: an
    instruction injected into an agent's working context is an authority tier above ordinary
    board writes, and worker credentials are simply never issued it."""
    question_id = str(b.get("question") or "").strip()
    text = str(b.get("text") or "").strip()
    if not question_id or not text:
        return JsonResponse({"errors": [{"code": "need_question_and_text"}]}, status=400)
    if ":" not in question_id:
        question_id = ids.make_id(hub_app.PROJECT_KEY, "note", question_id)

    state = hub_app.current_state()
    note_ent = (state.get("entities") or {}).get(question_id)
    if not note_ent or note_ent.get("type") != "note":
        return JsonResponse({"errors": [{"code": "no_such_question", "msg": question_id}]}, status=404)
    # The asker comes from the note's own stable field. provenance.agent is the LAST writer,
    # which after a first answer is the answerer — deriving from it would mis-target every
    # re-answered directive at the operator themselves.
    asker = str(note_ent.get("asker")
                or (note_ent.get("provenance") or {}).get("agent") or "").strip().lower()
    if not _valid_agent_name(asker):
        return JsonResponse({"errors": [{"code": "unknown_asker",
            "msg": "the question does not name who asked; issue a directive instead"}]}, status=409)

    question_text = str(note_ent.get("title") or "")
    # HONEST ATTRIBUTION, mechanically -- the mirror of the ask's hop stamp. An answer an
    # unattended run wrote reads identically to one a person wrote unless it SAYS so, and the
    # asker deserves to know which they got. Structured (`unattended`) for every surface, and
    # one line in the text for any reader that only sees the body.
    unattended = bool(b.get("unattended") is True or _unattended_hop(request, b))
    if unattended and "unattended run" not in text:
        text += "\n\n[answered by an unattended run -- verify against the source if it matters]"
    payload = {
        "type": "directive",
        "title": ("Answer: %s" % question_text)[:300],
        "body_md": text + "\n\n---\nIn answer to your question: " + question_text,
        "targets": [asker],
        "status": "active",
        "answers": question_id,
    }
    if unattended:
        payload["unattended"] = True
    existing_dir = next(
        (e for e in state["entities"].values()
         if isinstance(e, dict) and e.get("type") == "directive"
         and e.get("answers") == question_id),
        None)
    agent = b.get("agent", request.hub_auth.subject)
    if existing_dir:
        eid = existing_dir["id"]
        resp, status = _append("directive", eid, payload,
                               expected_version=existing_dir.get("version"),
                               agent=agent, idem=b.get("idem_key"), etype="directive.issued")
    else:
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "directive")
        resp, status = _append("directive", eid, payload, expected_version=None,
                               agent=agent, idem=b.get("idem_key"), etype="directive.issued")
    if status not in (200, 201):
        return JsonResponse(resp, status=status)

    # Retire the question by flipping its tags: `open` -> `answered`. The tag, not the
    # status enum, is what marks a question as awaiting an answer.
    still_open = False
    try:
        tags = [t for t in (note_ent.get("tags") or []) if str(t).lower() != "open"]
        if "answered" not in [str(t).lower() for t in tags]:
            tags.append("answered")
        closed = {k: v for k, v in note_ent.items() if k not in ("version", "provenance")}
        closed["tags"] = tags
        _retire_resp, retire_status = _append(
            "note", question_id, closed, expected_version=note_ent.get("version"),
            agent=agent, idem=None, etype="note.created")
        still_open = retire_status not in (200, 201)
    except Exception:                                    # noqa: BLE001 - never lose the reply
        still_open = True
    if still_open:
        # Loud, in the response the operator reads: an answer that leaves its question open
        # is how a board's question count only ever grows.
        resp.setdefault("data", {})["question_still_open"] = True
    resp.setdefault("data", {})["directive"] = eid
    resp["data"]["asker"] = asker

    # CRYSTALLIZE is OPT-IN, and on the origin system it used to be automatic — thirteen
    # "lessons" appeared in one afternoon, most of them requests that got fulfilled rather
    # than rules anybody should carry. The knowledge surface rides every future duplicate-ask
    # check, so minting indiscriminately taxes it to remember something true for one
    # afternoon. The answerer decides, because only they know which of the two this was.
    if not b.get("crystallize"):
        resp["data"]["crystallized"] = False
        return JsonResponse(resp, status=status)
    try:
        note_local = "qa-" + question_id.rsplit(":", 1)[-1]
        lesson_id = ids.make_id(hub_app.PROJECT_KEY, "note", note_local)
        existing_lesson = (state.get("entities") or {}).get(lesson_id)
        lesson_payload = {
            "type": "note", "category": "method",
            "title": (question_text or ("answer for " + asker))[:300],
            "body_md": text + "\n\n(Crystallized from a question asked by " + asker + ".)",
            "tags": ["pattern", "memory", "answered-question"],
            "status": "standing",
            "relates_to": [question_id],
        }
        _lesson_resp, lesson_status = _append(
            "note", lesson_id, lesson_payload,
            expected_version=existing_lesson.get("version") if existing_lesson else None,
            agent=agent, idem=None, etype="note.created")
        resp["data"]["crystallized"] = lesson_id if lesson_status in (200, 201) else False
    except Exception:                                    # noqa: BLE001 - bookkeeping never
        resp["data"]["crystallized"] = False             # loses an answer already sent
    return JsonResponse(resp, status=status)


@writer(scope="directive:write")
def directive(request, b):
    """An operator instruction addressed to named agents (or 'all'). `directive:write` is an
    authority tier above ordinary board writes: the shared-root credential holds it, and a
    worker credential is issued it only deliberately."""
    agent = b.get("agent", request.hub_auth.subject)
    is_create = not b.get("id")
    if is_create:
        state = hub_app.current_state()
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "directive")
        b.setdefault("status", "active")
        b.setdefault("targets", ["all"])
    else:
        eid = b["id"]
    payload = {k: v for k, v in b.items() if k not in ("agent", "expected_version", "idem_key")}
    payload["type"] = "directive"
    resp, status = _append("directive", eid, payload, expected_version=b.get("expected_version"),
                           agent=agent, idem=b.get("idem_key"),
                           etype="directive.issued" if is_create else "directive.updated")
    return JsonResponse(resp, status=status)


@writer(scope="ack:write")
def ack(request, b):
    """One agent's record that one directive (or answer) was delivered to it. A scoped
    credential acks FOR ITSELF — the write seam already forced b['agent'] to the credential
    subject. Stable id => replay-safe for offline retry queues."""
    agent = b.get("agent") or ""
    if not _valid_agent_name(agent):
        return JsonResponse({"errors": [{"code": "need_agent"}]}, status=422)
    directive_id = str(b.get("directive") or "")
    if directive_id and ":" not in directive_id:
        directive_id = ids.make_id(hub_app.PROJECT_KEY, "directive", directive_id)
    state = hub_app.current_state()
    if not directive_id or directive_id not in state["entities"]:
        return JsonResponse({"errors": [{"code": "unknown_directive", "id": directive_id}]}, status=404)
    local = "%s--%s" % (directive_id.rsplit(":", 1)[-1], _slug(agent, "agent"))
    eid = ids.make_id(hub_app.PROJECT_KEY, "ack", local)
    existing = state["entities"].get(eid)
    payload = {"type": "ack", "directive": directive_id, "agent": agent,
               "note": str(b.get("note") or "")}
    resp, status = _append("ack", eid, payload,
                           expected_version=existing.get("version") if existing else None,
                           agent=agent, idem=b.get("idem_key") or f"ack:{eid}",
                           etype="ack.recorded")
    if status in (200, 201):
        _retire_if_fully_acked(directive_id, agent)
    return JsonResponse(resp, status=status)


def _retire_if_fully_acked(directive_id, actor):
    """Close a directive once every agent it names has acknowledged it. Without this,
    delivered-and-read directives sit "active" forever and the queue reads as a backlog —
    and a queue people learn to ignore is worse than no queue. Deliberately conservative:
    only explicitly named targets ('all' has no closed roster to check off), and any
    failure is swallowed — an ack that was correctly recorded must not fail because the
    tidy-up afterwards did."""
    try:
        state = hub_app.current_state()
        ent = (state.get("entities") or {}).get(directive_id)
        if not ent or ent.get("status") != "active":
            return
        targets = [str(t).strip().lower() for t in (ent.get("targets") or []) if str(t).strip()]
        if not targets or "all" in targets:
            return
        acked = set()
        for other in (state.get("entities") or {}).values():
            if other.get("type") == "ack" and other.get("directive") == directive_id:
                acked.add(str(other.get("agent") or "").strip().lower())
        if not all(t in acked for t in targets):
            return
        payload = {k: ent[k] for k in ("type", "title", "body_md", "targets",
                                       "remediation_cmd", "answers") if k in ent}
        payload["status"] = "fulfilled"
        _append("directive", directive_id, payload, expected_version=ent.get("version"),
                agent=actor, idem=f"retire:{directive_id}:{ent.get('version')}",
                etype="directive.updated")
    except Exception:                                        # noqa: BLE001
        pass


@writer(scope="presence:write")
def presence_ping(request, b):
    """The seat heartbeat. An ordinary write proves an agent ACTED; only a repeating
    heartbeat proves the seat is still there after the request returned. A session between
    tasks pings here (with the X-Hub-* headers) so the live-console view stays true, and
    the response publishes the shared freshness contract so clients and the board can never
    quietly disagree about what offline means."""
    from hub_core import presence as _presence
    agent = b.get("agent") or request.hub_auth.subject or ""
    if not _valid_agent_name(str(agent)):
        return JsonResponse({"errors": [{"code": "need_agent"}]}, status=422)
    hub_app.observe_presence(agent, request.headers, heartbeat=True)
    return JsonResponse({"data": {"ok": True, "server_time": time.time(),
                                  **_presence.contract()}})


@writer(scope="presence:manage")
def forget_presence(request, b):
    """Drop a stale or phantom seat row. Without this, any machine that ever spoke to the
    hub keeps a presence row until the retirement horizon — a decommissioned laptop or a
    one-off probe sits on the fleet strip looking like a teammate, and the only remedy is
    editing files on the host. Refuses to drop everything: at least one of machine/target
    is required, so a typo can never blank the whole roster."""
    machine = (b.get("machine") or "").strip().lower()
    target = (b.get("target") or "").strip().lower()
    if not machine and not target:
        return JsonResponse({"errors": [{"code": "need_machine_or_target",
            "msg": "pass machine and/or target (the agent name) — refusing to drop everything"}]},
            status=422)
    from hub_core import presence as _presence
    removed = _presence.forget(hub_app.HUB_DIR, machine=machine, agent=target)
    if removed:
        # Presence changed with no ledger event: wake connected cockpits so the phantom
        # leaves every open board now, not at the next unrelated append.
        try:
            hub_app._publish_realtime("presence.observed")
        except Exception:                                    # noqa: BLE001
            pass
    return JsonResponse({"data": {"machine": machine, "target": target, "forgotten": removed}})


# ── Operational error ingest: the surfaces the ledger audit cannot see ──

@writer(scope="error:report")
def app_error(request, b):
    """Errors from a SATELLITE SERVICE this project ships, forwarded by that service's own
    error handler. Without this channel, "is anything broken?" has to be asked once per
    service, by someone who already suspects the answer — so it never gets asked. Services
    forward only what belongs on a queue a human drains: server exceptions and background
    job deaths, never uncaught browser noise from somebody's stale tab. Attributed to the
    APP (source `app.<slug>.<kind>`), never to a person's machine."""
    slug = re.sub(r"[^a-z0-9-]", "", str(b.get("app") or "").strip().lower())[:60]
    if not slug:
        return JsonResponse({"errors": [{"code": "need_app", "msg": "app slug is required"}]}, status=400)
    kind = re.sub(r"[^a-z_]", "", str(b.get("kind") or "server").strip().lower())[:20] or "server"
    row = hub_app.record_error(
        "app.%s.%s" % (slug, kind),
        str(b.get("message") or "Application error")[:800],
        severity=str(b.get("severity") or "error").lower(),
        code=str(b.get("code") or "app_error")[:120],
        details=str(b.get("details") or "")[:2000],
        context={
            "app": slug,
            "component": str(b.get("component") or "app")[:120],
            "operation": str(b.get("operation") or "")[:120],
            "path": str(b.get("path") or "")[:240],
            "machine": str(b.get("host") or "")[:120],
        },
    )
    return JsonResponse({"data": {"recorded": True, "fingerprint": row["fingerprint"]}}, status=201)


@writer(scope="error:report")
def agent_error(request, b):
    """Operational failures on a WORKER's side — a launcher that will not start, tooling
    that cannot write, a client refused upstream. Without this the operational stream
    covers exactly one computer: the one the hub runs on, and every worker-side failure is
    written to a local log read by nobody."""
    agent = b.get("agent") or request.hub_auth.subject or "unknown-agent"
    row = hub_app.record_error(
        "agent.%s.%s" % (agent, str(b.get("source") or "worker")[:60]),
        str(b.get("message") or "Worker reported an operational error")[:800],
        severity=str(b.get("severity") or "error").lower(),
        code=str(b.get("code") or "agent_error")[:120],
        details=str(b.get("details") or "")[:2000],
        context={
            "component": str(b.get("component") or "worker")[:120],
            "operation": str(b.get("operation") or "")[:120],
            "agent": str(agent)[:120],
            "machine": str(b.get("machine") or "")[:120],
        },
    )
    return JsonResponse({"data": {"recorded": True, "fingerprint": row["fingerprint"]}}, status=201)


@writer(scope="error:manage")
def ack_error(request, b):
    """Acknowledge (or reopen) one recurring error signature — the button of the error
    queue. Reporting is truthful both ways: reopening a signature that was never acked is
    a refusal, not a 200 over a row nothing touched."""
    from hub_core import errorlog as _errorlog
    fingerprint = str(b.get("fingerprint") or "")[:32]
    if not fingerprint:
        return JsonResponse({"errors": [{"code": "need_fingerprint"}]}, status=400)
    if b.get("reopen"):
        if not _errorlog.unack(hub_app.HUB_DIR, fingerprint):
            return JsonResponse({"errors": [{"code": "not_acked", "msg":
                "that signature is not acknowledged, so there was nothing to reopen"}]}, status=409)
        hub_app.errors_changed()
        return JsonResponse({"data": {"fingerprint": fingerprint, "reopened": True}})
    entry = _errorlog.ack(hub_app.HUB_DIR, fingerprint,
                          actor=b.get("agent") or request.hub_auth.subject or "",
                          note=b.get("note") or "")
    if not entry:
        return JsonResponse({"errors": [{"code": "ack_failed"}]}, status=500)
    hub_app.errors_changed()
    return JsonResponse({"data": {"fingerprint": fingerprint, "acked": entry}}, status=201)


@writer(scope="error:manage")
def clear_errors(request, b):
    """Clear resolved errors — by age, or by acknowledgement. Never 'everything': a clear
    must never be the operation that destroys evidence of a failure nobody has looked at."""
    from hub_core import errorlog as _errorlog
    only_acked = bool(b.get("only_acked"))
    before = b.get("older_than_hours")
    before_epoch = None
    if before not in (None, ""):
        try:
            before_epoch = time.time() - (float(before) * 3600.0)
        except (TypeError, ValueError):
            return JsonResponse({"errors": [{"code": "bad_older_than_hours"}]}, status=400)
    if before_epoch is None and not only_acked:
        return JsonResponse({"errors": [{"code": "need_bound",
            "msg": "pass older_than_hours and/or only_acked; a clear is never unbounded"}]},
            status=400)
    result = _errorlog.clear(hub_app.HUB_DIR, before_epoch, only_acked)
    hub_app.errors_changed()
    return JsonResponse({"data": result})
