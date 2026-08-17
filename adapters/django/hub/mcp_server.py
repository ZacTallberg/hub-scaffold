"""MCP 2026-07-28 server over the canonical Hub event plane.

The stateless Streamable HTTP adapter exposes board flow plus durable AgentRun operations. Every
mutation enters the ordinary token-gated HTTP write seams, preserving scoped authority, task-lease
fencing, OCC, schema validation, hash-chain durability, and immediate realtime publication.

``io.modelcontextprotocol/tasks`` handles map only to durable run aggregates created by a
task-augmented ``tools/call``. Backlog tasks are work contracts, not fake asynchronous handles.
The optional MCP polling hint is deliberately omitted: MCP point reads remain interoperable while
the Hub UI and worker coordination stay literally event-push realtime over the canonical SSE rail.
"""
import json

from django.http import JsonResponse

from hub_core import identity, runs, schedule

from . import hub_app
from .hub_write import writer

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
HUB_LEASE_META = "io.zacoberg.hub/leaseToken"


def _server_info():
    ident = identity.load()
    return {
        "name": f"{ident['key']}-hub-board",
        "title": f"{ident['key']} hub board and durable agent runs",
        "version": "1.1.0",
    }


def _run_fields(*required):
    properties = {
        "id": {"type": "string", "description": "durable AgentRun id"},
        "lease_token": {"type": "string", "description": "current fenced board-task lease"},
        "expected_version": {"type": "integer", "minimum": 1},
        "idem_key": {"type": "string"},
    }
    return properties, ["id", "lease_token", *required]


_MESSAGE_FIELDS, _MESSAGE_REQUIRED = _run_fields("content")
_MESSAGE_FIELDS.update({
    "content": {}, "role": {"enum": ["system", "operator", "worker", "tool"]},
    "kind": {"enum": ["progress", "context", "instruction", "output", "error"]},
    "status_message": {"type": "string"},
})
_COMMAND_FIELDS, _COMMAND_REQUIRED = _run_fields()
_COMMAND_FIELDS.update({
    "command_id": {"type": "string"}, "name": {"type": "string"},
    "arguments": {"type": "object"},
    "command_status": {"enum": ["queued", "running", "completed", "failed", "cancelled"]},
    "result": {"type": "object"}, "error": {"type": "object"},
    "evidence_uri": {"type": "array", "items": {"type": "string"}},
})
_CHECKPOINT_FIELDS, _CHECKPOINT_REQUIRED = _run_fields("summary")
_CHECKPOINT_FIELDS.update({
    "summary": {"type": "string"}, "state": {},
    "completed_steps": {"type": "array", "items": {"type": "string"}},
    "evidence_uri": {"type": "array", "items": {"type": "string"}},
})


