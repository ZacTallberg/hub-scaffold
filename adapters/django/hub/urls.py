"""The agent-operable Hub at /hub — unauthenticated reads and explicitly gated mutations.

Rendered entirely by hub_core (shell.render); no Django templates. The host must add read
authentication when entity data is not public. NEVER mount at the front door.
"""
from django.urls import path

from . import hub_api, hub_write, hubsite, mcp_server

app_name = "hub"
urlpatterns = [
    path("", hubsite.hub, name="hub"),
    path("hub.json", hub_api.hub_json),
    path("audit.json", hub_api.audit_json),
    path("graph.json", hub_api.graph_json),
    path("next.json", hub_api.next_json),
    # The live rail. These MUST stay above the `<str:type>.json` catch-all below, which would
    # otherwise match "cursor"/"delta" as entity types and 404 them as unknown collections.
    path("live/events", hub_api.live_events, name="live-events"),
    path("cursor.json", hub_api.cursor_json, name="cursor"),
    path("delta.json", hub_api.delta_json, name="delta"),
    path("dag.graphml", hub_api.dag_graphml, name="dag-graphml"),
    path("schema/<str:type>.schema.json", hub_api.schema_json),
    path("<str:type>.json", hub_api.type_json),
    path("<str:type>/<str:local>.json", hub_api.entity_json),
    path("api/task", hub_write.task),
    path("api/complete", hub_write.complete),
    path("api/adr", hub_write.adr),
    path("api/capability", hub_write.capability),
    path("api/decision", hub_write.decision),
    # The remaining entity types shipped as schemas with no writer — an agent could read and
    # validate them and had no way to create one through the API.
    path("api/gap", hub_write.gap),
    path("api/feat", hub_write.feat),
    path("api/note", hub_write.note),
    path("api/deploy", hub_write.deploy),
    path("api/claim", hub_write.claim),
    path("api/take", hub_write.take),
    path("api/release", hub_write.release),
    path("api/launch-grant", hub_write.launch_grant, name="launch-grant"),
    path("api/launch-grant/consume", hub_write.consume_launch_grant, name="consume-launch-grant"),
    path("api/heartbeat", hub_write.heartbeat),
    # MCP (Model Context Protocol) over the board: one token-gated JSON-RPC endpoint so any MCP
    # client can discover the board, pull ready work, spec a stub, and finish with a receipt. It
    # never touches the ledger directly — every mutation goes through the /hub/api/* write seam
    # above, so the receipt gate, lease fencing, OCC and schema validation all apply unchanged.
    path("api/mcp", mcp_server.mcp_endpoint, name="mcp"),
]
