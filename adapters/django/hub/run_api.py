"""Lease-fenced write seam for durable AgentRun lifecycle operations.

Each operation folds the latest run, applies one typed transition, commits it with OCC while the
task lease fence is held, and publishes the committed event through the Hub's realtime bus via
``_append``.  The endpoint never maintains a second run registry or process-local recovery cache.
"""
import uuid

from django.http import JsonResponse

from hub_core import ids, runs
from hub_core.process_lock import ProcessFileLock

from . import hub_app
from .hub_write import _append, writer


def _error(code, message, status, **extra):
    return JsonResponse({"errors": [{"code": code, "msg": message, **extra}]}, status=status)


def _lease_token(body):
    return body.get("lease_token") or body.get("token")


def _fenced(task_id, body, request):
    return hub_app.lease_authorized(task_id, _lease_token(body), request.hub_auth.subject,
                                    request.hub_auth.credential_id)


def _prior_create(task_id, idem_key, actor):
    """Find a create retry before allocating another aggregate id.

    Store-level idempotency is intentionally aggregate-scoped, while the aggregate id is allocated
    by this operation. Bind a create key to ``actor + task`` across run aggregates at this seam.
    """
    if not idem_key:
        return None
    store = hub_app.store()
    try:
        for event in reversed(store.events()):
            payload = event.get("payload") or {}
            if (event.get("type") == "run.created" and event.get("idem_key") == idem_key
                    and event.get("agent_id") == actor and payload.get("task") == task_id):
                return event
    finally:
        store.close()
    return None


def _has_idem(run_id, idem_key):
    if not idem_key:
        return False
    store = hub_app.store()
    try:
        return store.has_idem(run_id, idem_key)
    finally:
        store.close()


@writer(scope="run:write")
def create_run(request, b):
    task_id = b.get("task")
    if not isinstance(task_id, str) or not task_id.strip():
        return _error("missing_task", "task is required", 400)
    agent = request.hub_auth.subject

    # Serializing on the same short-lived fence lock used by completion ensures a lease cannot
    # expire/reclaim between authorization and the run append. It also makes numeric allocation
    # collision-free across concurrent creators without introducing a second coordination plane.
    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        if not _fenced(task_id, b, request):
            return _error("lease", "creating a run requires the current fenced task lease", 409)
        state = hub_app.current_state()
        task = state.get("entities", {}).get(task_id)
        if not task or task.get("type") != "task":
            return _error("not_found", f"unknown board task {task_id}", 404)
        prior = _prior_create(task_id, b.get("idem_key"), agent)
        if prior:
            eid = prior["aggregate"]
            entity = state["entities"].get(eid)
            if entity:
                return JsonResponse({"data": {"id": eid, "version": entity.get("version"),
                                                "event": prior.get("event_id"), "run": entity,
                                                "idempotent": True}})
        parent_run = b.get("parent_run")
        if parent_run:
            parent = state.get("entities", {}).get(parent_run)
            if not parent or parent.get("type") != "run":
                return _error("parent_run", f"unknown parent run {parent_run}", 422)
        eid = ids.next_id(state["entities"], hub_app.PROJECT_KEY, "run")
        try:
            ttl_ms = b.get("ttl_ms")
            if ttl_ms is not None:
                ttl_ms = int(ttl_ms)
                if ttl_ms < 1:
                    raise ValueError
        except (TypeError, ValueError):
            return _error("ttl_ms", "ttl_ms must be null or a positive integer", 422)
        payload = runs.create_payload(
            task=task_id,
            title=str(b.get("title") or task.get("title") or eid),
            goal=str(b.get("goal") or task.get("acceptance") or ""),
            owner=agent,
            credential_id=request.hub_auth.credential_id,
            trace_id=str(b.get("trace_id") or uuid.uuid4().hex),
            parent_run=parent_run,
            ttl_ms=ttl_ms,
        )
        if not _fenced(task_id, b, request):
            return _error("lease", "task lease expired before the run commit", 409)
        response, status = _append(
            "run", eid, payload, expected_version=0, agent=agent,
            idem=b.get("idem_key"), etype="run.created",
        )
    if status < 400:
        response["data"]["run"] = hub_app.current_state()["entities"][eid]
    return JsonResponse(response, status=status)


@writer(scope="run:write")
def update_run(request, b):
    run_id = b.get("id") or b.get("run")
    if not isinstance(run_id, str) or not run_id.strip():
        return _error("missing_id", "run id is required", 400)
    action = str(b.get("action") or "").strip().lower()
    if not action:
        return _error("missing_action", "run lifecycle action is required", 400)

    with ProcessFileLock(hub_app.CLAIMS, name=".claims.lock", timeout=30):
        state = hub_app.current_state()
        run = state.get("entities", {}).get(run_id)
        if not run or run.get("type") != "run":
            return _error("not_found", f"unknown run {run_id}", 404)
        if not _fenced(run.get("task"), b, request):
            return _error("lease", "run mutation requires the current fenced task lease", 409)

        actor = request.hub_auth.subject
        credential_id = request.hub_auth.credential_id
        # Resume is the explicit authority transfer/recovery operation. Cancellation intent and
        # client input are authorized by the current task fence itself. Every worker-authored
        # command/message/checkpoint/outcome remains bound to the run's current owner credential.
        owner_exempt = {"resume", "request_cancel", "input_response"}
        if action not in owner_exempt and (
                run.get("owner") != actor
                or (run.get("credential_id") and run.get("credential_id") != credential_id)):
            return _error("owner", "resume the run under this credential before mutating it", 409,
                          owner=run.get("owner"))
        expected = b.get("expected_version")
        if expected is not None and expected != run.get("version"):
            return _error("conflict", "run version changed", 409,
                          expected=expected, current=run.get("version"))
        if _has_idem(run_id, b.get("idem_key")):
            return JsonResponse({"data": {"id": run_id, "version": run.get("version"),
                                           "run": run, "idempotent": True,
                                           "recovery": runs.recovery_envelope(run)}})
        try:
            payload, operation = runs.transition(
                run, action, b, actor=actor, credential_id=credential_id, state=state,
            )
        except runs.TransitionError as exc:
            return _error("transition", str(exc), 409)
        # Empty payload is a truthful idempotent acknowledgement (for example cancelling an
        # already terminal run); do not append noise just to manufacture a version.
        if not payload:
            return JsonResponse({"data": {"id": run_id, "version": run.get("version"),
                                           "run": run, "operation": operation}})
        if not _fenced(run.get("task"), b, request):
            return _error("lease", "task lease expired before the run commit", 409)
        response, status = _append(
            "run", run_id, payload, expected_version=run.get("version"), agent=actor,
            idem=b.get("idem_key"), etype=f"run.{action}",
        )
    if status < 400:
        current = hub_app.current_state()["entities"][run_id]
        response["data"].update({"run": current, "operation": operation,
                                 "recovery": runs.recovery_envelope(current)})
    return JsonResponse(response, status=status)