TOOLS = [
    {"name": "board_next",
     "description": "Pull the readiness rail: top ready tasks plus work that needs specification.",
     "inputSchema": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "how many rows (default 3)"}}}},
    {"name": "spec_task",
     "description": "Give needs-spec work concrete acceptance; probes are reserved for rare critical boundaries.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "acceptance": {"type": "string"}, "verification_command": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "start_task",
     "description": "Claim a task and receive the fenced lease token required by run and completion operations.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "take_task",
     "description": "Atomically select and claim the highest-ranked ready task compatible with this worker.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"},
         "ttl_s": {"type": "integer", "minimum": 1, "maximum": 86400},
         "worker": schedule.WORKER_PROFILE_SCHEMA}, "required": ["agent"]}},
    {"name": "heartbeat_task",
     "description": "Renew a live task lease; this proves liveness, not progress.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "ttl_s": {"type": "integer", "minimum": 1, "maximum": 86400}},
         "required": ["id", "lease_token"]}},
    {"name": "release_task",
     "description": "Release exactly the caller's fenced lease so unfinished work can return to the queue.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}},
         "required": ["id", "agent", "lease_token"]}},
    {"name": "fail_task",
     "description": "Atomically record a real failure, return the lease, and create or reuse routed repair work.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "signature": {"type": "string"},
         "note": {"type": "string"}, "kind": {"type": "string"},
         "consequential": {"type": "boolean"},
         "evidence": {"type": "array", "items": {"type": "string"}}},
         "required": ["id", "agent", "lease_token", "signature", "note"]}},
    {"name": "finish_task",
     "description": "Complete the board work contract after its real operation succeeds.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "note": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"}},
         "verification_run": {"type": "object"}},
         "required": ["id", "agent", "lease_token", "note", "evidence"]}},
    {"name": "create_run",
     "description": "Durably create a resumable AgentRun for work already held by this task lease.",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string"}, "lease_token": {"type": "string"},
         "title": {"type": "string"}, "goal": {"type": "string"},
         "parent_run": {"type": "string"}, "trace_id": {"type": "string"},
         "ttl_ms": {"type": ["integer", "null"], "minimum": 1},
         "idem_key": {"type": "string"}}, "required": ["task", "lease_token"]}},
    {"name": "report_run_message",
     "description": "Append structured progress/context/output to the durable run and push it live immediately.",
     "inputSchema": {"type": "object", "properties": _MESSAGE_FIELDS,
                     "required": _MESSAGE_REQUIRED}},
    {"name": "record_run_command",
     "description": "Create or update a typed command record with status, result, error, and evidence.",
     "inputSchema": {"type": "object", "properties": _COMMAND_FIELDS,
                     "required": _COMMAND_REQUIRED}},
    {"name": "checkpoint_run",
     "description": "Persist the exact recovery state and completed step ids before an interrupt or handoff.",
     "inputSchema": {"type": "object", "properties": _CHECKPOINT_FIELDS,
                     "required": _CHECKPOINT_REQUIRED}},
    {"name": "request_run_input",
     "description": "Expose one uniquely keyed server-to-client request through MCP tasks/get.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "key": {"type": "string"}, "method": {"type": "string"},
         "params": {"type": "object"}, "status_message": {"type": "string"},
         "expected_version": {"type": "integer"}, "idem_key": {"type": "string"}},
         "required": ["id", "lease_token", "key", "method"]}},
    {"name": "handoff_run",
     "description": "Offer the run plus its latest checkpoint and receipt chain to a target worker subject.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "to": {"type": "string"}, "summary": {"type": "string"},
         "checkpoint_id": {"type": "string"}, "expected_version": {"type": "integer"},
         "idem_key": {"type": "string"}},
         "required": ["id", "lease_token", "to", "summary"]}},
    {"name": "resume_run",
     "description": "Resume under the current task lease and return a no-replay recovery envelope.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "summary": {"type": "string"}, "status_message": {"type": "string"},
         "expected_version": {"type": "integer"}, "idem_key": {"type": "string"}},
         "required": ["id", "lease_token"]}},
    {"name": "request_run_cancel",
     "description": "Record cooperative cancellation intent; the worker acknowledges at a safe checkpoint.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "reason": {"type": "string"}, "expected_version": {"type": "integer"},
         "idem_key": {"type": "string"}}, "required": ["id", "lease_token"]}},
    {"name": "acknowledge_run_cancel",
     "description": "Checkpointed worker acknowledgement that makes cooperative cancellation terminal.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "status_message": {"type": "string"}, "expected_version": {"type": "integer"},
         "idem_key": {"type": "string"}}, "required": ["id", "lease_token"]}},
    {"name": "finish_run",
     "description": "Complete a run with the original tools/call result shape and inherited child receipts.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "result": {"type": "object"},
         "evidence_uri": {"type": "array", "items": {"type": "string"}},
         "status_message": {"type": "string"}, "expected_version": {"type": "integer"},
         "idem_key": {"type": "string"}}, "required": ["id", "lease_token", "result"]}},
    {"name": "fail_run",
     "description": "Terminate a run with a JSON-RPC error while preserving its recovery history.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "error": {"type": "object"}, "status_message": {"type": "string"},
         "expected_version": {"type": "integer"}, "idem_key": {"type": "string"}},
         "required": ["id", "lease_token", "error"]}},
]


