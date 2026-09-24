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

from hub_core import agent_auth, collision, flow, ids, schedule, secretscan, validate
from hub_core.process_lock import ProcessFileLock
from hub_core.store import ConflictError, StoreBusy

from . import hub_app


_AUTH = ContextVar("hub_write_auth", default=None)
# The request being written, so a refusal recorded deep in the append names its route.
_REQUEST = ContextVar("hub_write_request", default=None)


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
        # A tuple scope is ANY-OF: a narrower operation scope that an older, broader grant
        # must keep satisfying (answering a question was once directive:write-only).
        scopes = scope if isinstance(scope, (tuple, list)) else (scope,)
        if not any(auth.allows(one) for one in scopes):
            _record_refusal(request, "insufficient_scope",
                            "a write was refused: subject %r lacks scope %r" % (auth.subject, scope))
            return JsonResponse({"errors": [{"code": "insufficient_scope",
                                              "required": scopes[0] if len(scopes) == 1 else list(scopes),
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
                return JsonResponse({"errors": [{"code": "identity",
                    "msg": "agent must match the immutable credential subject",
                    "subject": auth.subject}]}, status=403)
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
        req_marker = _REQUEST.set(request)
        try:
            response = fn(request, b, *a, **k)
        finally:
            _REQUEST.reset(req_marker)
            _AUTH.reset(marker)
        # Which worker client this hub serves: a client that differs knows it is stale (and,
        # opted in, converges itself); the seat's own version rode in on X-Hub-Client-Version.
        try:
            from . import hub_api
            response["X-Hub-Client-Current"] = hub_api.hub_client_version()
        except Exception:                                    # noqa: BLE001
            pass
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


def _own_repo_name() -> str:
    """The repository this hub resolves commits against, for an error that has to name it."""
    try:
        r = subprocess.run(["git", "-C", str(hub_app.WORK_ROOT), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        url = (r.stdout or "").strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        leaf = url.replace(":", "/").rsplit("/", 1)[-1]
        if leaf:
            return leaf
    except Exception:                                        # noqa: BLE001
        pass
    return getattr(hub_app.WORK_ROOT, "name", "") or "this repository"


_EVIDENCE_SHA = re.compile(r"[0-9a-f]{7,40}")


def _evidence_problem(ev):
    """Return None if the evidence string dereferences to something real, else the reason it
    doesn't. Accepted forms: http(s) URL (status <400), a commit sha in this repo, or an existing
    file path resolved from WORK_ROOT. This proves existence, not confinement: strict URL evidence
    is fetched from the Hub service account's network. 'done' evidence that cannot resolve is
    decoration.

    A LIST IS ORDINARY EVIDENCE: a task spanning six commits is normal, and refusing the list
    teaches people to put the proof in the accept note and leave the evidence field empty — an
    evidence field that stops carrying evidence is a gate everyone routes around. A comma- or
    space-separated list splits ONLY when every part is sha- or URL-shaped (a path or prose with
    spaces stays one item), and each part must dereference; a bad part is NAMED rather than the
    whole string refused."""
    import urllib.request

    ev = (ev or "").strip()
    if not ev:
        return "empty"
    parts = [x.strip() for x in re.split(r"[,\s]+", ev) if x.strip()]
    if len(parts) > 1 and all(_EVIDENCE_SHA.fullmatch(x) or x.startswith(("http://", "https://"))
                              for x in parts):
        for part in parts:
            why = _evidence_problem(part)
            if why:
                return "%s: %s" % (part[:60], why)
        return None
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
            r = subprocess.run(["git", "-C", str(hub_app.WORK_ROOT), "cat-file", "-e", ev + "^{commit}"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return None
            # NAME THE REPOSITORY. "not a commit in this repo" reads as "your sha is wrong" to a
            # worker whose commit lives in another project, where a bare sha can NEVER resolve
            # against this hub's checkout. Say which repo was searched and what to send instead.
            return ("not a commit in %s (the only repository this hub resolves bare shas "
                    "against). For work in another repository, send the commit's URL on its "
                    "forge, e.g. https://git.example.com/<repo>/commit/%s"
                    % (_own_repo_name(), ev[:12]))
        except Exception as e:
            return str(e)[:120]
    try:
        if (hub_app.WORK_ROOT / ev).exists():
            return None
    except OSError:
        pass
    return "not a resolvable URL, commit sha, or existing path from WORK_ROOT"


def _append_with_store(s, type_, eid, payload, *, expected_version, agent, idem, etype,
                       idem_scope=None):
    """Validate the MERGED entity, then append. Returns (response_dict, http_status).

    With ``idem_scope`` (creates only) a key already recorded under that id prefix replays the
    ORIGINAL record: the response names the aggregate the first attempt created, with
    ``replayed: true`` — a retried create is not a new record."""
    # The entity this write is about, from its own aggregate's events -- never a fold of the
    # whole ledger (hub_app.entity_from_store).
    existing = hub_app.entity_from_store(s, eid)
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
                      git_sha=hub_app._git_head(), idem_key=idem,
                      idem_scope=idem_scope if idem else None, **_event_identity(agent))
    except ConflictError as c:
        return ({"errors": [{"code": "conflict", "expected": c.expected, "current": c.current}]}, 409)
    except StoreBusy as busy:
        # BACK-PRESSURE, NOT A FAULT. The lock was held past the wait budget, so nothing was
        # appended — no line, no index row, no fsync. 503 + Retry-After (set by
        # LedgerBusyMiddleware) says exactly that to a caller that already retries writes.
        req = _REQUEST.get()
        hub_app.record_ledger_busy(getattr(req, "path", "") or f"append:{etype}",
                                   getattr(req, "method", "POST"), busy.waited_s, busy)
        return ({"errors": [{"code": "busy", "msg": str(busy), "retry_after": 2}]}, 503)
    if ev.get("seq", 0) > before:
        hub_app.publish_event(ev)
    stored = str(ev.get("aggregate") or eid)
    data = {"id": stored, "version": ev["result_version"], "event": ev["event_id"]}
    if stored != eid:
        data["replayed"] = True     # a retried create: this is the record its first attempt made
    return ({"data": data}, 200)


def _append(type_, eid, payload, *, expected_version, agent, idem, etype, idem_scope=None):
    """Append using a request-owned store and always release its database handle."""
    s = hub_app.store()
    try:
        return _append_with_store(s, type_, eid, payload, expected_version=expected_version,
                                  agent=agent, idem=idem, etype=etype, idem_scope=idem_scope)
    finally:
        s.close()


def _append_create(type_, payload, *, agent, idem, etype):
    """Allocate a fresh numeric id and append, RE-ALLOCATING on a create race.

    The id is derived from one read; a concurrent writer can mint the same id between that read
    and the append, and the create then answers 428 — a refusal that is not the caller's to
    resolve, because the SERVER allocated the id. Re-derive from a fresh read, bounded.

    A RETRIED CREATE IS NOT A NEW RECORD: the id is allocated per attempt, so the per-aggregate
    idempotency never saw a retry's first attempt, and a client whose response timed out minted
    a twin on every re-send. With an idem key the lookup spans every record of the type."""
    resp, status = {"errors": [{"code": "allocate_failed"}]}, 500
    scope = "%s:%s:" % (hub_app.PROJECT_KEY, type_)
    for _attempt in range(3):
        state = hub_app.current_state()
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, type_)
        resp, status = _append(type_, eid, payload, expected_version=None, agent=agent,
                               idem=idem, etype=etype, idem_scope=scope)
        if status != 428:
            break
    return resp, status


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
    refusal = _decision_guard(b, is_create)
    if refusal is not None:
        return refusal
    twins = []
    if is_create:
        state = hub_app.current_state()
        # MINT-TIME TWIN DETECTION. Two seats bitten by the same incident file the same unit
        # minutes apart and nothing on the write path compares them. Titles were never going to
        # catch it — what a pair of duplicates actually SHARE is the surfaces they touch, which is
        # what hub_core.collision compares. This WARNS and never refuses: a false positive that
        # blocks a legitimate mint is worse than a duplicate the operator can see and fold.
        twins = collision.mint_collisions(b, state)
        eid = None
        b.setdefault("status", "todo")
    else:
        eid = b["id"]
        existing = hub_app.entity(eid) or None
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
        resp, status = _append_create("task", payload, agent=agent, idem=b.get("idem_key"),
                                      etype="task.created")
    else:
        resp, status = _append("task", eid, payload, expected_version=b.get("expected_version"),
                               agent=agent, idem=b.get("idem_key"), etype="task.updated")
    if (resp.get("data") or {}).get("replayed"):
        twins = []                  # the retry IS the first record; it is not its own twin
    if twins and status < 400:
        resp["warnings"] = [{"code": "possible_twin",
                             "msg": "this task shares surfaces with existing work; fold if duplicate",
                             "candidates": twins}]
    return JsonResponse(resp, status=status)


def _is_true(value) -> bool:
    return value in (True, 1, "1", "true")


def _decision_guard(b, is_create):
    """A decision (work_kind ``decision``) is a person's call and is NEVER unattended.

    Turning a task INTO a decision clears the flag (fail safe). Asking for ``unattended`` on a
    decision — on create or on a later write, including a later write that only sets the flag on
    a task that already is a decision — is refused with its fix, because the caller expected a
    worker that must not come. Observed once: decision tasks marked unattended were offered in
    priority order and an unattended worker claimed one that rewrote production figures awaiting
    their owner's check."""
    kind = str(b.get("work_kind") or "").strip().lower()
    if kind == "decision":
        if _is_true(b.get("unattended")):
            return JsonResponse({"errors": [{"code": "decision_not_unattended",
                "msg": "a decision is a person's call and is never offered to an unattended "
                       "worker; drop unattended, or change work_kind if this is build work"}]},
                status=409)
        b["unattended"] = False     # becoming (or staying) a decision always clears the flag
        return None
    if _is_true(b.get("unattended")) and not is_create and not kind:
        current = (hub_app.current_state().get("entities") or {}).get(b.get("id")) or {}
        if str(current.get("work_kind") or "").lower() == "decision":
            return JsonResponse({"errors": [{"code": "decision_not_unattended",
                "msg": "%s is a decision (a person's call); it is never offered to an "
                       "unattended worker" % b.get("id")}]}, status=409)
    return None


def _commit_done(eid, token, agent, payload, *, verified_version, auth, idem=None):
    """THE one terminal write every ``done`` takes, whoever grants it.

    ``complete()``, a person's decision and a deploy's automatic close all end here, so ``done``
    always rests on a held lease (re-checked under the lease lock, bound to the subject that
    acquired it), a version bound to exactly the task that was judged, and recorded evidence —
    there is no second way to mint terminal task state. Returns ``(response, status)``; the
    lease is released only when the transition landed."""
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        if not hub_app.lease_authorized(eid, token, auth.subject, auth.credential_id):
            return ({"errors": [{"code": "lease",
                                 "msg": "lease expired or was reclaimed during verification"}]}, 409)
        resp, status = _append("task", eid, payload, expected_version=verified_version,
                               agent=agent, idem=idem, etype="task.transitioned")
        if status == 200:
            hub_app.release_lease(eid, token)
    return resp, status


@writer(scope="task:complete")
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
    if strict:
        # FALSE-GREEN GUARD: evidence must DEREFERENCE — a string nothing can resolve is not evidence.
        bad = {}
        for e in evidence:
            problem = _evidence_problem(e)
            if problem:
                bad[str(e)[:200]] = problem
        if bad:
            return JsonResponse({"errors": [{"code": "evidence_unresolvable",
                "msg": "every evidence_uri must dereference (URL <400 / commit in repo / existing path from WORK_ROOT)",
                "bad": bad}]}, status=422)
    ent = hub_app.entity(eid)
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
    resp, status = _commit_done(eid, token, agent, payload, verified_version=verified_version,
                                auth=request.hub_auth, idem=b.get("idem_key"))
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


@writer(scope="credential:manage")
def tier(request, b):
    """Set one agent's visibility tier: `{agent, tier}` with tier operator | member |
    contributor (or "" to clear). Identity management, so it rides the credential scope. A tier
    is a fact about an identity — never a header a reader sends about itself."""
    from hub_core import veil as _veil
    agent = str(b.get("target") or "").strip().lower()
    value = str(b.get("tier") or "").strip().lower()
    if not _valid_agent_name(agent):
        return JsonResponse({"errors": [{"code": "need_target",
            "msg": "pass target (the agent whose tier to set)"}]}, status=422)
    try:
        tiers = _veil.set_tier(hub_app.HUB_DIR, agent, value)
    except (ValueError, OSError, TimeoutError) as exc:
        return JsonResponse({"errors": [{"code": "bad_tier", "msg": str(exc)}]}, status=422)
    return JsonResponse({"data": {"agent": agent, "tier": tiers.get(agent, ""),
                                  "tiers": tiers}})


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
    if status == 200:
        # THE DEPLOY CLOSES THE LOOP (hub_core.task_completion). Fail-soft: the release record
        # is already durable, and a task that could not be annotated must never 500 it.
        try:
            outcome = close_deployed_tasks(eid, sha)
            if any(outcome.get(k) for k in ("closed", "stepped", "refused", "unchecked")):
                resp = dict(resp, tasks=outcome)
        except Exception as exc:                             # noqa: BLE001
            hub_app.record_error("hub.deploy-close", "deploy-driven task closure failed: %s"
                                 % type(exc).__name__, severity="error",
                                 context={"component": "hub-write", "deploy": eid})
    return JsonResponse(resp, status=status)


DEPLOY_ACTOR = "hub"
_CLOSE_LEASE_TTL_S = 120


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_auto_close(eid, state, why, sha, deploy_id):
    """Write what the automatic close did onto the task, so a task waiting on a close that
    cannot fire SAYS so instead of looking untouched."""
    ent = (hub_app.current_state().get("entities") or {}).get(eid)
    if not ent or ent.get("status") == "done":
        return
    _append("task", eid, {"type": "task", "auto_close": {
        "state": state, "why": str(why or "")[:200], "sha": str(sha or "")[:40],
        "deploy": deploy_id, "at": _utc_now()}},
        expected_version=ent.get("version"), agent=DEPLOY_ACTOR,
        idem="deploy-close-%s:%s:%s" % (state, eid, str(sha or "")[:12]), etype="task.updated")


def close_deployed_tasks(deploy_id, deployed_sha):
    """Step every open task whose recorded commit this verified release contains, and finish
    the UNATTENDED ones through the one done path (a lease the hub holds, the deploy record as
    evidence). A person's task is only stepped. Returns what happened for the record's answer."""
    from hub_core import task_completion
    tasks = hub_app.current_state().get("by_type", {}).get("task", [])
    matches, unchecked = task_completion.plan_matches(tasks, deployed_sha, hub_app.git_is_ancestor)
    closed, stepped, refused = [], [], {}
    for task, commit in matches:
        tid = task["id"]
        at = _utc_now()
        plan = [dict(x) for x in (task.get("plan") or []) if isinstance(x, dict)]
        plan.append(task_completion.deployed_step(commit, deployed_sha, deploy_id, at))
        resp, status = _append("task", tid, {"type": "task", "plan": plan},
                               expected_version=task.get("version"), agent=DEPLOY_ACTOR,
                               idem="deploy-step:%s:%s" % (tid, commit[:12]), etype="task.updated")
        if status != 200:
            refused[tid] = "the deployed checkpoint could not be written (%s)" % status
            continue
        if not task_completion.is_unattended(task):
            stepped.append(tid)
            continue
        why = _hub_finish(tid, commit, deployed_sha, deploy_id)
        if why:
            refused[tid] = why
            _record_auto_close(tid, "refused", why, commit, deploy_id)
        else:
            closed.append(tid)
    out = {"closed": closed, "stepped": stepped, "refused": refused}
    if unchecked:
        out["unchecked"] = ("the repository could not answer whether %s %s contained in %s; only a "
                            "task naming the served sha exactly could close"
                            % (", ".join(u[:12] for u in unchecked[:5]),
                               "is" if len(unchecked) == 1 else "are", deployed_sha[:12]))
    return out


def _hub_finish(tid, commit, deployed_sha, deploy_id):
    """Finish one unattended task as the hub: claim a short lease (refused when anyone else
    holds it — the hub never finishes work another agent holds), then the shared done path.
    Returns '' on success, else the reason."""
    from hub_core import agent_auth
    auth = agent_auth.AuthContext(subject=DEPLOY_ACTOR, credential_id="hub-deploy-close",
                                  scopes=("task:complete",), actor_kind="hub",
                                  mode="hub-internal")
    res = hub_app.claim(tid, DEPLOY_ACTOR, ttl_s=_CLOSE_LEASE_TTL_S, auth_subject=auth.subject,
                        credential_id=auth.credential_id, actor_kind=auth.actor_kind)
    if not res.get("ok"):
        return "held by %s" % res.get("held_by")
    ent = (hub_app.current_state().get("entities") or {}).get(tid)
    if not ent or ent.get("status") == "done":
        if res.get("created"):
            hub_app.release_lease(tid, res["token"])
        return "gone or already done"
    note = ("Closed by the hub: its recorded commit %s is live in verified build %s (%s)."
            % (commit[:12], deployed_sha[:12], deploy_id))
    payload = {"type": "task", "status": "done", "verified_by": [note],
               "evidence_uri": [deploy_id, commit], "auto_close": {
                   "state": "closed", "sha": commit[:40], "deploy": deploy_id, "at": _utc_now()}}
    resp, status = _commit_done(tid, res["token"], DEPLOY_ACTOR, payload,
                                verified_version=ent.get("version"), auth=auth,
                                idem="deploy-close:%s:%s" % (tid, deployed_sha[:12]))
    if status != 200:
        if res.get("created"):
            hub_app.release_lease(tid, res["token"])
        return "done refused: %s %s" % (status, json.dumps(resp)[:120])
    return ""


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
            return JsonResponse({"errors": [{"code": verdict["state"],
                                             "msg": verdict["reason"]}]}, status=409)
        res = hub_app.claim(eid, agent, ttl_s=ttl,
                            auth_subject=request.hub_auth.subject,
                            credential_id=request.hub_auth.credential_id,
                            actor_kind=request.hub_auth.actor_kind,
                            session=_claim_session(request, b),
                            machine=str(request.headers.get("X-Hub-Machine") or ""))
        if not res["ok"]:
            return JsonResponse(res, status=409)
        if status != "in_progress":
            transition, transition_status = _append(
                "task", eid, {"type": "task", "status": "in_progress"},
                expected_version=ent.get("version"), agent=agent,
                idem=b.get("idem_key"), etype="task.transitioned",
            )
            if keep_lease_on_raced_transition(res, transition_status):
                # A RENEWAL whose transition lost an OCC race is not a failed claim: this worker
                # already held the lease, and the version moved because something else — very
                # often its own retried request, after a timeout — already did the work.
                # Releasing here tore down a valid fencing token and made the next complete()
                # answer "claim the task first" to a worker that had claimed. Hand back the
                # token it still holds and the version as it now stands.
                fresh = hub_app.current_state().get("entities", {}).get(eid) or {}
                res["version"] = fresh.get("version", ent.get("version"))
                res["transition_raced"] = True
                return JsonResponse(res, status=200)
            if transition_status != 200:
                if res.get("created"):
                    hub_app.release_lease(eid, res["token"])
                return JsonResponse(transition, status=transition_status)
            res["version"] = transition["data"]["version"]
        else:
            res["version"] = ent.get("version")
    return JsonResponse(res, status=200 if res["ok"] else 409)


def keep_lease_on_raced_transition(res, transition_status) -> bool:
    """Should a failed todo->in_progress transition LEAVE the lease alone?

    Yes exactly when this call did not create the lease and the transition lost an OCC race.
    A lease this call CREATED is still cleaned up on failure: nothing else holds it. The rule
    fails toward keeping: a stranded lease expires on its own, a destroyed one strands the
    worker that holds its token."""
    return transition_status == 409 and not (res or {}).get("created")


def _claim_session(request, b) -> str:
    """The console making this claim: an explicit `session` in the body, else the observed
    presence header every client already sends. Empty when neither is present."""
    value = b.get("session") if isinstance(b.get("session"), str) else ""
    return (value or request.headers.get("X-Hub-Session") or "").strip()[:64]


@writer(scope="task:release")
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
        candidates = []
        for task in state.get("entities", {}).values():
            if task.get("type") != "task" or task.get("id") in lease_ids:
                continue
            verdict = flow.classify(task, state.get("flags", {}).get(task.get("id"), {}), None)
            if verdict["available"]:
                candidates.append(task)
        if not candidates:
            return JsonResponse({"errors": [{"code": "no_ready_task"}]}, status=409)
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
                            session=_claim_session(request, b),
                            machine=str(request.headers.get("X-Hub-Machine") or ""))
        if not res["ok"]:
            return JsonResponse(res, status=409)
        if task.get("status") != "in_progress":
            transition, code = _append("task", eid, {"type": "task", "status": "in_progress"},
                                       expected_version=task.get("version"), agent=agent,
                                       idem=b.get("idem_key"), etype="task.transitioned")
            if code != 200:
                if res.get("created"):
                    hub_app.release_lease(eid, res["token"])
                return JsonResponse(transition, status=code)
            res["version"] = transition["data"]["version"]
        res["task"] = task
        res["routing"] = routing
    return JsonResponse(res)


@writer(scope="task:heartbeat")
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


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _session_header(request) -> str:
    """The sender's console id from X-Hub-Session, when it is shaped like one."""
    value = str(request.headers.get("X-Hub-Session") or "").strip()
    return value if _SESSION_ID.fullmatch(value) else ""


def _reader_tier(agent) -> str:
    try:
        from . import veil as _veil
        return _veil.tier_of(agent)
    except Exception:                                        # noqa: BLE001
        return ""


@writer(scope="ask:write")
def ask(request, b):
    """A question as a first-class board entity that DELIVERS.

    The note lands tagged `question`+`open`, carries its asker in a stable `asker` field
    (never derived from provenance, which answering REWRITES), and the asking console's
    session in `from_session`, so the answer is delivered back to the console that asked. It
    wakes connected cockpits through the push plane the moment it exists and is returned by
    /hub/inbox/wait for whoever should answer it:

        to=<agent>       addressed: that agent's inbox, nobody else's
        (no to)          the operator's
        waited too long  every console's (HUB_ASK_UNSTICK_S; never a human-only gate)

    `human_only: true` files a gate — something only a person can do — which is delivered to
    the operator and never widened. Idempotent on the exact wording (the id is a digest of it),
    and guarded against restatements: a question the board already has is refused with the
    matching ids instead of minting another copy. `anyway: true` is the escape hatch."""
    agent = b.get("agent") or ""
    if not _valid_agent_name(agent):
        return JsonResponse({"errors": [{"code": "need_agent",
            "msg": "ask requires a valid lowercase agent name (the asker) in the payload"}]},
            status=422)
    text = str(b.get("question") or "").strip()
    if not text:
        return JsonResponse({"errors": [{"code": "need_question"}]}, status=400)
    to = str(b.get("to") or "").strip().lower()
    if to and not _valid_agent_name(to):
        return JsonResponse({"errors": [{"code": "bad_addressee",
            "msg": "`to` must be a lowercase agent name"}]}, status=422)
    if to and to == agent:
        return JsonResponse({"errors": [{"code": "self_addressed",
            "msg": "an ask addressed to its own asker reaches nobody"}]}, status=422)
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
    tags = ["question", "open"] + (["human-only"] if b.get("human_only") else [])
    payload = {"type": "note", "category": "context", "title": text[:300],
               "asker": agent, "from_agent": agent, "status": "standing", "tags": tags,
               "body_md": str(b.get("context") or "")}
    if to:
        payload["to"] = to
    session = _session_header(request)
    if session:
        payload["from_session"] = session
    tier = _reader_tier(agent)
    if tier and tier != "member":
        # A contributor's ask says so, so an answer is checked against what that tier may see.
        payload["tier"] = tier
    related = [t for t in (b.get("relates_to") or []) if isinstance(t, str) and ":" in t]
    if related:
        payload["relates_to"] = related
    resp, status = _append("note", eid, payload,
                           expected_version=existing.get("version") if existing else None,
                           agent=agent, idem=b.get("idem_key"), etype="note.created")
    return JsonResponse(resp, status=status)


@writer(scope=("ask:answer", "directive:write"))
def answer(request, b):
    """Close a question: reply to the asker AND retire the open question, as ONE verb.

    ANYONE WHO MAY ANSWER, MAY ANSWER — the `ask:answer` scope (or the older, broader
    `directive:write`). The gate is on the act, not the vehicle: the reply travels as a
    directive, but this one is built here with `targets` = the asker read from the QUESTION
    (never chosen by the caller) and `answers` = an existing question, so it cannot broadcast
    and cannot carry a standing instruction. A single-answerer funnel is how a board's open
    asks pile up behind the one person allowed to clear them.

    The reply is ADDRESSED: to the asker, and to the asking CONSOLE when the question recorded
    one (never broadcast to every window the asker has open). Re-answering updates the one
    answer directive in place and bumps its `delivery_revision`, so a correction is a new
    delivery that an ack of the old text does not close. A reply to a contributor that names a
    hidden facet is refused unless the answerer passes `disclose: true` (and holds
    `veil:disclose`), and the disclosure is recorded."""
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
    # re-answered directive at the answerer themselves.
    asker = str(note_ent.get("asker")
                or (note_ent.get("provenance") or {}).get("agent") or "").strip().lower()
    if not _valid_agent_name(asker):
        return JsonResponse({"errors": [{"code": "unknown_asker",
            "msg": "the question does not name who asked; issue a directive instead"}]}, status=409)

    # THE ANSWER VEIL: the person who asked cannot be told a thing exists by the act of
    # answering them.
    try:
        from . import veil as _veil
        asker_tier = _veil.tier_of(asker)
        hit = _veil.Veil(asker_tier).hit(text)
    except Exception:                                        # noqa: BLE001
        asker_tier, hit = "", None
    if hit:
        if not (b.get("disclose") and request.hub_auth.allows("veil:disclose")):
            return JsonResponse({"errors": [{"code": "answer_would_disclose", "facet": hit,
                "tier": asker_tier,
                "msg": "this reply names the '%s' facet, which %s cannot see at tier %s. "
                       "Reword it, or pass disclose=true (veil:disclose scope) to send it "
                       "anyway and record the disclosure." % (hit, asker, asker_tier)}]},
                status=422)
        try:
            hub_app.record_error(
                "hub.veil", "a facet was disclosed to a %s in an answer" % asker_tier,
                severity="warning", code="veil_disclosed",
                context={"component": "veil", "facet": hit, "asker": asker,
                         "question": question_id, "by": request.hub_auth.subject})
        except Exception:                                    # noqa: BLE001
            pass

    question_text = str(note_ent.get("title") or "")
    payload = {
        "type": "directive",
        "title": ("Answer: %s" % question_text)[:300],
        "body_md": text + "\n\n---\nIn answer to your question: " + question_text,
        "targets": [asker],
        "status": "active",
        "answers": question_id,
    }
    # The ORIGINAL ask identifies the destination console — not the answering request.
    asking_session = str(note_ent.get("from_session") or "")
    if _SESSION_ID.fullmatch(asking_session):
        payload["session"] = asking_session
    agent = b.get("agent", request.hub_auth.subject)
    # One question has exactly ONE answer directive; the find-or-create is re-run on an OCC
    # refusal, because both the id and the version it derives come from a read a concurrent
    # answer can beat. Bounded: a race surviving three fresh reads is returned loudly.
    resp, status, eid = {"errors": [{"code": "occ_retry_exhausted"}]}, 409, ""
    for attempt in range(3):
        if attempt:
            state = hub_app.current_state()
        existing_dir = next(
            (e for e in state["entities"].values()
             if isinstance(e, dict) and e.get("type") == "directive"
             and e.get("answers") == question_id), None)
        if existing_dir:
            eid = existing_dir["id"]
            if all(existing_dir.get(k) == payload.get(k)
                   for k in ("title", "body_md", "targets", "session")):
                resp, status = {"data": {"id": eid, "version": existing_dir.get("version"),
                                         "unchanged": True}}, 200
                break
            payload["delivery_revision"] = int(existing_dir.get("delivery_revision") or 0) + 1
            resp, status = _append("directive", eid, payload,
                                   expected_version=existing_dir.get("version"),
                                   agent=agent, idem=b.get("idem_key"), etype="directive.issued")
        else:
            eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "directive")
            payload["delivery_revision"] = 1
            resp, status = _append("directive", eid, payload, expected_version=None,
                                   agent=agent, idem=b.get("idem_key"), etype="directive.issued")
        if status not in (409, 428):
            break
    if status not in (200, 201):
        return JsonResponse(resp, status=status)

    # Retire the question by flipping its tags: `open` -> `answered`. The tag, not the
    # status enum, is what marks a question as awaiting an answer.
    still_open = False
    try:
        tags = [t for t in (note_ent.get("tags") or []) if str(t).lower() != "open"]
        if "answered" not in [str(t).lower() for t in tags]:
            tags.append("answered")
        if tags != list(note_ent.get("tags") or []):
            closed = {k: v for k, v in note_ent.items() if k not in ("version", "provenance")}
            closed["tags"] = tags
            _retire_resp, retire_status = _append(
                "note", question_id, closed, expected_version=note_ent.get("version"),
                agent=agent, idem=None, etype="note.created")
            still_open = retire_status not in (200, 201)
    except Exception:                                    # noqa: BLE001 - never lose the reply
        still_open = True
    if still_open:
        # Loud, in the response the answerer reads: an answer that leaves its question open
        # is how a board's question count only ever grows.
        resp.setdefault("data", {})["question_still_open"] = True
    hub_app.receipt("question", question_id, "resolved", agent=asker,
                    detail="answered by %s" % agent)
    resp.setdefault("data", {})["directive"] = eid
    resp["data"]["asker"] = asker
    if payload.get("session"):
        resp["data"]["session"] = payload["session"]
    if payload.get("delivery_revision"):
        resp["data"]["delivery_revision"] = payload["delivery_revision"]

    # CRYSTALLIZE is OPT-IN: the knowledge surface rides every future duplicate-ask check, so
    # minting indiscriminately taxes it to remember something true for one afternoon. The
    # answerer decides, because only they know which of the two this was.
    if not b.get("crystallize") or hit:
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
    subject. Stable id => replay-safe for offline retry queues.

    A REVISIONED answer must be acked at the revision the reader actually read: a missing
    revision answers 428 with the current one, a stale revision 409 — a correction is never
    marked read by a receipt for the text it replaced."""
    agent = b.get("agent") or ""
    if not _valid_agent_name(agent):
        return JsonResponse({"errors": [{"code": "need_agent"}]}, status=422)
    directive_id = str(b.get("directive") or "")
    if directive_id and ":" not in directive_id:
        directive_id = ids.make_id(hub_app.PROJECT_KEY, "directive", directive_id)
    state = hub_app.current_state()
    target = state["entities"].get(directive_id) if directive_id else None
    if not target:
        return JsonResponse({"errors": [{"code": "unknown_directive", "id": directive_id}]}, status=404)
    kind = "answer" if target.get("answers") else "directive"
    revision = target.get("delivery_revision")
    if revision is not None:
        received = b.get("delivery_revision")
        if received is None:
            hub_app.receipt(kind, directive_id, "failed", agent=agent, outcome="need_revision")
            return JsonResponse({"errors": [{"code": "need_delivery_revision",
                "current_revision": revision,
                "msg": "read the current answer with `inbox`, then ack its delivery_revision"}]},
                status=428)
        if type(received) is not int or received != revision:
            hub_app.receipt(kind, directive_id, "failed", agent=agent, outcome="stale_revision",
                            detail="acked revision %r, current %r" % (received, revision))
            return JsonResponse({"errors": [{"code": "answer_changed",
                "current_revision": revision,
                "msg": "the answer was corrected after the revision you read — read the "
                       "correction before acknowledging it"}]}, status=409)
    local = "%s--%s" % (directive_id.rsplit(":", 1)[-1], _slug(agent, "agent"))
    eid = ids.make_id(hub_app.PROJECT_KEY, "ack", local)
    existing = state["entities"].get(eid)
    payload = {"type": "ack", "directive": directive_id, "agent": agent,
               "note": str(b.get("note") or "")}
    idem = b.get("idem_key") or f"ack:{eid}"
    if revision is not None:
        payload["delivery_revision"] = revision
        idem += f":revision:{revision}"
    resp, status = _append("ack", eid, payload,
                           expected_version=existing.get("version") if existing else None,
                           agent=agent, idem=idem, etype="ack.recorded")
    if status in (200, 201):
        hub_app.receipt(kind, directive_id, "delivered", agent=agent,
                        detail=str(b.get("note") or "")[:200])
        _retire_if_fully_acked(directive_id, agent)
    else:
        hub_app.receipt(kind, directive_id, "failed", agent=agent, outcome="append_%s" % status)
    return JsonResponse(resp, status=status)


@writer(scope="message:write")
def message(request, b):
    """Agent-to-agent mail: `{to, note, title?, session?, machine?}` — no operator in the loop.

    The note lands tagged `message`+`open` with `to`, the sender in `from_agent` and the
    sender's console in `from_session` (from X-Hub-Session), so a reply can be addressed back to
    exactly that console. `session` addresses one of the recipient's consoles; `machine` pins
    delivery to one of their computers. It is delivered by the recipient's inbox/wait like any
    other item and retired by /hub/api/message/ack. Idempotent on (sender, recipient, text)."""
    agent = b.get("agent") or request.hub_auth.subject or ""
    to = str(b.get("to") or "").strip().lower()
    note_text = str(b.get("note") or b.get("body") or "").strip()
    if not _valid_agent_name(str(agent)) or not _valid_agent_name(to):
        return JsonResponse({"errors": [{"code": "need_sender_and_recipient",
            "msg": "message needs a valid sender (agent) and recipient (to)"}]}, status=422)
    if not note_text:
        return JsonResponse({"errors": [{"code": "need_note"}]}, status=400)
    target_session = str(b.get("session") or "").strip()
    if target_session and not _SESSION_ID.fullmatch(target_session):
        return JsonResponse({"errors": [{"code": "bad_session"}]}, status=422)
    title = str(b.get("title") or note_text.splitlines()[0])[:300]
    local = "m-%s-%s" % (_slug(agent, "agent"), hashlib.sha256(
        ("%s|%s|%s" % (to, target_session, note_text)).encode("utf-8")).hexdigest()[:8])
    eid = ids.make_id(hub_app.PROJECT_KEY, "note", local)
    state = hub_app.current_state()
    existing = state["entities"].get(eid)
    if existing and "open" in [str(t).lower() for t in (existing.get("tags") or [])]:
        return JsonResponse({"data": {"id": eid, "already": True}})
    payload = {"type": "note", "category": "context", "title": title, "status": "standing",
               "tags": ["message", "open"], "to": to, "from_agent": str(agent),
               "body_md": note_text}
    sender_session = _session_header(request)
    if sender_session:
        payload["from_session"] = sender_session
    if target_session:
        payload["session"] = target_session
    machine = str(b.get("machine") or "").strip().lower()[:60]
    if machine:
        payload["machine"] = machine
    resp, status = _append("note", eid, payload,
                           expected_version=existing.get("version") if existing else None,
                           agent=str(agent), idem=b.get("idem_key"), etype="note.created")
    if status in (200, 201):
        resp.setdefault("data", {})["to"] = to
        if target_session:
            resp["data"]["session"] = target_session
    return JsonResponse(resp, status=status)


@writer(scope="message:write")
def message_ack(request, b):
    """Retire an addressed message once it reached its recipient. Only the recipient may retire
    it — a REFUSED delivery is recorded as a failed receipt, because without it an item that
    never reached anyone looks exactly like one that did. A message pinned to one machine is
    retired only from that machine."""
    mid = str(b.get("id") or "").strip()
    if not mid:
        return JsonResponse({"errors": [{"code": "need_id"}]}, status=400)
    if ":" not in mid:
        mid = ids.make_id(hub_app.PROJECT_KEY, "note", mid)
    state = hub_app.current_state()
    ent = (state.get("entities") or {}).get(mid)
    tags = [str(t).lower() for t in ((ent or {}).get("tags") or [])]
    if not ent or ent.get("type") != "note" or "message" not in tags:
        return JsonResponse({"errors": [{"code": "no_such_message", "msg": mid}]}, status=404)
    agent = str(b.get("agent") or request.hub_auth.subject or "").lower()
    to = str(ent.get("to") or "").lower()
    if to and to != agent and request.hub_auth.mode == "scoped-agent":
        hub_app.receipt("message", mid, "failed", agent=to, outcome="not_recipient",
                        detail="%s tried to retire mail addressed to %s" % (agent, to))
        return JsonResponse({"errors": [{"code": "not_recipient",
            "msg": "that message is addressed to %s" % to}]}, status=403)
    pinned = str(ent.get("machine") or "").strip().lower()
    reader_machine = str(request.headers.get("X-Hub-Machine") or "").strip().lower()
    if pinned and reader_machine and pinned != reader_machine:
        hub_app.receipt("message", mid, "failed", agent=to, outcome="other_machine",
                        detail="pinned to %s, retired from %s" % (pinned, reader_machine))
        return JsonResponse({"errors": [{"code": "other_machine",
            "msg": "that message is for %s, not %s" % (pinned, reader_machine)}]}, status=409)
    if "open" not in tags:
        return JsonResponse({"data": {"id": mid, "already": True}})
    closed = {k: v for k, v in ent.items() if k not in ("version", "provenance")}
    closed["tags"] = [t for t in (ent.get("tags") or []) if str(t).lower() != "open"] + ["delivered"]
    via = str(b.get("via") or "")[:40]
    if via:
        closed["found_at"] = "delivered via %s" % via
    resp, status = _append("note", mid, closed, expected_version=ent.get("version"),
                           agent=agent or "agent", idem=b.get("idem_key") or f"msgack:{mid}",
                           etype="note.created")
    hub_app.receipt("message", mid, "delivered" if status in (200, 201) else "failed",
                    agent=to or agent, outcome="ok" if status in (200, 201) else "append_%s" % status,
                    detail=("delivered via %s" % via) if via else "")
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
        from hub_core.inbox import ack_covers
        acked = set()
        for other in (state.get("entities") or {}).values():
            if other.get("type") == "ack" and ack_covers(ent, other):
                acked.add(str(other.get("agent") or "").strip().lower())
        if not all(t in acked for t in targets):
            return
        payload = {k: ent[k] for k in ("type", "title", "body_md", "targets",
                                       "remediation_cmd", "answers", "session",
                                       "delivery_revision") if k in ent}
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
    # The session DIGEST rides the heartbeat body: what a supervisor distilled from the
    # session's own activity (phase, doing, narration, last result, targets) and, for an
    # unattended run, its lifecycle (kind, run, subject, started/ended, outcome). Bounded and
    # typed by hub_core.presence; unknown keys are dropped.
    digest = b.get("session_state") if isinstance(b.get("session_state"), dict) else {}
    hub_app.observe_presence(agent, request.headers, heartbeat=True, extra=digest)
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


# ── The agents' own feed: what they DID, in their words ──

@writer(scope="update:write")
def agent_update(request, b):
    """Post one first-person line to the updates feed: ``{summary, kind?, evidence?, item?}``.

    The agent is bound by the write seam (a scoped credential's subject, never a body claim), so
    an update can never be attributed to someone else; the machine comes from X-Hub-Machine. A
    write lost to contention answers a RETRYABLE 503 naming the reason and records a warning row
    — a lost narrative line is real data loss that must be visible, but not a defect to page on."""
    from hub_core import updates as _updates
    summary = str(b.get("summary") or "").strip()
    if not summary:
        return JsonResponse({"errors": [{"code": "need_summary"}]}, status=400)
    agent = b.get("agent") or request.hub_auth.subject
    row, reason = _updates.record(
        hub_app.HUB_DIR, agent=agent, kind=str(b.get("kind") or "fixed"), summary=summary,
        machine=request.headers.get("X-Hub-Machine") or str(b.get("machine") or ""),
        evidence=str(b.get("evidence") or ""), item=str(b.get("item") or ""),
        by=str(b.get("by") or ""))
    if row is None:
        try:
            hub_app.record_error(
                "hub.agent-updates", "an agent update was dropped: %s" % reason,
                severity="warning", code="update_write_failed",
                context={"component": "agent-updates", "agent": str(agent)[:120]})
        except Exception:                                    # noqa: BLE001
            pass
        return JsonResponse({"errors": [{"code": "update_write_failed", "reason": reason,
            "msg": "the updates feed was busy and this line was not appended; retry is safe"}]},
            status=503)
    hub_app._publish_realtime("updates.recorded")
    return JsonResponse({"data": row}, status=201)


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


# ---- decisions: a person's call, never an agent's -------------------------------------------

def _deciders():
    """Who may decide: HUB_DECIDERS (comma list, settings or env), default the operator."""
    raw = hub_app._dj_setting("HUB_DECIDERS") or os.environ.get("HUB_DECIDERS") or ""
    names = {x.strip().lower() for x in str(raw).split(",") if x.strip()}
    operator = str(hub_app._dj_setting("HUB_OPERATOR_AGENT") or os.environ.get("HUB_OPERATOR_AGENT")
                   or "operator").strip().lower()
    return names or {operator}


def board_link(eid):
    """A deep link that opens one task's detail on the board (the board's own #task-<local>)."""
    return "/hub/#task-%s" % str(eid).rsplit(":", 1)[-1]


@writer(scope="task:decide")
def decide_task(request, b):
    """Decide a decision task: ``{id, decision, then: file|close|reply}``.

    A decision is a person's call, so an agent can never make one: the caller must hold a
    scoped credential whose immutable subject is a named decider (HUB_DECIDERS, default the
    operator). The shared-root token is refused — it is how automation writes, and a decision it
    made would be indistinguishable from one a person made.

    * ``file``  — mint the build task the decision leads to (its acceptance leads with the
                  decision; unattended so the queue takes it) and close the decision naming it.
    * ``close`` — record the decision with nothing to build.
    * ``reply`` — a question or answer back, NOT a decision: it lands on the task's trail, the
                  decision stays open (``decision`` stays absent), and the filer is messaged.

    Closing goes through the one done path (a lease the decider holds for the moment of the
    write, evidence = the board link). A second press returns the first result, never a second
    build task: one lock, and the entity is re-read inside it."""
    auth = request.hub_auth
    if auth.mode != "scoped-agent" or auth.subject.lower() not in _deciders():
        return JsonResponse({"errors": [{"code": "decision_needs_a_person",
            "msg": "a decision is a person's call and an agent cannot make it: decide with a "
                   "credential issued to a named decider (HUB_DECIDERS)"}]}, status=403)
    person = auth.subject
    eid = str(b.get("id") or "").strip()
    text = " ".join(str(b.get("decision") or "").split())
    then = str(b.get("then") or "").strip().lower()
    if not eid:
        return JsonResponse({"errors": [{"code": "need_id"}]}, status=400)
    if len(text) < 3:
        return JsonResponse({"errors": [{"code": "need_decision",
                                         "msg": "say what was decided, or what you are asking"}]},
                            status=422)
    if then not in ("file", "close", "reply"):
        return JsonResponse({"errors": [{"code": "need_then",
            "msg": "then is 'file' (file the work this leads to), 'close' (nothing to build) or "
                   "'reply' (a question or answer back; the decision stays open)"}]}, status=422)
    with ProcessFileLock(hub_app.CLAIMS, name=".decide.lock", timeout=30):
        ent = hub_app.current_state().get("entities", {}).get(eid)
        if not ent or ent.get("type") != "task":
            return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
        if str(ent.get("work_kind") or "").lower() != "decision":
            return JsonResponse({"errors": [{"code": "not_a_decision",
                "msg": "%s is build work, not a decision" % eid}]}, status=409)
        made = ent.get("decision") if isinstance(ent.get("decision"), dict) else None
        if made:
            return JsonResponse({"data": dict(made, id=eid, status=ent.get("status"),
                                              already_decided=True)})
        if ent.get("status") not in ("todo", "blocked", "in_progress"):
            return JsonResponse({"errors": [{"code": "not_open", "status": ent.get("status")}]},
                                status=409)
        expected = b.get("expected_version")
        if expected is not None and expected != ent.get("version"):
            return JsonResponse({"errors": [{"code": "conflict",
                                             "current_version": ent.get("version")}]}, status=409)
        when = _utc_now()
        plan = [dict(x) for x in (ent.get("plan") or []) if isinstance(x, dict)]
        if then == "reply":
            plan.append({"step": "Reply", "done": True, "kind": "checkpoint",
                         "note": ("Reply from %s on %s: %s" % (person, when, text))[:600],
                         "note_at": when})
            resp, status = _append("task", eid, {"type": "task", "plan": plan},
                                   expected_version=ent.get("version"), agent=person, idem=None,
                                   etype="task.updated")
            if status != 200:
                return JsonResponse(resp, status=status)
            # The FILER is who created the decision (stamped once by the fold), never the last
            # writer: an unrelated priority edit must not redirect the reply to its editor.
            filer = str((ent.get("provenance") or {}).get("created_by") or "").strip().lower()
            paged = ""
            # The shared-root compatibility subject is not a seat anybody reads; a reply to a
            # decision it filed has nobody to page, and says so (paged: "").
            if filer and filer not in (person.lower(), "shared-root"):
                state = hub_app.current_state()
                did = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "directive")
                note = {"type": "directive", "status": "active", "targets": [filer],
                        "title": ("Reply on %s: %s" % (eid, text))[:200],
                        "body_md": ("%s replied on %s (%s) instead of deciding:\n\n%s\n\nAnswer on "
                                    "the task (step it with your answer); once a checkpoint newer "
                                    "than the reply is on its trail the decision reaches them "
                                    "again: %s" % (person, eid, ent.get("title") or "", text,
                                                   board_link(eid)))}
                _r, nstatus = _append("directive", did, note, expected_version=None, agent=person,
                                      idem="decision-reply:%s:%s" % (eid, when), etype="directive.issued")
                paged = filer if nstatus == 200 else ""
            return JsonResponse({"data": {"id": eid, "status": ent.get("status"), "replied": True,
                                          "reply": {"text": text, "by": person, "at": when},
                                          "paged": paged, "decision": None}})
        stamp = "Decided by %s on %s: %s" % (person, when, text)
        followup = ""
        if then == "file":
            priority = str(ent.get("priority") or "").upper()
            subject = str(ent.get("title") or eid)
            payload = {"type": "task", "status": "todo", "work_kind": "product",
                       "unattended": True,
                       "priority": priority if priority in ("P0", "P1", "P2") else "P2",
                       "title": ("Build the decision: %s" % subject)[:200],
                       "acceptance": ("DECIDED by %s on %s: %s\n\nThis task builds that decision. "
                                      "It came from %s (%s). The question and its context: %s"
                                      % (person, when[:10], text, eid, subject,
                                         ent.get("acceptance") or ""))}
            if ent.get("project"):
                payload["project"] = ent["project"]
            resp, status = _append_create("task", payload, agent=person,
                                          idem="decision-file:%s" % eid, etype="task.created")
            if status != 200:
                return JsonResponse(resp, status=status)
            followup = (resp.get("data") or {}).get("id") or ""
            stamp += " Build task: %s." % (followup or "filed")
        plan.append({"step": "Decided", "done": True, "kind": "checkpoint",
                     "note": stamp[:600], "note_at": when})
        record = {"text": text[:2000], "decided_by": person, "decided_at": when, "then": then}
        if followup:
            record["followup"] = followup
        res = hub_app.claim(eid, person, ttl_s=_CLOSE_LEASE_TTL_S, auth_subject=auth.subject,
                            credential_id=auth.credential_id, actor_kind=auth.actor_kind)
        if not res.get("ok"):
            return JsonResponse({"errors": [{"code": "held", "held_by": res.get("held_by"),
                "msg": "someone holds this decision's lease; release it before deciding"}]},
                status=409)
        update = {"type": "task", "status": "done", "plan": plan, "unattended": False,
                  "decision": record, "verified_by": [stamp[:600]],
                  "evidence_uri": [board_link(followup or eid)]}
        resp, status = _commit_done(eid, res["token"], person, update,
                                    verified_version=ent.get("version"), auth=auth)
        if status != 200:
            if res.get("created"):
                hub_app.release_lease(eid, res["token"])
            return JsonResponse(resp, status=status)
    return JsonResponse({"data": dict(record, id=eid, status="done", followup=followup,
                                      followup_url=board_link(followup) if followup else "")})


# ---- hand / unclaim: letting go of a task, including one an orphaned console holds -------------

def _releasable_lease(request, b, eid):
    """The lease on ``eid`` this caller may release: the one whose fencing token it presents, or
    — the orphaned-lease remedy — one held by the SAME agent from a console that is no longer
    live. A lease a live console of the same agent holds, or another agent's, is never touched.
    Returns ``(lease_or_None, refusal_or_None)``."""
    lease = next((row for row in hub_app.leases() if row.get("task") == eid), None)
    if not lease:
        return None, None
    token = b.get("token") if isinstance(b.get("token"), str) else ""
    if token:
        if lease.get("token") != token or not hub_app.lease_authorized(
                eid, token, request.hub_auth.subject, request.hub_auth.credential_id):
            return None, JsonResponse({"errors": [{"code": "lease_mismatch"}]}, status=409)
        return lease, None
    agent = str(b.get("agent") or "")
    if lease.get("agent") != agent:
        return None, JsonResponse({"errors": [{"code": "held", "held_by": lease.get("agent"),
            "msg": "another agent holds this task"}]}, status=409)
    if lease.get("auth_subject") and lease.get("auth_subject") != request.hub_auth.subject:
        return None, JsonResponse({"errors": [{"code": "lease_subject_mismatch"}]}, status=409)
    sid = str(lease.get("session") or "")[:8]
    live = {str(r.get("session") or "")[:8] for r in hub_app.live_sessions()
            if not r.get("finished")}
    if not sid or sid in live:
        return None, JsonResponse({"errors": [{"code": "lease_live",
            "msg": "the lease is held by a live console (%s); release it from there, or pass its "
                   "token" % (sid or "unknown session")}]}, status=409)
    return lease, None


def _lifecycle_row(plan, kind, note, at):
    """Append (or count up) a scheduler row. A recurring hand-back is ONE row counting itself,
    never N rows that read as N checkpoints of work."""
    plan = [dict(x) for x in (plan or []) if isinstance(x, dict)]
    last = plan[-1] if plan else None
    if last and last.get("kind") == kind and last.get("lifecycle"):
        last["times"] = int(last.get("times") or 1) + 1
        last["note"] = note[:600]
        last["note_at"] = at
        return plan
    plan.append({"step": {"handed_back": "Handed back to the queue",
                          "lease_released": "Lease released"}.get(kind, kind),
                 "done": True, "kind": kind, "lifecycle": True, "note": note[:600],
                 "note_at": at})
    return plan


def _let_go(request, b, *, kind):
    eid = str(b.get("id") or "").strip()
    agent = str(b.get("agent") or "").strip()
    if not eid or not agent:
        return JsonResponse({"errors": [{"code": "need_id_agent"}]}, status=400)
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        ent = hub_app.current_state().get("entities", {}).get(eid)
        if not ent or ent.get("type") != "task":
            return JsonResponse({"errors": [{"code": "not_found"}]}, status=404)
        if kind == "handed_back" and str(ent.get("work_kind") or "").lower() == "decision":
            return JsonResponse({"errors": [{"code": "decision_not_unattended",
                "msg": "a decision is a person's call and is never handed to an unattended worker"}]},
                status=409)
        if ent.get("status") in ("done", "dropped"):
            return JsonResponse({"errors": [{"code": "closed", "status": ent.get("status")}]},
                                status=409)
        lease, refusal = _releasable_lease(request, b, eid)
        if refusal is not None:
            return refusal
        if lease is None and kind == "lease_released" and ent.get("status") != "in_progress":
            # Nothing is held and nothing is in flight: there is nothing to let go of, so write
            # nothing — a scheduler row here would record an event that did not happen.
            return JsonResponse({"data": {"id": eid, "task": eid, "version": ent.get("version"),
                                          "released": False, "noop": True,
                                          "status": ent.get("status"),
                                          "unattended": ent.get("unattended")}})
        at = _utc_now()
        note = str(b.get("note") or "").strip() or (
            "handed back by %s" % agent if kind == "handed_back" else "released by %s" % agent)
        update = {"type": "task", "plan": _lifecycle_row(ent.get("plan"), kind, note, at)}
        if ent.get("status") == "in_progress":
            update["status"] = "todo"
        if kind == "handed_back":
            update["unattended"] = True
        resp, status = _append("task", eid, update, expected_version=ent.get("version"),
                               agent=agent, idem=b.get("idem_key"), etype="task.updated")
        if status != 200:
            return JsonResponse(resp, status=status)
        released = hub_app.release_lease(eid, lease["token"]) if lease else False
    return JsonResponse({"data": dict(resp["data"], task=eid, released=bool(released),
                                      status=update.get("status", ent.get("status")),
                                      unattended=update.get("unattended", ent.get("unattended")))})


@writer(scope="task:release")
def hand(request, b):
    """Put a task back on the queue FOR AN UNATTENDED WORKER: status todo, ``unattended: true``,
    the lease released (the caller's own, or one an orphaned console of the same agent holds),
    and one counted ``handed_back`` scheduler row on the plan — never a work checkpoint."""
    return _let_go(request, b, kind="handed_back")


@writer(scope="task:release")
def unclaim(request, b):
    """Let go of a task without handing it anywhere: the lease released (own, or an orphaned
    console's of the same agent), an in-progress task back to todo, ``unattended`` unchanged."""
    return _let_go(request, b, kind="lease_released")


@writer(scope="presence:write")
def overlap_seen(request, b):
    """Record that these crossover signals reached their console (sidecar only, never the
    ledger), so each is announced once per side until it changes."""
    from hub_core import overlap
    ids_ = b.get("ids") if isinstance(b.get("ids"), list) else []
    return JsonResponse({"data": {"marked": overlap.mark_seen(hub_app.HUB_DIR, ids_)}})
