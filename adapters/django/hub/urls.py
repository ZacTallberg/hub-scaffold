"""The agent-operable Hub at /hub — authenticated reads and explicitly gated mutations.

Rendered entirely by hub_core (shell.render); no Django templates. Every read route is wrapped
by ``read_auth.reader``: reads require an authenticated principal unless the operator declares
the board public (``HUB_READ_AUTH = "public"``). NEVER mount at the front door.
"""
from django.urls import path

from . import hub_api, hub_write, hubsite, mcp_server, read_auth, run_api

R = read_auth.reader

app_name = "hub"
urlpatterns = [
    path("", R(hubsite.hub), name="hub"),
    path("hub.json", R(hub_api.hub_json)),
    path("audit.json", R(hub_api.audit_json)),
    path("graph.json", R(hub_api.graph_json)),
    path("next.json", R(hub_api.next_json)),
    # The live rail. These MUST stay above the `<str:type>.json` catch-all below, which would
    # otherwise match "cursor"/"delta" as entity types and 404 them as unknown collections.
    path("live/events", R(hub_api.live_events), name="live-events"),
    path("cursor.json", R(hub_api.cursor_json), name="cursor"),
    path("delta.json", R(hub_api.delta_json), name="delta"),
    # The ask/answer surfaces, the operational stream, board search, and request identity.
    # Same rule: above the catch-all, or each would 404 as an unknown entity collection.
    path("questions.json", R(hub_api.questions_json), name="questions"),
    path("inbox.json", R(hub_api.inbox_json), name="inbox"),
    path("inbox/wait", R(hub_api.inbox_wait), name="inbox-wait"),
    path("errors.json", R(hub_api.errors_json), name="errors"),
    path("search.json", R(hub_api.search_json), name="search"),
    path("whoami.json", R(hub_api.whoami_json), name="whoami"),
    path("components.json", R(hub_api.components_json), name="components"),
    path("dag.graphml", R(hub_api.dag_graphml), name="dag-graphml"),
    path("schema/<str:type>.schema.json", R(hub_api.schema_json)),
    path("<str:type>.json", R(hub_api.type_json)),
    path("<str:type>/<str:local>.json", R(hub_api.entity_json)),
    path("api/task", hub_write.task),
    path("api/complete", hub_write.complete),
    path("api/adr", hub_write.adr),
    path("api/capability", hub_write.capability),
    path("api/agent-credential", hub_write.agent_credential),
    path("api/decision", hub_write.decision),
    # The remaining entity types shipped as schemas with no writer — an agent could read and
    # validate them and had no way to create one through the API.
    path("api/gap", hub_write.gap),
    path("api/feat", hub_write.feat),
    path("api/note", hub_write.note),
    path("api/deploy", hub_write.deploy),
    path("api/claim", hub_write.claim),
    path("api/take", hub_write.take),
    path("api/fail", hub_write.fail),
    path("api/release", hub_write.release),
    # The ask/answer loop and the directive plane (delivery closed by acks).
    path("api/ask", hub_write.ask),
    path("api/answer", hub_write.answer),
    path("api/directive", hub_write.directive),
    path("api/ack", hub_write.ack),
    # Observed presence: the seat heartbeat (ordinary writes stamp activity on their own).
    path("api/presence", hub_write.presence_ping),
    path("api/forget-presence", hub_write.forget_presence),
    # The operational error stream's ingest and queue actions.
    path("api/app-error", hub_write.app_error),
    path("api/agent-error", hub_write.agent_error),
    path("api/ci-failure", hub_write.ci_failure),
    path("api/ack-error", hub_write.ack_error),
    path("api/clear-errors", hub_write.clear_errors),
    path("api/client-error", hub_api.client_error, name="client-error"),
    path("api/launch-grant", hub_write.launch_grant, name="launch-grant"),
    path("api/launch-grant/consume", hub_write.consume_launch_grant, name="consume-launch-grant"),
    path("api/heartbeat", hub_write.heartbeat),
    path("api/run", run_api.create_run),
    path("api/run/update", run_api.update_run),
    # MCP (Model Context Protocol) over the board: one token-gated JSON-RPC endpoint so any MCP
    # client can discover the board, pull ready work, spec a stub, and finish with a receipt. It
    # never touches the ledger directly — every mutation goes through the /hub/api/* write seam
    # above, so the receipt gate, lease fencing, OCC and schema validation all apply unchanged.
    path("api/mcp", mcp_server.mcp_endpoint, name="mcp"),
]