def _seam(path, payload, auth_headers, method="post"):
    """Enter the same HTTP seam as every external worker; never write the ledger directly."""
    from django.test import Client
    client = Client()
    forwarded = {}
    if auth_headers.get("agent"):
        forwarded["HTTP_X_AGENT_TOKEN"] = auth_headers["agent"]
    elif auth_headers.get("root"):
        forwarded["HTTP_X_WRITE_TOKEN"] = auth_headers["root"]
    if method == "get":
        response = client.get(path, payload, **forwarded)
    else:
        response = client.post(path, data=json.dumps(payload), content_type="application/json",
                               **forwarded)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.content.decode("utf-8", "replace")[:500]}


def _tool_result(status, body):
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": json.dumps({"status": status, "body": body})}],
        "isError": status >= 400,
    }


def _entity_version(entity_id):
    entity = hub_app.current_state().get("entities", {}).get(entity_id)
    return entity, (entity or {}).get("version")


def _run_update(args, action, auth_headers):
    payload = dict(args)
    payload["action"] = action
    return _seam("/hub/api/run/update", payload, auth_headers)


def _call_tool(name, args, auth_headers):
    """Return ``(standard_tool_result, newly_created_run_or_none)``."""
    if name == "board_next":
        status, body = _seam("/hub/next.json", {"n": int(args.get("n", 3))}, auth_headers,
                             method="get")
    elif name == "spec_task":
        entity, version = _entity_version(args["id"])
        if entity is None:
            status, body = 404, {"errors": [{"code": "not_found", "msg": args["id"]}]}
        else:
            payload = {"id": args["id"], "agent": args["agent"],
                       "expected_version": version}
            for key in ("acceptance", "verification_command"):
                if args.get(key):
                    payload[key] = args[key]
            status, body = _seam("/hub/api/task", payload, auth_headers)
    elif name == "start_task":
        status, body = _seam("/hub/api/claim", {"id": args["id"], "agent": args["agent"]},
                             auth_headers)
    elif name == "take_task":
        payload = {"agent": args["agent"]}
        for key in ("ttl_s", "worker"):
            if args.get(key) is not None:
                payload[key] = args[key]
        status, body = _seam("/hub/api/take", payload, auth_headers)
    elif name == "heartbeat_task":
        payload = {"id": args["id"], "token": args["lease_token"]}
        if args.get("ttl_s") is not None:
            payload["ttl_s"] = args["ttl_s"]
        status, body = _seam("/hub/api/heartbeat", payload, auth_headers)
    elif name == "release_task":
        status, body = _seam("/hub/api/release", {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
        }, auth_headers)
    elif name == "fail_task":
        payload = {"id": args["id"], "agent": args["agent"],
                   "token": args["lease_token"], "signature": args["signature"],
                   "note": args["note"]}
        for key in ("kind", "consequential"):
            if args.get(key) is not None:
                payload[key] = args[key]
        if args.get("evidence") is not None:
            payload["evidence_uri"] = args["evidence"]
        status, body = _seam("/hub/api/fail", payload, auth_headers)
    elif name == "finish_task":
        payload = {"id": args["id"], "agent": args["agent"],
                   "token": args["lease_token"], "accept_note": args["note"],
                   "evidence_uri": args["evidence"]}
        if args.get("verification_run"):
            payload["verification_run"] = args["verification_run"]
        status, body = _seam("/hub/api/complete", payload, auth_headers)
    elif name == "create_run":
        status, body = _seam("/hub/api/run", args, auth_headers)
        created = ((body.get("data") or {}).get("run") if status < 400 else None)
        return _tool_result(status, body), created
    else:
        actions = {
            "report_run_message": "message",
            "record_run_command": "command",
            "checkpoint_run": "checkpoint",
            "request_run_input": "input_request",
            "handoff_run": "handoff",
            "resume_run": "resume",
            "request_run_cancel": "request_cancel",
            "acknowledge_run_cancel": "ack_cancel",
            "finish_run": "complete",
            "fail_run": "fail",
        }
        if name not in actions:
            return None, None
        status, body = _run_update(args, actions[name], auth_headers)
    return _tool_result(status, body), None


def _client_tasks(params):
    meta = params.get("_meta") or {}
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
    return TASKS_EXTENSION in (capabilities.get("extensions") or {})


