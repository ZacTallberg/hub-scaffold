"""The agent-operable Hub at /hub — unauthenticated reads and explicitly gated mutations.

Rendered entirely by hub_core (shell.render); no Django templates. The host must add read
authentication when entity data is not public. NEVER mount at the front door.
"""
from django.urls import path

from . import held, hub_api, hub_write, hubsite, veil, mcp_server, run_api

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
    path("agent-updates.json", hub_api.agent_updates_json, name="agent-updates"),
    path("receipts.json", hub_api.receipts_json, name="receipts"),
    path("activity.json", hub_api.activity_json, name="activity"),
    path("perf.json", hub_api.perf_json, name="perf"),
    path("veil-audit.json", hub_api.veil_audit_json, name="veil-audit"),
    path("tiers.json", hub_api.tiers_json, name="tiers"),
    # Operator attention (owner, fix, values, age), the live consoles with their crossovers,
    # and one project's annotated task feed (ETag/304) — same catch-all rule as above.
    path("attention.json", hub_api.attention_json, name="attention"),
    path("consoles.json", hub_api.consoles_json, name="consoles"),
    path("project/<str:slug>/tasks.json", hub_api.project_tasks_json, name="project-tasks"),
    # The promotion lane's queue (open holds, oldest first) and the per-machine item claims.
    # Both sit ABOVE the generic <type>.json catch-all.
    path("held.json", held.held_json, name="held"),
    path("item-claims.json", hub_api.item_claims_json, name="item-claims"),
    path("dag.graphml", hub_api.dag_graphml, name="dag-graphml"),
    path("schema/<str:type>.schema.json", hub_api.schema_json),
    path("<str:type>.json", hub_api.type_json),
    path("<str:type>/<str:local>.json", hub_api.entity_json),
    path("api/task", hub_write.task),
    path("api/complete", hub_write.complete),
    path("api/adr", hub_write.adr),
    path("api/capability", hub_write.capability),
    path("api/agent-credential", hub_write.agent_credential),
    path("api/tier", hub_write.tier),
    path("api/decision", hub_write.decision),
    # The remaining entity types shipped as schemas with no writer — an agent could read and
    # validate them and had no way to create one through the API.
    path("api/gap", hub_write.gap),
    path("api/feat", hub_write.feat),
    path("api/note", hub_write.note),
    path("api/deploy", hub_write.deploy),
    path("api/claim", hub_write.claim),
    path("api/take", hub_write.take),
    # Give a task to a named agent: `to` is the recipient, `agent` stays the writer.
    path("api/assign", hub_write.assign),
    # One responder per non-task item (a question, an error fingerprint) across machines.
    path("api/item-claim", hub_write.item_claim),
    # The promotion lane: hold a finished commit back from live; promote it with evidence.
    path("api/held", held.hold),
    path("api/held/promote", held.promote),
    path("api/held/abandon", held.abandon),
    path("api/fail", hub_write.fail),
    path("api/release", hub_write.release),
    # Letting go of a task: back to the queue for an unattended worker (hand) or just released
    # (unclaim) — including a lease an orphaned console of the same agent still holds.
    # `api/hand` dispatches on the body: with `to` it assigns, without it it hands back.
    path("api/hand", hub_write.hand),
    path("api/hand-back", hub_write.hand_back),
    path("api/unclaim", hub_write.unclaim),
    # A person decides a decision task (file the build task / close / reply); agents are refused.
    path("api/task/decide", hub_write.decide_task),
    path("api/overlap-seen", hub_write.overlap_seen),
    # The ask/answer loop and the directive plane (delivery closed by acks).
    path("api/ask", hub_write.ask),
    path("api/answer", hub_write.answer),
    path("api/directive", hub_write.directive),
    path("api/ack", hub_write.ack),
    # Agent-to-agent mail, delivered by the recipient's inbox like any other addressed item.
    path("api/message", hub_write.message),
    path("api/message/ack", hub_write.message_ack),
    # Observed presence: the seat heartbeat (ordinary writes stamp activity on their own).
    path("api/presence", hub_write.presence_ping),
    path("api/forget-presence", hub_write.forget_presence),
    # The operational error stream's ingest and queue actions.
    path("api/agent-update", hub_write.agent_update),
    path("api/app-error", hub_write.app_error),
    path("api/agent-error", hub_write.agent_error),
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

# EVERY ROUTE DECLARES ITS VISIBILITY (see veil.py). Writes are `open`: their credential is the
# boundary. A route missing from this table serves no narrowed reader and fails the hub audit's
# routes:undeclared-visibility check — a new route is private until somebody decides otherwise.
VISIBILITY = {
    "": "veiled", "hub.json": "veiled", "graph.json": "veiled", "next.json": "veiled",
    "delta.json": "veiled", "questions.json": "veiled", "inbox.json": "veiled",
    "inbox/wait": "veiled", "errors.json": "veiled", "search.json": "veiled",
    "agent-updates.json": "veiled", "activity.json": "veiled",
    "attention.json": "veiled", "consoles.json": "veiled",
    "project/<str:slug>/tasks.json": "veiled", "held.json": "veiled",
    "item-claims.json": "veiled",
    "<str:type>.json": "veiled", "<str:type>/<str:local>.json": "veiled",
    "cursor.json": "open", "whoami.json": "open", "schema/<str:type>.schema.json": "open",
    # A stream cannot be scrubbed record by record, and the rest are operator diagnostics.
    "live/events": "member", "audit.json": "member", "receipts.json": "member",
    "perf.json": "member", "veil-audit.json": "member", "tiers.json": "member",
    "dag.graphml": "member",
}
for _pattern in urlpatterns:
    _route = str(_pattern.pattern)
    if _route.startswith("api/"):
        veil.declare("open", _pattern.callback)
    elif _route in VISIBILITY:
        veil.declare(VISIBILITY[_route], _pattern.callback)
