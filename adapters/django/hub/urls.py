"""The agent-operable Hub at /hub — authenticated reads and explicitly gated mutations.

Rendered entirely by hub_core (shell.render); no Django templates. Every read route is wrapped
by ``read_auth.reader``: reads require an authenticated principal unless the operator declares
the board public (``HUB_READ_AUTH = "public"``). NEVER mount at the front door.
"""
from django.urls import path

from . import (app_services, evals_api, evidence_api, held, histories_api, hub_api, hub_write,
               hubsite, knowledge_api, knowledge_write, veil, mcp_server, read_auth, run_api)

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
    # The error stream FOLDED into owned problems, every service's observability verdict,
    # one service diagnosed, live-console crossovers, and a credential's enrollment state.
    path("problems.json", R(hub_api.problems_json), name="problems"),
    path("app_health.json", R(hub_api.app_health_json), name="app-health"),
    path("doctor.json", R(hub_api.doctor_json), name="doctor"),
    path("overlap.json", R(hub_api.overlap_json), name="overlap"),
    path("enroll/status.json", R(hub_api.enroll_status_json), name="enroll-status"),
    path("search.json", R(knowledge_api.search_json), name="search"),
    # Knowledge: related records, the per-prompt memory index, the mirror feed, and the
    # published capability catalog. Above the catch-all for the same reason as the rest.
    path("related.json", R(knowledge_api.related_json), name="related"),
    path("guidance.json", R(knowledge_api.guidance_json), name="guidance"),
    path("knowledge/since", R(knowledge_api.knowledge_since), name="knowledge-since"),
    path("capabilities.json", R(knowledge_api.capabilities_json), name="capabilities"),
    # The shared evidence store (hub_core.evidence): readings keyed by subject and commit, so a
    # console reads the probe a peer already ran instead of re-running it.
    path("evidence.json", R(evidence_api.evidence_json), name="evidence"),
    # The standing-eval trend (hub_core.evals): runs scored on the board's own data.
    path("eval.json", R(evals_api.eval_json), name="eval"),
    path("whoami.json", R(hub_api.whoami_json), name="whoami"),
    # Standing documents rendered through their facet fences for the presenting credential.
    path("doctrine.json", R(hub_api.doctrine_json), name="doctrine"),
    path("agent-updates.json", R(hub_api.agent_updates_json), name="agent-updates"),
    path("receipts.json", R(hub_api.receipts_json), name="receipts"),
    path("activity.json", R(hub_api.activity_json), name="activity"),
    path("perf.json", R(hub_api.perf_json), name="perf"),
    path("veil-audit.json", R(hub_api.veil_audit_json), name="veil-audit"),
    path("tiers.json", R(hub_api.tiers_json), name="tiers"),
    # Operator attention (owner, fix, values, age), the live consoles with their crossovers,
    # and one project's annotated task feed (ETag/304) — same catch-all rule as above.
    path("attention.json", R(hub_api.attention_json), name="attention"),
    path("consoles.json", R(hub_api.consoles_json), name="consoles"),
    path("project/<str:slug>/tasks.json", R(hub_api.project_tasks_json), name="project-tasks"),
    # The promotion lane's queue (open holds, oldest first) and the per-machine item claims.
    # Both sit ABOVE the generic <type>.json catch-all.
    path("held.json", R(held.held_json), name="held"),
    path("item-claims.json", R(hub_api.item_claims_json), name="item-claims"),
    path("components.json", R(hub_api.components_json), name="components"),
    # Seat convergence and derived per-person output (same catch-all rule as above).
    path("distribution.json", R(hub_api.distribution_json), name="distribution"),
    path("built.json", R(hub_api.built_json), name="built"),
    path("ci-events.json", R(hub_api.ci_events_json), name="ci-events"),
    # Console chat histories: off unless HUB_HISTORIES_ENABLED; readable only with history:read
    # or the adopter's HUB_HISTORY_VIEWER predicate (404 to everyone else).
    path("history.json", R(histories_api.history_json), name="history"),
    path("dag.graphml", R(hub_api.dag_graphml), name="dag-graphml"),
    # Services to the apps around the hub (adapters/django/hub/app_services.py): hosted UI
    # components and their per-app properties (OPEN presentation reads, declared public), and
    # one app's slice of the board. Above the catch-alls, which would read
    # "components/props/<slug>.json" as an entity.
    path("components/", app_services.component_index, name="component-index"),
    path("components/props/<str:slug>.json", app_services.component_props,
         name="component-props"),
    path("components/<str:name>/<str:filename>", app_services.component_file,
         name="component-file"),
    path("app-feed.json", R(app_services.app_feed_json), name="app-feed"),
    path("schema/<str:type>.schema.json", R(hub_api.schema_json)),
    path("<str:type>.json", R(hub_api.type_json)),
    path("<str:type>/<str:local>.json", R(hub_api.entity_json)),
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
    # Knowledge records: a lesson is admitted and TAGGED with what it may overlap (never
    # refused for resemblance); finding / method / review say which kind of record they are.
    path("api/lesson", knowledge_write.lesson),
    path("api/finding", knowledge_write.finding),
    path("api/method", knowledge_write.method),
    path("api/review", knowledge_write.review),
    path("api/eval", evals_api.post),
    # Retire (or re-open) any knowledge record — gap, note, directive, ADR, finding — through the
    # lifecycle rules in hub_core.record_state: a reason is required and appended, never lost.
    path("api/retire", hub_write.retire),
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
    path("api/hand-to-queue", hub_write.hand_to_queue),
    # A run that ended with its task unfinished: back to todo with ONE self-counting row, the
    # fenced lease proven (an unattended launcher's hand-back; hub_core/unattended/board.py).
    path("api/hand-back", hub_write.hand_back),
    path("api/unclaim", hub_write.unclaim),
    # A person decides a decision task (file the build task / close / reply); agents are refused.
    path("api/task/decide", hub_write.decide_task),
    path("api/overlap-seen", hub_write.overlap_seen),
    # The ask/answer loop and the directive plane (delivery closed by acks).
    path("api/ask", hub_write.ask),
    path("api/answer", hub_write.answer),
    path("api/question/withdraw", hub_write.withdraw_question),
    path("api/evidence", evidence_api.post),
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
    path("api/ci-failure", hub_write.ci_failure),
    path("api/ack-error", hub_write.ack_error),
    path("api/clear-errors", hub_write.clear_errors),
    # CI results: gated by the CI webhook secret, not an agent credential (the sender is CI).
    path("api/ci-event", hub_write.ci_event, name="ci-event"),
    # The problem queue's verbs: a console claims, resolves (acks every row behind it),
    # releases, or escalates a diagnosed problem onto the ask/task it waits for.
    path("api/problem/claim", hub_write.problem_claim),
    path("api/problem/resolve", hub_write.problem_resolve),
    path("api/problem/release", hub_write.problem_release),
    path("api/problem/escalate", hub_write.problem_escalate),
    path("api/overlap/seen", hub_write.overlap_seen),
    # A machine un-enrolls itself: its scoped credential revokes itself.
    path("api/leave", hub_write.leave),
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
    path("api/menu/rank", app_services.menu_rank_view),
    path("api/history", histories_api.upload, name="history-upload"),
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
    "item-claims.json": "veiled", "components.json": "veiled",
    "problems.json": "veiled", "app_health.json": "veiled", "doctor.json": "veiled",
    "overlap.json": "veiled", "enroll/status.json": "veiled",
    "app-feed.json": "veiled", "components/": "open", "components/props/<str:slug>.json": "open",
    "components/<str:name>/<str:filename>": "open",
    "history.json": "member", "doctrine.json": "veiled", "evidence.json": "member",
    "related.json": "veiled", "guidance.json": "veiled", "knowledge/since": "veiled",
    "capabilities.json": "veiled", "eval.json": "member",
    "distribution.json": "veiled", "built.json": "veiled", "ci-events.json": "member",
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