def _missing_tasks_capability():
    return {
        "code": -32003,
        "message": "Missing required client capability",
        "data": {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
    }


def _task_target(request, params):
    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        return None, {"code": -32602, "message": "taskId is required"}
    if request.headers.get("Mcp-Name") != task_id:
        return None, {"code": -32602,
                      "message": "Mcp-Name header must equal params.taskId"}
    entity = hub_app.current_state().get("entities", {}).get(task_id)
    if not entity or entity.get("type") != "run":
        return None, {"code": -32602, "message": "unknown durable task handle"}
    return entity, None


def _lease_from_meta(params):
    meta = params.get("_meta") or {}
    return meta.get(HUB_LEASE_META) or meta.get("hub/leaseToken")


def _rpc(rid, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return JsonResponse(body)


@writer(scope="mcp:call")
def mcp_endpoint(request, b):
    if not isinstance(b, dict):
        return _rpc(None, error={"code": -32600, "message": "not a JSON-RPC 2.0 request"})
    rid = b.get("id")
    method, params = b.get("method"), b.get("params") or {}
    if b.get("jsonrpc") != "2.0" or not method:
        return _rpc(rid, error={"code": -32600, "message": "not a JSON-RPC 2.0 request"})
    if not isinstance(params, dict):
        return _rpc(rid, error={"code": -32602, "message": "params must be an object"})
    auth_headers = {"agent": request.headers.get("X-Agent-Token", ""),
                    "root": request.headers.get("X-Write-Token", "")}

    if method == "initialize":
        return _rpc(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False},
                             "extensions": {TASKS_EXTENSION: {}}},
            "serverInfo": _server_info(),
        })
    if method == "server/discover":
        return _rpc(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": _server_info(),
            "capabilities": {"tools": {"listChanged": False},
                             "extensions": {TASKS_EXTENSION: {}}},
            "transport": "streamable-http-stateless",
        })
    if method == "tools/list":
        return _rpc(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name") or ""
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None:
            return _rpc(rid, error={"code": -32602, "message": f"unknown tool {name!r}"})
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc(rid, error={"code": -32602,
                                    "message": "tool arguments must be an object"})
        required = spec["inputSchema"].get("required") or []
        missing = [field for field in required if field not in arguments]
        if missing:
            return _rpc(rid, error={"code": -32602,
                                    "message": "missing required argument(s): " + ", ".join(missing)})
        try:
            standard, created_run = _call_tool(name, arguments, auth_headers)
        except (KeyError, TypeError, ValueError) as exc:
            return _rpc(rid, error={"code": -32602,
                                    "message": f"invalid tool arguments: {exc}"})
        # Task creation is server-directed and scoped to this individual declaring request. The
        # run is already committed and resolvable before this handle is returned.
        if created_run is not None and _client_tasks(params):
            return _rpc(rid, runs.mcp_task(created_run, result_type="task"))
        return _rpc(rid, standard)

    if method in ("tasks/get", "tasks/update", "tasks/cancel"):
        if not _client_tasks(params):
            return _rpc(rid, error=_missing_tasks_capability())
        run, problem = _task_target(request, params)
        if problem:
            return _rpc(rid, error=problem)
        if method == "tasks/get":
            return _rpc(rid, runs.mcp_task(run, result_type="complete"))

        lease_token = _lease_from_meta(params)
        if not lease_token:
            return _rpc(rid, error={"code": -32602,
                "message": f"params._meta[{HUB_LEASE_META!r}] is required for task mutation"})
        payload = {"id": run["id"], "lease_token": lease_token}
        if method == "tasks/update":
            responses = params.get("inputResponses")
            if not isinstance(responses, dict):
                return _rpc(rid, error={"code": -32602,
                                        "message": "inputResponses must be an object"})
            payload.update({"action": "input_response", "input_responses": responses})
        else:
            payload.update({"action": "request_cancel",
                            "reason": "Cancellation requested through MCP tasks/cancel."})
        status, body = _seam("/hub/api/run/update", payload, auth_headers)
        if status >= 400:
            return _rpc(rid, error={"code": -32000, "message": "durable task update refused",
                                    "data": {"status": status, "body": body}})
        return _rpc(rid, {"resultType": "complete"})

    return _rpc(rid, error={"code": -32601, "message": f"method not found: {method}"})
