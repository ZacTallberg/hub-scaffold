"""MCP server over the board: JSON-RPC 2.0, stateless Streamable HTTP.

One token-gated POST endpoint (/hub/api/mcp) speaking the Model Context Protocol 2026-07-28
revision with the io.modelcontextprotocol/tasks extension, so any MCP client can discover the
board, pull ready work, spec a stub, and finish with a receipt. The adapter NEVER touches the
ledger directly: every mutation goes through the same /hub/api/* write seam every worker uses
(the caller's X-Agent-Token or shared-root compatibility token is forwarded verbatim), so the receipt gate, lease fencing, OCC,
schema validation, and the WIP ceiling all apply unchanged. A verification receipt is required
only when the task explicitly carries a rare one-shot critical-boundary command. Reads are the same
ledger fold the rail serves. The server never executes a verification_command (RCE ruling):
finish_task takes the client's typed receipt, it does not produce one.

tasks/get maps the ledger-folded task onto the tasks-extension states: done->completed,
dropped->cancelled, executable work without concrete acceptance -> input_required, else working.
"""
import json

from django.http import JsonResponse

from hub_core import identity, schedule

from . import hub_app
from .hub_write import writer

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


def _server_info():
    ident = identity.load()
    return {"name": f"{ident['key']}-hub-board",
            "title": f"{ident['key']} hub board (hash-chained task ledger)",
            "version": "1.0.0"}

TOOLS = [
    {"name": "board_next",
     "description": "Pull the readiness rail: top ready tasks + needs-spec work (429 board_saturated surfaces verbatim).",
     "inputSchema": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "how many rows (default 3)"}}}},
    {"name": "spec_task",
     "description": "Give a needs-spec task concrete acceptance; add verification_command only for a rare transient critical-boundary probe.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "acceptance": {"type": "string"}, "verification_command": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "start_task",
     "description": "Claim the lease; the claim seam performs the sole todo-to-in_progress transition and returns the lease token finish_task needs.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "take_task",
     "description": "Atomically filter the ready frontier for this worker, then select and claim its highest-ranked compatible task; returns routing reasons, the task, and its lease token.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"},
         "ttl_s": {"type": "integer", "minimum": 1, "maximum": 86400},
         "worker": schedule.WORKER_PROFILE_SCHEMA},
         "required": ["agent"]}},
    {"name": "heartbeat_task",
     "description": "Renew a live task lease before heartbeat_after_s elapses; this proves liveness, not task progress.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "lease_token": {"type": "string"},
         "ttl_s": {"type": "integer", "minimum": 1, "maximum": 86400}},
         "required": ["id", "lease_token"]}},
    {"name": "release_task",
     "description": "Release exactly the caller's fenced lease so blocked or interrupted work can return to the ready queue without waiting for expiry.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}},
         "required": ["id", "agent", "lease_token"]}},
    {"name": "fail_task",
     "description": "Atomically record one real failed attempt, return its fenced lease, apply bounded backoff/circuit state, and create or reuse specialist repair work.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "signature": {"type": "string"},
         "note": {"type": "string"}, "kind": {"type": "string"},
         "consequential": {"type": "boolean"},
         "evidence": {"type": "array", "items": {"type": "string"}}},
         "required": ["id", "agent", "lease_token", "signature", "note"]}},
    {"name": "finish_task",
     "description": "Submit completion; include an exit-0 verification_run only when this task explicitly carries a transient critical-boundary command.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "note": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"}},
         "verification_run": {"type": "object"}},
         "required": ["id", "agent", "lease_token", "note", "evidence"]}},
]


def _seam(path, payload, auth_headers, method="post"):
    """The ONE door to the board: the same HTTP write seam every worker uses, caller's token
    forwarded verbatim. Returns (status, body)."""
    from django.test import Client
    c = Client()
    forwarded = {}
    if auth_headers.get("agent"):
        forwarded["HTTP_X_AGENT_TOKEN"] = auth_headers["agent"]
    elif auth_headers.get("root"):
        forwarded["HTTP_X_WRITE_TOKEN"] = auth_headers["root"]
    if method == "get":
        r = c.get(path, payload, **forwarded)
    else:
        r = c.post(path, data=json.dumps(payload), content_type="application/json",
                   **forwarded)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.content.decode("utf-8", "replace")[:500]}


def _tool_result(status, body):
    return {"content": [{"type": "text", "text": json.dumps({"status": status, "body": body})}],
            "isError": status >= 400}


def _entity_version(task_id):
    ent = hub_app.current_state().get("entities", {}).get(task_id)
    return ent, (ent or {}).get("version")


