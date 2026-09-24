"""The agent-operable Hub at /hub — unauthenticated reads and explicitly gated mutations.

Rendered entirely by hub_core (shell.render); no Django templates. The host must add read
authentication when entity data is not public. NEVER mount at the front door.
"""
from django.urls import path

from . import app_services, histories_api, hub_api, hub_write, hubsite, mcp_server, run_api

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
    # The ask/answer surfaces, the operational stream, board search, and request identity.
    # Same rule: above the catch-all, or each would 404 as an unknown entity collection.
    path("questions.json", hub_api.questions_json, name="questions"),
    path("inbox.json", hub_api.inbox_json, name="inbox"),
    path("inbox/wait", hub_api.inbox_wait, name="inbox-wait"),
    path("errors.json", hub_api.errors_json, name="errors"),
    path("search.json", hub_api.search_json, name="search"),
    path("whoami.json", hub_api.whoami_json, name="whoami"),
    # Console chat histories: off unless HUB_HISTORIES_ENABLED; readable only with history:read
    # or the adopter's HUB_HISTORY_VIEWER predicate (404 to everyone else).
    path("history.json", histories_api.history_json, name="history"),
    path("dag.graphml", hub_api.dag_graphml, name="dag-graphml"),
    # Services to the apps around the hub (adapters/django/hub/app_services.py): hosted UI
    # components and their per-app properties, and one app's slice of the board. Above the
    # catch-alls, which would read "components/props/<slug>.json" as an entity.
    path("components/", app_services.component_index, name="components"),
    path("components/props/<str:slug>.json", app_services.component_props,
         name="component-props"),
    path("components/<str:name>/<str:filename>", app_services.component_file,
         name="component-file"),
    path("app-feed.json", app_services.app_feed_json, name="app-feed"),
    path("schema/<str:type>.schema.json", hub_api.schema_json),
    path("<str:type>.json", hub_api.type_json),
    path("<str:type>/<str:local>.json", hub_api.entity_json),
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
    # Retire (or re-open) any knowledge record — gap, note, directive, ADR, finding — through the
    # lifecycle rules in hub_core.record_state: a reason is required and appended, never lost.
    path("api/retire", hub_write.retire),
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
    path("api/ack-error", hub_write.ack_error),
    path("api/clear-errors", hub_write.clear_errors),
    path("api/client-error", hub_api.client_error, name="client-error"),
    path("api/launch-grant", hub_write.launch_grant, name="launch-grant"),
    path("api/launch-grant/consume", hub_write.consume_launch_grant, name="consume-launch-grant"),
    path("api/heartbeat", hub_write.heartbeat),
    # Per-app component settings (operator), a person's cross-app preferences (an app's server
    # on behalf of a person it signed in), and the brokered agent (the hub holds the one key).
    path("api/component-props", app_services.set_component_props),
    path("api/profile", app_services.profile),
    path("api/agent/ask", app_services.agent_ask),
    path("api/agent/history", app_services.agent_history),
    path("api/agent/conversation", app_services.agent_conversation),
    path("api/history", histories_api.upload, name="history-upload"),
    path("api/run", run_api.create_run),
    path("api/run/update", run_api.update_run),
    # MCP (Model Context Protocol) over the board: one token-gated JSON-RPC endpoint so any MCP
    # client can discover the board, pull ready work, spec a stub, and finish with a receipt. It
    # never touches the ledger directly — every mutation goes through the /hub/api/* write seam
    # above, so the receipt gate, lease fencing, OCC and schema validation all apply unchanged.
    path("api/mcp", mcp_server.mcp_endpoint, name="mcp"),
]
