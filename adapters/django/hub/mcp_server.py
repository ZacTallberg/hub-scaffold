"""MCP server over the board (ADR-0009 interop edge): JSON-RPC 2.0, stateless Streamable HTTP.

One token-gated POST endpoint (/hub/api/mcp) speaking the Model Context Protocol 2026-07-28
revision with the io.modelcontextprotocol/tasks extension, so any MCP client can discover the
board, pull ready work, spec a stub, and finish with a receipt. The adapter NEVER touches the
ledger directly: every mutation goes through the same /hub/api/* write seam every worker uses
(the caller's X-Write-Token is forwarded verbatim), so the receipt gate, lease fencing, OCC,
schema validation, and the WIP ceiling all apply unchanged — an MCP finish without a passing
exit-0 verification_run is rejected by complete() exactly like any other. Reads are the same
ledger fold the rail serves. The server never executes a verification_command (RCE ruling):
finish_task takes the client's typed receipt, it does not produce one.

tasks/get maps the ledger-folded task onto the tasks-extension states: done->completed,
dropped->cancelled, needs-spec (the readiness rail's own _spec_problem) -> input_required —
the board asking the client for a concrete acceptance/verification_command — else working.
"""
import json

from django.http import JsonResponse

from hub_core import identity

from . import hub_app
from .hub_write import writer

_IDENT = identity.load()
PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
SERVER_INFO = {"name": f"{_IDENT['key']}-hub-board", "title": f"{_IDENT['key']} hub board (hash-chained task ledger)",
               "version": "1.0.0"}

TOOLS = [
    {"name": "board_next",
     "description": "Pull the readiness rail: top ready tasks + needs-spec work (429 board_saturated surfaces verbatim).",
     "inputSchema": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "how many rows (default 3)"}}}},
    {"name": "spec_task",
     "description": "Give a needs-spec task its concrete acceptance and/or verification_command (a command that exits 0 iff the work is truly done).",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "credential": {"type": "string", "description": "the agent's per-worker identity credential (required once the agent is enrolled)"},
         "acceptance": {"type": "string"}, "verification_command": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "start_task",
     "description": "Claim the lease and set the task active; returns the lease token finish_task needs.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "credential": {"type": "string", "description": "the agent's per-worker identity credential (required once the agent is enrolled)"}},
         "required": ["id", "agent"]}},
    {"name": "finish_task",
     "description": "Submit completion: the receipt gate requires a typed exit-0 verification_run the CLIENT ran out-of-band (the server never executes commands).",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "credential": {"type": "string", "description": "the agent's per-worker identity credential (required once the agent is enrolled)"},
         "lease_token": {"type": "string"}, "note": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"}},
         "verification_run": {"type": "object"}},
         "required": ["id", "agent", "lease_token", "note", "evidence", "verification_run"]}},
]


def _seam(path, payload, token, method="post"):
    """The ONE door to the board: the same HTTP write seam every worker uses, caller's token
    forwarded verbatim. Returns (status, body)."""
    from django.test import Client
    c = Client()
    if method == "get":
        r = c.get(path, payload, HTTP_X_WRITE_TOKEN=token)
    else:
        r = c.post(path, data=json.dumps(payload), content_type="application/json",
                   HTTP_X_WRITE_TOKEN=token)
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


def _call_tool(name, args, token):
    if name == "board_next":
        return _tool_result(*_seam("/hub/next.json", {"n": int(args.get("n", 3))}, token, method="get"))
    # Identity rides the SAME body field the write seams check (`credential`) — the adapter
    # forwards it verbatim, so enrolled-agent enforcement applies to MCP callers unchanged.
    cred = {"credential": args["credential"]} if args.get("credential") else {}
    if name == "spec_task":
        ent, ver = _entity_version(args["id"])
        if ent is None:
            return _tool_result(404, {"errors": [{"code": "not_found", "msg": args["id"]}]})
        payload = {"id": args["id"], "agent": args["agent"], "expected_version": ver, **cred}
        for k in ("acceptance", "verification_command"):
            if args.get(k):
                payload[k] = args[k]
        return _tool_result(*_seam("/hub/api/task", payload, token))
    if name == "start_task":
        st, lease = _seam("/hub/api/claim", {"id": args["id"], "agent": args["agent"], **cred}, token)
        if st != 200:
            return _tool_result(st, lease)
        ent, ver = _entity_version(args["id"])
        st2, body = _seam("/hub/api/task", {"id": args["id"], "agent": args["agent"],
                                            "status": "active", "planning_state": "active",
                                            "expected_version": ver, "token": lease["token"], **cred}, token)
        if st2 != 200:
            return _tool_result(st2, body)
        return _tool_result(200, {"activated": body.get("data", {}), "lease_token": lease["token"],
                                  "lease_expires": lease.get("expires")})
    if name == "finish_task":
        return _tool_result(*_seam("/hub/api/complete", {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
            "accept_note": args["note"], "evidence_uri": args["evidence"],
            "verification_run": args["verification_run"], **cred}, token))
    return None


def _tasks_get(params):
    task_id = params.get("taskId") or params.get("id")
    ent = hub_app.current_state().get("entities", {}).get(task_id)
    if not ent or ent.get("type") != "task":
        return None
    from .hub_api import _spec_problem
    board_status = ent.get("status")
    needs = _spec_problem(ent) if board_status in ("todo", "blocked") else None
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


@writer
def mcp_endpoint(request, b):
    rid = b.get("id")
    method, params = b.get("method"), b.get("params") or {}
    if b.get("jsonrpc") != "2.0" or not method:
        return _rpc(rid, error={"code": -32600, "message": "not a JSON-RPC 2.0 request"})
    token = request.headers.get("X-Write-Token", "")
    if method == "initialize":
        return _rpc(rid, {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False},
                                           "extensions": {TASKS_EXTENSION: {}}},
                          "serverInfo": SERVER_INFO})
    if method == "server/discover":
        return _rpc(rid, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO,
                          "extensions": [{"name": TASKS_EXTENSION, "version": "1"}],
                          "transport": "streamable-http-stateless"})
    if method == "tools/list":
        return _rpc(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = (params.get("name") or "")
        out = _call_tool(name, params.get("arguments") or {}, token)
        if out is None:
            return _rpc(rid, error={"code": -32602, "message": f"unknown tool {name!r}"})
        return _rpc(rid, out)
    if method == "tasks/get":
        out = _tasks_get(params)
        if out is None:
            return _rpc(rid, error={"code": -32602, "message": "unknown task"})
        return _rpc(rid, out)
    return _rpc(rid, error={"code": -32601, "message": f"method not found: {method}"})