def _call_tool(name, args, auth_headers):
    if name == "board_next":
        return _tool_result(*_seam("/hub/next.json", {"n": int(args.get("n", 3))}, auth_headers, method="get"))
    if name == "spec_task":
        ent, ver = _entity_version(args["id"])
        if ent is None:
            return _tool_result(404, {"errors": [{"code": "not_found", "msg": args["id"]}]})
        payload = {"id": args["id"], "agent": args["agent"], "expected_version": ver}
        for k in ("acceptance", "verification_command"):
            if args.get(k):
                payload[k] = args[k]
        return _tool_result(*_seam("/hub/api/task", payload, auth_headers))
    if name == "start_task":
        # Claim owns BOTH lease acquisition and the one durable todo -> in_progress transition.
        # A second task upsert here used to post schema-invalid `active`/`planning_state` fields,
        # report a tool error, and leave the successfully granted lease hidden from the caller.
        return _tool_result(*_seam(
            "/hub/api/claim", {"id": args["id"], "agent": args["agent"]}, auth_headers))
    if name == "take_task":
        payload = {"agent": args["agent"]}
        if args.get("ttl_s") is not None:
            payload["ttl_s"] = args["ttl_s"]
        if args.get("worker") is not None:
            payload["worker"] = args["worker"]
        return _tool_result(*_seam("/hub/api/take", payload, auth_headers))
    if name == "heartbeat_task":
        payload = {"id": args["id"], "token": args["lease_token"]}
        if args.get("ttl_s") is not None:
            payload["ttl_s"] = args["ttl_s"]
        return _tool_result(*_seam("/hub/api/heartbeat", payload, auth_headers))
    if name == "release_task":
        return _tool_result(*_seam("/hub/api/release", {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
        }, auth_headers))
    if name == "fail_task":
        payload = {"id": args["id"], "agent": args["agent"],
                   "token": args["lease_token"], "signature": args["signature"],
                   "note": args["note"]}
        for key in ("kind", "consequential"):
            if args.get(key) is not None:
                payload[key] = args[key]
        if args.get("evidence") is not None:
            payload["evidence_uri"] = args["evidence"]
        return _tool_result(*_seam("/hub/api/fail", payload, auth_headers))
    if name == "finish_task":
        payload = {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
            "accept_note": args["note"], "evidence_uri": args["evidence"],
        }
        if args.get("verification_run"):
            payload["verification_run"] = args["verification_run"]
        return _tool_result(*_seam("/hub/api/complete", payload, auth_headers))
    return None


def _tasks_get(params):
    task_id = params.get("taskId") or params.get("id")
    ent = hub_app.current_state().get("entities", {}).get(task_id)
    if not ent or ent.get("type") != "task":
        return None
    board_status = ent.get("status")
    # Keep this aligned with the readiness rail: executable work needs a concrete outcome, not a
    # standing test. Optional critical-boundary probes never determine pullability.
    needs = ("missing acceptance"
             if board_status in ("todo", "blocked")
             and ent.get("work_kind") in ("product", "verification")
             and not str(ent.get("acceptance") or "").strip() else None)
    if board_status == "done":
        status = "completed"
    elif board_status == "dropped":
        status = "cancelled"
    elif needs:
        status = "input_required"
    else:
        status = "working"
    return {"task": {
        "taskId": task_id,
        "status": status,
        "resultType": "input_required" if status == "input_required" else "result",
        "boardStatus": board_status,
        "title": ent.get("title"),
        "needs": needs,
        "version": ent.get("version"),
    }}


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
        server_info = _server_info()
        return _rpc(rid, {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False},
                                           "extensions": {TASKS_EXTENSION: {}}},
                          "serverInfo": server_info})
    if method == "server/discover":
        return _rpc(rid, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": _server_info(),
                          "extensions": [{"name": TASKS_EXTENSION, "version": "1"}],
                          "transport": "streamable-http-stateless"})
    if method == "tools/list":
        return _rpc(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = (params.get("name") or "")
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None:
            return _rpc(rid, error={"code": -32602, "message": f"unknown tool {name!r}"})
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc(rid, error={"code": -32602, "message": "tool arguments must be an object"})
        required = spec["inputSchema"].get("required") or []
        missing = [field for field in required if field not in args]
        if missing:
            return _rpc(rid, error={"code": -32602,
                                    "message": "missing required argument(s): " + ", ".join(missing)})
        try:
            out = _call_tool(name, args, auth_headers)
        except (KeyError, TypeError, ValueError) as exc:
            return _rpc(rid, error={"code": -32602, "message": f"invalid tool arguments: {exc}"})
        return _rpc(rid, out)
    if method == "tasks/get":
        out = _tasks_get(params)
        if out is None:
            return _rpc(rid, error={"code": -32602, "message": "unknown task"})
        return _rpc(rid, out)
    return _rpc(rid, error={"code": -32601, "message": f"method not found: {method}"})
