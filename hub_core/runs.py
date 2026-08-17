"""Pure lifecycle rules for durable agent runs.

The board task is the work contract; a ``run`` is one resumable execution attempt.  Every
transition returns a schema-ready partial payload for the event writer.  No process-local state
is authoritative: recovery is reconstructed from the folded run aggregate plus completed child
receipts already present in the canonical event plane.
"""
from datetime import datetime, timezone


TERMINAL = {"completed", "cancelled", "failed"}
MCP_STATUS = {
    "working": "working",
    "input_required": "input_required",
    "handoff_pending": "working",
    "cancel_requested": "working",
    "cancelled": "cancelled",
    "completed": "completed",
    "failed": "failed",
}


class TransitionError(ValueError):
    """A lifecycle transition that cannot be applied to the current durable state."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _next_id(prefix, records):
    used = {str(record.get("id") or "") for record in records}
    n = len(records) + 1
    while f"{prefix}-{n:04d}" in used:
        n += 1
    return f"{prefix}-{n:04d}"


def create_payload(*, task, title, goal, owner, credential_id, trace_id, parent_run=None,
                   ttl_ms=None):
    payload = {
        "task": task,
        "title": title,
        "goal": goal or "",
        "status": "working",
        "status_message": "Run accepted and connected to the canonical event plane.",
        "owner": owner,
        "attempt": 1,
        "trace_id": trace_id,
        "commands": [],
        "messages": [],
        "checkpoints": [],
        "handoffs": [],
        "input_requests": {},
        "input_responses": {},
        "receipt_chain": [],
        "evidence_uri": [],
        "ttl_ms": ttl_ms,
    }
    if credential_id:
        payload["credential_id"] = credential_id
    if parent_run:
        payload["parent_run"] = parent_run
    return payload


def recovery_envelope(run):
    """The exact durable context a replacement worker needs to continue without replay."""
    checkpoint = (run.get("checkpoints") or [None])[-1]
    return {
        "run": run.get("id"),
        "task": run.get("task"),
        "attempt": run.get("attempt", 1),
        "status": run.get("status"),
        "latest_checkpoint": checkpoint,
        "completed_steps": list((checkpoint or {}).get("completed_steps") or []),
        "receipt_chain": list(run.get("receipt_chain") or []),
        "unfinished_commands": [command for command in (run.get("commands") or [])
                                if command.get("status") not in ("completed", "cancelled")],
        "recent_messages": list((run.get("messages") or [])[-20:]),
    }


def _child_receipts(run, state):
    """Flatten completed descendants once; a parent inherits proof rather than replaying it."""
    existing = list(run.get("receipt_chain") or [])
    receipts = {receipt.get("run"): receipt for receipt in existing if receipt.get("run")}
    entities = (state or {}).get("entities", {})
    children = sorted((entity for entity in entities.values()
                       if entity.get("type") == "run"
                       and entity.get("parent_run") == run.get("id")
                       and entity.get("status") == "completed"),
                      key=lambda entity: entity.get("id", ""))
    for child in children:
        for inherited in child.get("receipt_chain") or []:
            if inherited.get("run"):
                receipts.setdefault(inherited["run"], inherited)
        provenance = child.get("provenance") or {}
        receipts[child["id"]] = {
            "run": child["id"],
            "completed_at": provenance.get("updated_at") or utc_now(),
            "evidence_uri": list(child.get("evidence_uri") or []),
            "result": dict(child.get("result") or {}),
        }
    return [receipts[key] for key in sorted(receipts)]


def _mutable(run, action):
    if run.get("status") == "completed":
        raise TransitionError("a completed run is immutable")
    if run.get("status") in ("cancelled", "failed") and action != "resume":
        raise TransitionError(f"{run.get('status')} run must resume before {action}")


def transition(run, action, body, *, actor, credential_id, state=None):
    """Apply one typed lifecycle operation and return ``(payload, operation_result)``.

    Authorization and OCC live at the HTTP boundary; these rules remain pure and reusable by any
    future adapter.  Full embedded collections are replaced atomically in the aggregate version.
    """
    action = str(action or "").strip().lower()
    if not action:
        raise TransitionError("action is required")
    if action == "request_cancel" and run.get("status") in TERMINAL:
        return {}, {"already_terminal": run.get("status")}
    _mutable(run, action)
    at = utc_now()

    if action == "message":
        messages = list(run.get("messages") or [])
        content = body.get("content")
        if content is None:
            raise TransitionError("message content is required")
        record = {
            "id": body.get("message_id") or _next_id("message", messages),
            "role": body.get("role") or "worker",
            "kind": body.get("kind") or "progress",
            "content": content,
            "at": at,
            "agent": actor,
        }
        messages.append(record)
        return ({"messages": messages, "status_message": body.get("status_message")
                 or (content if isinstance(content, str) else "Structured progress received.")},
                {"message": record})

    if action == "command":
        commands = [dict(command) for command in (run.get("commands") or [])]
        command_id = body.get("command_id")
        position = next((i for i, command in enumerate(commands)
                         if command.get("id") == command_id), None) if command_id else None
        if position is None:
            if not str(body.get("name") or "").strip():
                raise TransitionError("name is required for a new command")
            record = {
                "id": command_id or _next_id("command", commands),
                "name": body["name"],
                "arguments": body.get("arguments") or {},
                "status": body.get("command_status") or "queued",
                "created_at": at,
                "updated_at": at,
            }
            commands.append(record)
            position = len(commands) - 1
        else:
            record = commands[position]
            if body.get("name") is not None:
                record["name"] = body["name"]
            if body.get("arguments") is not None:
                record["arguments"] = body["arguments"]
            if body.get("command_status") is not None:
                record["status"] = body["command_status"]
            record["updated_at"] = at
        for field in ("result", "error"):
            if body.get(field) is not None:
                record[field] = body[field]
        if body.get("evidence_uri") is not None:
            record["evidence_uri"] = list(body.get("evidence_uri") or [])
        commands[position] = record
        return ({"commands": commands,
                 "status_message": body.get("status_message") or f"Command {record['name']}: {record['status']}"},
                {"command": record})

    if action == "checkpoint":
        checkpoints = list(run.get("checkpoints") or [])
        summary = str(body.get("summary") or "").strip()
        if not summary:
            raise TransitionError("checkpoint summary is required")
        record = {
            "id": body.get("checkpoint_id") or _next_id("checkpoint", checkpoints),
            "sequence": len(checkpoints) + 1,
            "summary": summary,
            "state": body.get("state"),
            "completed_steps": list(dict.fromkeys(body.get("completed_steps") or [])),
            "evidence_uri": list(body.get("evidence_uri") or []),
            "at": at,
            "agent": actor,
        }
        checkpoints.append(record)
        projected = {**run, "checkpoints": checkpoints}
        return ({"checkpoints": checkpoints,
                 "status_message": body.get("status_message") or summary},
                {"checkpoint": record, "recovery": recovery_envelope(projected)})

    if action == "input_request":
        key = str(body.get("key") or "").strip()
        method = str(body.get("method") or "").strip()
        if not key or not method:
            raise TransitionError("input request key and method are required")
        requests = dict(run.get("input_requests") or {})
        responses = dict(run.get("input_responses") or {})
        if key in requests or key in responses:
            raise TransitionError("input request keys are unique for the run lifetime")
        requests[key] = {"method": method, "params": body.get("params") or {}}
        return ({"input_requests": requests, "status": "input_required",
                 "status_message": body.get("status_message") or "Waiting for client input."},
                {"input_request": {key: requests[key]}})

    if action == "input_response":
        supplied = body.get("input_responses") or {}
        if not isinstance(supplied, dict):
            raise TransitionError("input_responses must be an object")
        requests = dict(run.get("input_requests") or {})
        responses = dict(run.get("input_responses") or {})
        accepted = []
        for key, value in supplied.items():
            if key not in requests or key in responses:
                continue
            responses[key] = value if isinstance(value, dict) else {"value": value}
            requests.pop(key, None)
            accepted.append(key)
        status = "input_required" if requests else "working"
        return ({"input_requests": requests, "input_responses": responses, "status": status,
                 "status_message": ("Waiting for remaining client input." if requests
                                    else "Client input received; work resumed.")},
                {"accepted": accepted, "ignored": sorted(set(supplied) - set(accepted))})

    if action == "handoff":
        target = str(body.get("to") or "").strip()
        summary = str(body.get("summary") or "").strip()
        if not target or not summary:
            raise TransitionError("handoff target and summary are required")
        handoffs = list(run.get("handoffs") or [])
        checkpoint_id = body.get("checkpoint_id")
        checkpoints = run.get("checkpoints") or []
        if checkpoint_id and not any(cp.get("id") == checkpoint_id for cp in checkpoints):
            raise TransitionError("handoff checkpoint does not exist")
        if not checkpoint_id and checkpoints:
            checkpoint_id = checkpoints[-1].get("id")
        record = {
            "id": body.get("handoff_id") or _next_id("handoff", handoffs),
            "from": actor,
            "to": target,
            "summary": summary,
            "status": "offered",
            "at": at,
        }
        if checkpoint_id:
            record["checkpoint"] = checkpoint_id
        handoffs.append(record)
        return ({"handoffs": handoffs, "status": "handoff_pending",
                 "status_message": f"Handoff offered to {target}: {summary}"},
                {"handoff": record})

    if action == "resume":
        handoffs = [dict(handoff) for handoff in (run.get("handoffs") or [])]
        offered = next((handoff for handoff in reversed(handoffs)
                        if handoff.get("status") == "offered"), None)
        if offered and offered.get("to") != actor:
            raise TransitionError(f"handoff is reserved for {offered.get('to')}")
        if offered:
            offered["status"] = "accepted"
            offered["accepted_at"] = at
        elif run.get("owner") != actor:
            checkpoints = run.get("checkpoints") or []
            recovered = {
                "id": _next_id("handoff", handoffs),
                "from": run.get("owner") or "unknown",
                "to": actor,
                "summary": body.get("summary") or "Recovered after prior worker/process loss.",
                "status": "accepted",
                "at": at,
                "accepted_at": at,
            }
            if checkpoints:
                recovered["checkpoint"] = checkpoints[-1].get("id")
            handoffs.append(recovered)
        changed_owner = (run.get("owner") != actor
                         or run.get("credential_id") != credential_id
                         or run.get("status") != "working")
        payload = {
            "handoffs": handoffs,
            "owner": actor,
            "status": "working",
            "status_message": body.get("status_message") or "Run resumed from durable state.",
            "attempt": int(run.get("attempt") or 1) + (1 if changed_owner else 0),
        }
        if credential_id:
            payload["credential_id"] = credential_id
        projected = {**run, **payload}
        return payload, {"recovery": recovery_envelope(projected)}

    if action == "request_cancel":
        return ({"status": "cancel_requested", "cancel_requested_at": at,
                 "cancel_requested_by": actor, "cancel_reason": body.get("reason") or "",
                 "status_message": body.get("reason") or "Cancellation requested; awaiting worker acknowledgement."},
                {"cancellation": "requested"})

    if action == "ack_cancel":
        if run.get("status") != "cancel_requested":
            raise TransitionError("cancellation has not been requested")
        return ({"status": "cancelled",
                 "status_message": body.get("status_message") or "Cancellation acknowledged at a safe checkpoint."},
                {"cancellation": "acknowledged"})

    if action == "complete":
        result = body.get("result")
        if not isinstance(result, dict):
            raise TransitionError("completion result must be an object matching the original request result")
        receipts = _child_receipts(run, state)
        return ({"status": "completed", "result": result,
                 "evidence_uri": list(body.get("evidence_uri") or []),
                 "receipt_chain": receipts,
                 "status_message": body.get("status_message") or "Run completed."},
                {"result": result, "receipt_chain": receipts})

    if action == "fail":
        error = body.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), int) \
                or not str(error.get("message") or "").strip():
            raise TransitionError("failure requires a JSON-RPC error object with integer code and message")
        return ({"status": "failed", "error": error,
                 "status_message": body.get("status_message") or error["message"]},
                {"error": error})

    raise TransitionError(f"unknown run action {action!r}")


def mcp_task(run, *, result_type="complete"):
    """Render the exact current Tasks-extension result shape from one durable run."""
    provenance = run.get("provenance") or {}
    created = provenance.get("created_at") or provenance.get("updated_at") or utc_now()
    updated = provenance.get("updated_at") or created
    status = MCP_STATUS.get(run.get("status"), "working")
    task = {
        "resultType": result_type,
        "taskId": run["id"],
        "status": status,
        "createdAt": created,
        "lastUpdatedAt": updated,
        "ttlMs": run.get("ttl_ms"),
    }
    if run.get("status_message"):
        task["statusMessage"] = run["status_message"]
    if status == "input_required":
        task["inputRequests"] = dict(run.get("input_requests") or {})
    elif status == "completed":
        task["result"] = dict(run.get("result") or {})
    elif status == "failed":
        task["error"] = dict(run.get("error") or {})
    return task
