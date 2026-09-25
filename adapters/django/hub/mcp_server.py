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
import re

from django.http import JsonResponse

from hub_core import identity, runs, schedule

from . import hub_app
from .hub_write import writer

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
HUB_LEASE_META = "io.github.hub-scaffold/leaseToken"


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
     "description": "Pull the readiness rail: top ready tasks plus work that needs specification. "
                    "`unattended: true` returns only the unattended lane (marked tasks, P0-P2, never a decision).",
     "inputSchema": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "how many rows (default 3)"},
         "unattended": {"type": "boolean"},
         "agent": {"type": "string",
                   "description": "the calling agent; hides work given to somebody else"},
         "machine": {"type": "string",
                     "description": "the calling machine; hides work only another machine can do"}}}},
    {"name": "create_task",
     "description": "File a task. `unattended` offers it to unattended workers (P0-P2); `decision` makes it a "
                    "person's call, delivered to the deciders and never to an unattended worker. Pass the same "
                    "`idem_key` on a retry to get the first record back instead of a duplicate.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "acceptance": {"type": "string"}, "agent": {"type": "string"},
         "priority": {"enum": ["P0", "P1", "P2", "P3"]}, "project": {"type": "string"},
         "unattended": {"type": "boolean"}, "decision": {"type": "boolean"},
         "idem_key": {"type": "string"}},
         "required": ["title", "acceptance", "agent"]}},
    {"name": "hand_task",
     "description": "Hand a task off. With `to`: GIVE it to that named agent (to=\"\" clears it) -- `agent` is "
                    "who is writing, the recipient is shown as owner and their inbox carries it until they "
                    "claim it; `machine` also sets MACHINE AFFINITY. Without `to`: hand it BACK to the queue "
                    "for an unattended worker (todo, unattended, lease released); without a lease token it "
                    "releases only a lease an orphaned console of the same agent holds.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"}, "to": {"type": "string"},
         "machine": {"type": "string",
                     "description": "with `to`: also set MACHINE AFFINITY (only this machine can do it)"},
         "lease_token": {"type": "string"}, "note": {"type": "string"}},
         "required": ["id"]}},
    {"name": "unclaim_task",
     "description": "Let go of a task (lease released, in-progress back to todo); same orphaned-lease remedy as hand_task.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"}, "lease_token": {"type": "string"},
         "note": {"type": "string"}}, "required": ["id", "agent"]}},
    {"name": "recall_task",
     "description": "A task's joined row: status, holder (live or abandoned), run, pushed/deployed commits, "
                    "hand-backs, checkpoints and evidence — what a peer needs before picking it up.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "decide_task",
     "description": "Decide a decision task: then=file (mint the build task), close, or reply (a question back; "
                    "stays open). Only a named decider's own credential may; agents are refused.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "decision": {"type": "string"},
         "then": {"enum": ["file", "close", "reply"]}}, "required": ["id", "decision", "then"]}},
    {"name": "board_attention",
     "description": "What needs a person: each condition with who acts, the exact fix, its values and how long it has stood.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "board_consoles",
     "description": "Live consoles (attended, unattended runs, finished recaps) and crossovers between them; "
                    "`session` returns the signals addressed to that console with peer evidence and a suggested split.",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}}},
    {"name": "project_tasks",
     "description": "One project's annotated task feed: open tasks plus those finished in the last 14 days.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}},
                     "required": ["project"]}},
    {"name": "spec_task",
     "description": "Give needs-spec work concrete acceptance; probes are reserved for rare critical boundaries.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "acceptance": {"type": "string"}, "verification_command": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "start_task",
     "description": "Claim a task and receive the fenced lease token required by run and completion operations. "
                    "Pass `session` (this console's id) so the board binds the claim to THIS console, not to every console the agent has open.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "session": {"type": "string", "description": "the claiming console's session id"}},
         "required": ["id", "agent"]}},
    {"name": "take_task",
     "description": "Atomically select and claim the highest-ranked ready task compatible with this worker.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"},
         "ttl_s": {"type": "integer", "minimum": 1, "maximum": 86400},
         "session": {"type": "string", "description": "the claiming console's session id"},
         "worker": schedule.WORKER_PROFILE_SCHEMA}, "required": ["agent"]}},
    {"name": "task_lineage",
     "description": "Trace a task hop by hop to what is serving it: the commits it recorded, the "
                    "verified deploy that carries one, the first release to carry it, and whether "
                    "the newest verified deploy still contains it. Unknown hops say why.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                     "required": ["id"]}},
    {"name": "hold_commit",
     "description": "Record a finished commit deliberately NOT live yet (it must be rebuilt/"
                    "proven first) so it ages in public. Refused for a commit nobody else can "
                    "fetch unless unpushed_reason records why.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "sha": {"type": "string"}, "reason": {"type": "string"},
         "rebuild": {"type": "string"}, "branch": {"type": "string"},
         "from_gap": {"type": "string"}, "attested": {"type": "boolean"},
         "unpushed_reason": {"type": "string"}, "agent": {"type": "string"}},
         "required": ["repo", "sha", "reason", "rebuild"]}},
    {"name": "promote_held",
     "description": "Free a held commit: the rebuild ran; evidence is the pipeline, sha or URL "
                    "that proves it.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "sha": {"type": "string"}, "evidence": {"type": "string"},
         "note": {"type": "string"}, "agent": {"type": "string"}},
         "required": ["repo", "sha", "evidence"]}},
    {"name": "held_queue",
     "description": "The promotion queue: every open hold, oldest first, with its age and urgency.",
     "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}}},
    {"name": "claim_item",
     "description": "Claim a non-task item (a question id or an error fingerprint) for ONE machine "
                    "so two machines never work the same thing; the same machine re-claims "
                    "idempotently. release=true gives it back. session names the console "
                    "holding it: a claim whose console is provably gone frees itself after the "
                    "grace, and a refusal says when.",
     "inputSchema": {"type": "object", "properties": {
         "item": {"type": "string"}, "machine": {"type": "string"},
         "release": {"type": "boolean"}, "agent": {"type": "string"},
         "session": {"type": "string"}},
         "required": ["item", "machine"]}},
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
    {"name": "hand_back_task",
     "description": "A run is ending with its task unfinished: return it to todo with ONE "
                    "self-counting hand-back row (shown on the task, never counted as a done "
                    "step) and release the fenced lease, so the board stops reading it as in flight.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"},
         "note": {"type": "string", "description": "why the run ended unfinished; what is left"}},
         "required": ["id", "agent", "lease_token", "note"]}},
    {"name": "fail_task",
     "description": "Atomically record a real failure, return the lease, and create or reuse routed repair work.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "signature": {"type": "string"},
         "note": {"type": "string"}, "kind": {"type": "string"},
         "consequential": {"type": "boolean"},
         "evidence": {"type": "array", "items": {"type": "string"}}},
         "required": ["id", "agent", "lease_token", "signature", "note"]}},
    {"name": "step_task",
     "description": "Record a checkpoint on a task: mark a plan step done with what actually happened. "
                    "Pass `sha` (+ `pipeline_id`/`pipeline_url`) to record a typed `pushed` checkpoint naming "
                    "the commit — the record a verified deploy later closes the task against. A planless task "
                    "takes the note as a new checkpoint; a number past the end grows uncounted placeholders.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string", "description": "required while the task is leased"},
         "step": {"type": "string", "description": "1-based number or text fragment; default first undone"},
         "note": {"type": "string"},
         "kind": {"type": "string", "description": "checkpoint (default), pushed, deployed, or a lifecycle kind"},
         "sha": {"type": "string"}, "pipeline_id": {"type": "string"},
         "pipeline_url": {"type": "string"}},
         "required": ["id", "agent"]}},
    {"name": "plan_task",
     "description": "Declare or extend a task's checklist; existing steps keep their state.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"},
         "steps": {"type": "array", "items": {"type": "string"}}},
         "required": ["id", "agent", "steps"]}},
    {"name": "finish_task",
     "description": "Complete the board work contract after its real operation succeeds.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "agent": {"type": "string"},
         "lease_token": {"type": "string"}, "note": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"}},
         "verification_run": {"type": "object"}},
         "required": ["id", "agent", "lease_token", "note", "evidence"]}},
    {"name": "ask_operator",
     "description": "Blocked on a fact only the operator has? File a DELIVERED question instead of "
                    "stalling in silence. Refused with matching ids when the board already has it; "
                    "pass anyway=true for a genuinely different question.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "question": {"type": "string"},
         "context": {"type": "string"}, "anyway": {"type": "boolean"},
         "to": {"type": "string", "description": "address one agent; default the operator"},
         "human_only": {"type": "boolean",
                        "description": "only a person can satisfy it: delivered as a gate"},
         "idem_key": {"type": "string", "description": "repeat it on a retry: a call that "
                      "already landed replays instead of being refused as its own duplicate"},
         "hop": {"type": "integer", "minimum": 0, "maximum": 9,
                 "description": "unattended runs only: the HUB_RESPONDER_HOP your launcher set"}},
         "required": ["agent", "question"]}},
    {"name": "answer_question",
     "description": "Answer an open question and retire it. The reply is addressed to the asker "
                    "(and to the console that asked); a correction bumps its delivery revision.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"}, "text": {"type": "string"},
         "crystallize": {"type": "boolean"}}, "required": ["question", "text"]}},
    {"name": "check_inbox",
     "description": "What is addressed to this agent right now — messages to it, questions it "
                    "should answer (stuck asks reach everyone), directives aimed at it and the "
                    "answer to its own question, fresh unclaimed problems it owns, and crossovers "
                    "with other live consoles it has not been told. Name your session to get only "
                    "this console's mail (and deliveries pinned to it); machine pins likewise. "
                    "Ack what you have acted on.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "session": {"type": "string"},
         "machine": {"type": "string"}}, "required": ["agent"]}},
    {"name": "ack_item",
     "description": "Acknowledge ANY addressed id, routed by its own type: a crossover (ov-...) "
                    "is recorded as seen, a problem (p-<12 hex>) is RESOLVED with the note as "
                    "its root cause, a directive/answer is acked, and a 16-hex error signature "
                    "the directive store does not know is acked as a signature.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "id": {"type": "string"},
         "note": {"type": "string"}, "evidence": {"type": "string"}},
         "required": ["agent", "id"]}},
    {"name": "list_problems",
     "description": "The operational error stream FOLDED into problems — one per thing somebody "
                    "fixes, with state (unclaimed / in_flight / escalated / resolved), holder, "
                    "count, recency and cause. include=all adds what the read-time bar holds "
                    "back (with the reason); id=<p-id> returns one with its full stored trace.",
     "inputSchema": {"type": "object", "properties": {
         "include": {"enum": ["", "resolved", "all"]}, "app": {"type": "string"},
         "id": {"type": "string"}}}},
    {"name": "claim_problem",
     "description": "Put this console's name on a problem BEFORE digging, so no other console "
                    "duplicates the work. Another live console's claim is refused naming the "
                    "holder; take=true displaces it on the record.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "problem": {"type": "string"},
         "note": {"type": "string"}, "take": {"type": "boolean"},
         "session": {"type": "string"}, "machine": {"type": "string"},
         "name": {"type": "string"}}, "required": ["agent", "problem"]}},
    {"name": "resolve_problem",
     "description": "Resolve a problem: acknowledge every row behind it at once and record the "
                    "ROOT CAUSE and evidence where the next person will look. A recurrence "
                    "afterwards reopens it.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "problem": {"type": "string"},
         "note": {"type": "string"}, "evidence": {"type": "string"},
         "session": {"type": "string"}}, "required": ["agent", "problem", "note"]}},
    {"name": "release_problem",
     "description": "Hand a claimed (or reopened-under-you) problem back to the queue.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "problem": {"type": "string"}},
         "required": ["agent", "problem"]}},
    {"name": "escalate_problem",
     "description": "Park a DIAGNOSED problem on the open ask or task it is waiting for; it "
                    "leaves the unclaimed queue until that blocker closes.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "problem": {"type": "string"},
         "blocked_on": {"type": "string"}, "note": {"type": "string"}},
         "required": ["agent", "problem", "blocked_on"]}},
    {"name": "app_health",
     "description": "Every service and whether its failures can REACH the board: observed / "
                    "partial / dark / unbuilt, with each gap named as an observation. An empty "
                    "problem list for a dark service means nothing.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "diagnose_app",
     "description": "One service diagnosed from evidence: its health row, OPEN problems "
                    "(resolved ones listed apart as history), and BLOCKED vs WAITING.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string"}}, "required": ["app"]}},
    {"name": "check_crossovers",
     "description": "Crossovers between this console and other live consoles (same file, "
                    "problem, task, project subtree or subject), each with who, what they are "
                    "on, how to reach them and a proposed split. Without agent/session: the "
                    "whole roster and every pair.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "session": {"type": "string"}}}},
    {"name": "ack_directive",
     "description": "Record that a directive/answer was delivered to this agent; it leaves the "
                    "inbox, and a directive acked by every named target retires itself. Pass the "
                    "delivery_revision you read when the item carries one.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "directive": {"type": "string"},
         "note": {"type": "string"}, "delivery_revision": {"type": "integer"},
         "idem_key": {"type": "string"}},
         "required": ["agent", "directive"]}},
    {"name": "send_message",
     "description": "Send mail to another agent; it is delivered into their inbox (to one of "
                    "their consoles when `session` names it).",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "to": {"type": "string"}, "note": {"type": "string"},
         "title": {"type": "string"}, "session": {"type": "string"},
         "machine": {"type": "string"}}, "required": ["agent", "to", "note"]}},
    {"name": "ack_message",
     "description": "Retire a message that reached you (only its recipient may).",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "id": {"type": "string"}, "via": {"type": "string"}},
         "required": ["agent", "id"]}},
    {"name": "post_update",
     "description": "Post one first-person line to the agents' updates feed — what you just fixed, "
                    "answered, acked or shipped, with the sha/URL that proves it.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "summary": {"type": "string"},
         "kind": {"enum": ["fixed", "answered", "acked", "shipped", "escalated", "noop"]},
         "evidence": {"type": "string"}, "item": {"type": "string"}},
         "required": ["agent", "summary"]}},
    {"name": "report_ci_failure",
     "description": "Report a failed CI job with the tail of its log. The hub classifies what the "
                    "LOG says (rollback, real failure, stopped before deploying, unclear) and files "
                    "one operational row whose severity follows that verdict, not the trigger.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "job": {"type": "string"},
         "trace": {"type": "string", "description": "the job log tail"},
         "pipeline": {"type": "string"}, "job_id": {"type": "string"},
         "ref": {"type": "string"}, "sha": {"type": "string"},
         "source": {"type": "string", "description": "push, schedule, api, ..."},
         "deployless": {"type": "boolean"}, "url": {"type": "string"}},
         "required": ["project", "job"]}},
    {"name": "record_deploy",
     "description": "Record one verified release AFTER the front-door canary observed its sha. "
                    "Immutable and idempotent by sha: an exact repeat answers idempotent, a "
                    "changed proof for the same sha is refused. tasks_closed names the done tasks "
                    "this release carries (an empty list is allowed).",
     "inputSchema": {"type": "object", "properties": {
         "sha": {"type": "string"},
         "served_sha": {"type": "string", "description": "what the canary observed; must equal sha"},
         "tasks_closed": {"type": "array", "items": {"type": "string"}},
         "at": {"type": "string", "description": "UTC record time; stamped now when omitted"},
         "method": {"type": "string"}, "build": {"type": "string"},
         "audit_ok": {"type": "boolean"}},
         "required": ["sha", "served_sha", "tasks_closed"]}},
    {"name": "list_components",
     "description": "Standard components and app skeletons. Asked to build a standard app, or to "
                    "apply components to one? Read the skeleton: its applied rows carry each "
                    "component's CURRENT get/entry, in dependency order, and name anything missing.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"enum": ["component", "skeleton"]}}}},
    {"name": "register_component",
     "description": "Register (or update, with expected_version) a standard component or an app "
                    "skeleton in the capability graph.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "kind": {"enum": ["component", "skeleton"]},
         "maturity": {"enum": ["concept", "prototype", "proven", "reusable", "extracted"]},
         "what": {"type": "string"}, "when": {"type": "string"}, "get": {"type": "string"},
         "entry": {"type": "string"}, "delivery": {"enum": ["copy", "hosted", "package"]},
         "hosted_at": {"type": "string"}, "exemplar": {"type": "string"},
         "default": {"type": "boolean"},
         "depends_on": {"type": "array", "items": {"type": "string"}},
         "applies": {"type": "array", "items": {"type": "string"}},
         "applies_all": {"type": "boolean"},
         "expected_version": {"type": "integer"}},
         "required": ["name", "kind"]}},
    {"name": "app_feed",
     "description": "One app's slice of the board: open tasks and built-on-request notes that name "
                    "the app, each row saying which field matched.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string"}, "name": {"type": "string"}}, "required": ["app"]}},
    {"name": "app_fixes",
     "description": "The errors one app forwarded that reached the board, each as its problem and "
                    "mirror task: state (reported, being fixed, waiting on a person, fixed), who "
                    "has it, the fixer's steps and, once fixed, the root cause.",
     "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                     "required": ["app"]}},
    {"name": "list_hosted_components",
     "description": "The UI components this hub hosts for its apps: measured versions, files, how "
                    "to link them, and the apps observed loading them.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_component_props",
     "description": "One app's component properties plus the schema that bounds them.",
     "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}},
                     "required": ["app"]}},
    {"name": "set_component_props",
     "description": "REPLACE one app's component properties ({component: {key: value}}; keys left "
                    "out return to defaults). Needs component:configure. Refused keys are listed.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string"}, "props": {"type": "object"}}, "required": ["app", "props"]}},
    {"name": "person_profile",
     "description": "Read or change one person's cross-app presentation preferences on behalf of "
                    "the app that signed them in: theme, motion, ui/text size, font, agent "
                    "placement, sidebar state, the two help switches, starred apps and their mark. "
                    "With no `prefs` it reads, including the apps the person can reach. With `app` "
                    "the change goes to that app's override (an empty prefs object removes it) and "
                    "the answer resolves for that app. Needs profile:read / profile:write.",
     "inputSchema": {"type": "object", "properties": {
         "person": {"type": "string"}, "app": {"type": "string"},
         "prefs": {"type": "object"}}, "required": ["person"]}},
    {"name": "ask_agent",
     "description": "Ask the brokered agent service a question (the hub holds its key). Answers "
                    "carry citations; an unconfigured or failing lane says exactly why.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"}, "app": {"type": "string"}}, "required": ["question"]}},
    {"name": "search_board",
     "description": "Ranked search over the whole board — use it BEFORE asking; the fact may "
                    "already be recorded.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         "required": ["query"]}},
    {"name": "seat_distribution",
     "description": "Is every seat running what this hub publishes? Per-seat artifact grades; "
                    "offline seats are named and never graded as drift; the verdict states what "
                    "it did not grade.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "built_by_person",
     "description": "What each person built, derived from the ledger (completed tasks, releases, "
                    "authored gaps/feats/ADRs/decisions/notes), machines folded into their person.",
     "inputSchema": {"type": "object", "properties": {
         "person": {"type": "string", "description": "narrow to one identity"}}}},
    {"name": "record_entity",
     "description": "Create or amend a gap/feat/note/adr/decision/capability through its versioned "
                    "upsert; the current version is read first and one lost race is retried.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"enum": ["gap", "feat", "note", "adr", "decision", "capability"]},
         "fields": {"type": "object"}, "agent": {"type": "string"}},
         "required": ["type", "fields"]}},
    {"name": "ci_events",
     "description": "The raw CI deliveries behind a CI row (credential needs ci:read).",
     "inputSchema": {"type": "object", "properties": {
         "pipeline": {"type": "string"}, "job": {"type": "string"},
         "project": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "list_collection",
     "description": "Every row of one board collection (task, adr, feat, gap, cap, deploy, "
                    "note, directive, ack, run). The board snapshot may carry a large collection "
                    "only as its newest rows; this returns all of them.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string"}}, "required": ["type"]}},
    {"name": "read_errors",
     "description": "The operational error stream with the bar applied: metadata carries the "
                    "QUEUE's counts (on_board, unclaimed, claimed, oldest_unclaimed_s) and what "
                    "the bar held back (deferred, off_board) whatever you list. app= narrows to "
                    "one service; include=deferred also lists the held-back rows.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string"},
         "include": {"enum": ["deferred", "all"]}}}},
    # Knowledge: record what was learned by its kind, find it again, and read the per-prompt index.
    {"name": "share_lesson",
     "description": "Record a LESSON — a rule earned from a mistake. Admitted and tagged with the "
                    "records it may duplicate or correct (never refused for resemblance); an identical "
                    "live rule returns duplicate_of and writes nothing.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "rule": {"type": "string"}, "why": {"type": "string"},
         "tier": {"enum": ["normal", "foundational"]},
         "verify": {"type": "string", "description": "for a state claim: the command/URL that answers it now"},
         "verified_as_of": {"type": "string"}, "supersedes": {"type": "string"},
         "tags": {"type": "array", "items": {"type": "string"}}},
         "required": ["agent", "rule"]}},
    {"name": "record_knowledge",
     "description": "Record a FINDING (a fact discovered about how a system behaves), a METHOD (a "
                    "procedure the team follows) or a REVIEW (a question only a person may answer).",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"enum": ["finding", "method", "review"]}, "agent": {"type": "string"},
         "title": {"type": "string"}, "note": {"type": "string"}, "evidence": {"type": "string"},
         "category": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
         "relates_to": {"type": "array", "items": {"type": "string"}},
         "expected_version": {"type": "integer"}},
         "required": ["kind", "agent", "title"]}},
    {"name": "record_gap",
     "description": "Record a GAP — a named deficiency someone could own and close, with a severity.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "title": {"type": "string"},
         "severity": {"enum": ["P0", "P1", "P2", "P3"]},
         "note": {"type": "string", "description": "what is missing and how it shows"},
         "evidence": {"type": "string"},
         "source": {"type": "string"}},
         "required": ["agent", "title", "severity"]}},
    {"name": "recall_record",
     "description": "One board record in full by id, including any overlap suspicions and their "
                    "verdicts; a phrase falls through to ranked search.",
     "inputSchema": {"type": "object", "properties": {
         "ref": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         "required": ["ref"]}},
    {"name": "find_related",
     "description": "Records close to an id (or free text) by weighted vocabulary AND by meaning; "
                    "each basis says when it could not run.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "list_capabilities",
     "description": "What an agent can already do here: ledger capabilities merged with the "
                    "published catalog, with the publication commit and state.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "q": {"type": "string"}}}},
    {"name": "board_guidance",
     "description": "The knowledge index ranked for what this agent is doing (focus), with the "
                    "live block (inbox, unclaimed errors). memory_rank says how it was ordered.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "focus": {"type": "string"},
         "memory_cap": {"type": "integer", "minimum": 1, "maximum": 200},
         "memory_full": {"type": "integer", "minimum": 0, "maximum": 40}}}},
    {"name": "retire_record",
     "description": "Retire (or re-open) a knowledge record that stopped being true: a gap, note, "
                    "directive, ADR or finding. A reason is required and is appended with a dated "
                    "stamp; a closed/mitigated gap must name the task that closed it. Retired "
                    "records leave search immediately.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "type": {"type": "string"},
         "title": {"type": "string", "description": "exact title, with type, instead of id"},
         "status": {"type": "string"}, "note": {"type": "string"},
         "addressed_by": {"type": "array", "items": {"type": "string"}},
         "superseded_by": {"type": "string"}, "agent": {"type": "string"}},
         "required": ["agent"]}},
    {"name": "console_history",
     "description": "Read how a console is being driven, to coach prompting: with no session, the "
                    "stored consoles; with agent + session, that console's prompts, replies and "
                    "one-line tool calls (never tool output). Needs history:read; answers 404 "
                    "while the hub has histories disabled.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "machine": {"type": "string"},
         "session": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 3000}}}},
    {"name": "record",
     "description": "Record what you learned as the RIGHT kind of record: finding (a discovered "
                    "fact, with evidence), method (a reusable procedure), gap (an ownable "
                    "deficiency with a severity), or review (a human gate delivered to the "
                    "operator before the thing ships).",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"enum": ["finding", "method", "gap", "review"]},
         "agent": {"type": "string"}, "title": {"type": "string"},
         "text": {"type": "string",
                  "description": "evidence (finding/gap), how it is done (method), or the "
                                 "context a person needs to decide (review)"},
         "severity": {"enum": ["P0", "P1", "P2", "P3"], "description": "gap only"},
         "relates_to": {"type": "array", "items": {"type": "string"}}},
         "required": ["kind", "agent", "title", "text"]}},
    {"name": "read_doctrine",
     "description": "Read a standing document (doctrine, charter, agents) exactly as this "
                    "credential may see it; audience-scoped sections are applied server-side.",
     "inputSchema": {"type": "object", "properties": {
         "doc": {"type": "string", "description": "document name (default doctrine)"}}}},
    {"name": "report_app_error",
     "description": "Put a satellite service's failure on the operational error stream, attributed "
                    "to the APP: a server exception, a background-job death, or a fault inside an "
                    "agentic chat loop (kind=agent; send thread/turn/tool, never the prompt). "
                    "Needs a token with error:report.",
     "inputSchema": {"type": "object", "properties": {
         "app": {"type": "string", "description": "the service's slug"},
         "message": {"type": "string"},
         "kind": {"type": "string", "description": "server|background|django|data|other|agent|"
                                                   "js|promise|http|stream (default server)"},
         "severity": {"enum": ["info", "warning", "error", "critical"]},
         "code": {"type": "string"}, "details": {"type": "string"},
         "component": {"type": "string"}, "operation": {"type": "string"},
         "path": {"type": "string"}, "host": {"type": "string"}},
         "required": ["app", "message"]}},
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
    # The caller's console and machine ride through, so a lease an MCP client takes is bound
    # to that console exactly like one taken over plain HTTP (hub_core.liveness).
    for name, value in (auth_headers.get("presence") or {}).items():
        if value:
            forwarded["HTTP_" + name.upper().replace("-", "_")] = value
    if method == "get":
        response = client.get(path, payload, **forwarded)
    else:
        response = client.post(path, data=json.dumps(payload), content_type="application/json",
                               **forwarded)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.content.decode("utf-8", "replace")[:500]}


def _ack_item(args, auth_headers):
    """The same id-type routing the CLI's `ack` does, over the same seams."""
    import re
    item = str(args.get("id") or "").strip()
    agent = args["agent"]
    if item.startswith("ov-"):
        return _seam("/hub/api/overlap/seen", {"agent": agent, "ids": [item]}, auth_headers)
    m = re.fullmatch(r"(?:problem:)?(p-[0-9a-f]{12})", item.lower())
    if m:
        if not args.get("note"):
            return 422, {"errors": [{"code": "need_note",
                                     "msg": "a problem is closed by resolving it: pass note "
                                            "(the root cause) and evidence"}]}
        return _seam("/hub/api/problem/resolve",
                     {"agent": agent, "problem": m.group(1), "note": args["note"],
                      "evidence": args.get("evidence") or ""}, auth_headers)
    payload = {"agent": agent, "directive": item}
    if args.get("note"):
        payload["note"] = args["note"]
    status, body = _seam("/hub/api/ack", payload, auth_headers)
    unknown = status == 404 and "unknown_directive" in json.dumps(body)
    if unknown and re.fullmatch(r"[0-9a-f]{16}", item.lower()):
        return _seam("/hub/api/ack-error", {"agent": agent, "fingerprint": item.lower(),
                                            "note": args.get("note") or ""}, auth_headers)
    return status, body


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


def _plan_write(args, auth_headers, mutate, attempts=3):
    """Read the task, apply `mutate(plan) -> plan`, write it through the ordinary task seam
    under OCC, and re-apply on a version race — the same bounded retry the client's
    `_task_write` uses, so a checkpoint never dead-letters on a race it could simply redo."""
    status, body = 404, {"errors": [{"code": "not_found", "msg": args["id"]}]}
    for _attempt in range(attempts):
        entity, version = _entity_version(args["id"])
        if entity is None or entity.get("type") != "task":
            return 404, {"errors": [{"code": "not_found", "msg": args["id"]}]}
        try:
            plan = mutate(entity.get("plan") or [])
        except ValueError as exc:
            return 422, {"errors": [{"code": "bad_step", "msg": str(exc)}]}
        payload = {"id": args["id"], "agent": args["agent"], "expected_version": version,
                   "plan": plan}
        if args.get("lease_token"):
            payload["token"] = args["lease_token"]
        status, body = _seam("/hub/api/task", payload, auth_headers)
        codes = {e.get("code") for e in (body.get("errors") or []) if isinstance(e, dict)}
        if status not in (409, 428) or not codes & {"conflict", "precondition_required"}:
            break
    return status, body


def _record(args, auth_headers):
    """One tool, four record kinds, each landing on the write route that owns it (the same
    mapping as the CLI's finding/method/gap/review verbs)."""
    kind, agent = args["kind"], args["agent"]
    title, text = str(args["title"]), str(args["text"])
    related = [str(r) for r in (args.get("relates_to") or [])]
    if kind in ("finding", "method"):
        payload = {"agent": agent, "title": title, "body_md": text, "status": "standing",
                   "category": "discovery" if kind == "finding" else "method",
                   "tags": [kind]}
        if related:
            payload["relates_to"] = related
        return _seam("/hub/api/note", payload, auth_headers)
    if kind == "gap":
        if args.get("severity") not in ("P0", "P1", "P2", "P3"):
            return 400, {"errors": [{"code": "need_severity",
                                     "msg": "a gap carries a severity P0..P3"}]}
        return _seam("/hub/api/gap", {"agent": agent, "title": title, "status": "open",
                                      "severity": args["severity"], "evidence": text},
                     auth_headers)
    if kind == "review":
        payload = {"agent": agent, "question": "Review gate: " + title, "anyway": True,
                   "review": True,
                   "context": text + "\n\nThis is a human gate: nothing it covers ships "
                                     "until a person answers."}
        if related:
            payload["relates_to"] = related
        return _seam("/hub/api/ask", payload, auth_headers)
    return 400, {"errors": [{"code": "unknown_kind", "msg": str(kind)}]}


def _call_tool(name, args, auth_headers):
    """Return ``(standard_tool_result, newly_created_run_or_none)``."""
    if name == "board_next":
        query = {"n": int(args.get("n", 3))}
        if args.get("unattended"):
            query["unattended"] = "1"
        for key in ("agent", "machine"):
            if args.get(key):
                query[key] = args[key]
        status, body = _seam("/hub/next.json", query, auth_headers, method="get")
    elif name == "create_task":
        payload = {k: args[k] for k in ("title", "acceptance", "agent", "priority", "project",
                                        "idem_key") if args.get(k)}
        if args.get("decision"):
            payload["work_kind"] = "decision"
        elif args.get("unattended"):
            payload["unattended"] = True
        status, body = _seam("/hub/api/task", payload, auth_headers)
    elif name == "hand_task" and "to" in args:
        payload = {"id": args["id"], "to": args["to"]}
        for key in ("agent", "machine"):
            if args.get(key) is not None:
                payload[key] = args[key]
        status, body = _seam("/hub/api/assign", payload, auth_headers)
    elif name in ("hand_task", "unclaim_task"):
        payload = {"id": args["id"], "agent": args.get("agent") or "agent"}
        if args.get("lease_token"):
            payload["token"] = args["lease_token"]
        if args.get("note"):
            payload["note"] = args["note"]
        status, body = _seam("/hub/api/" + ("hand-to-queue" if name == "hand_task" else "unclaim"),
                             payload, auth_headers)
    elif name == "recall_task":
        local = str(args["id"]).rsplit(":", 1)[-1]
        status, body = _seam("/hub/task/%s.json" % local, {}, auth_headers, method="get")
    elif name == "decide_task":
        status, body = _seam("/hub/api/task/decide", {"id": args["id"], "decision": args["decision"],
                                                      "then": args["then"]}, auth_headers)
    elif name == "board_attention":
        status, body = _seam("/hub/attention.json", {}, auth_headers, method="get")
    elif name == "board_consoles":
        query = {"session": args["session"]} if args.get("session") else {}
        status, body = _seam("/hub/consoles.json", query, auth_headers, method="get")
    elif name == "project_tasks":
        from urllib.parse import quote
        status, body = _seam("/hub/project/%s/tasks.json" % quote(str(args["project"]), safe=""),
                             {}, auth_headers, method="get")
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
        payload = {"id": args["id"], "agent": args["agent"]}
        if args.get("session"):
            payload["session"] = args["session"]
        status, body = _seam("/hub/api/claim", payload, auth_headers)
    elif name == "take_task":
        payload = {"agent": args["agent"]}
        for key in ("ttl_s", "worker", "session"):
            if args.get(key) is not None:
                payload[key] = args[key]
        status, body = _seam("/hub/api/take", payload, auth_headers)
    elif name == "task_lineage":
        local = str(args["id"]).rsplit(":", 1)[-1]
        status, body = _seam("/hub/task/%s.json" % local, {"lineage": "1"}, auth_headers,
                             method="get")
        if status < 400:
            body = {"data": (body.get("data") or {}).get("lineage") or {}}
    elif name == "hold_commit":
        payload = {k: args[k] for k in ("repo", "sha", "reason", "rebuild", "branch", "from_gap",
                                        "attested", "unpushed_reason", "agent")
                   if args.get(k) not in (None, "")}
        status, body = _seam("/hub/api/held", payload, auth_headers)
    elif name == "promote_held":
        payload = {k: args[k] for k in ("repo", "sha", "evidence", "note", "agent")
                   if args.get(k) not in (None, "")}
        status, body = _seam("/hub/api/held/promote", payload, auth_headers)
    elif name == "held_queue":
        status, body = _seam("/hub/held.json",
                             {"repo": args["repo"]} if args.get("repo") else {},
                             auth_headers, method="get")
    elif name == "claim_item":
        payload = {k: args[k] for k in ("item", "machine", "release", "agent", "session")
                   if args.get(k) not in (None, "")}
        status, body = _seam("/hub/api/item-claim", payload, auth_headers)
    elif name == "heartbeat_task":
        payload = {"id": args["id"], "token": args["lease_token"]}
        if args.get("ttl_s") is not None:
            payload["ttl_s"] = args["ttl_s"]
        status, body = _seam("/hub/api/heartbeat", payload, auth_headers)
    elif name == "release_task":
        status, body = _seam("/hub/api/release", {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
        }, auth_headers)
    elif name == "hand_back_task":
        status, body = _seam("/hub/api/hand-back", {
            "id": args["id"], "agent": args["agent"], "token": args["lease_token"],
            "note": args["note"],
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
    elif name == "step_task":
        import datetime as _dt
        from hub_core import checkpoints as _cp
        at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        kind = args.get("kind") or ("pushed" if args.get("sha") else "checkpoint")
        status, body = _plan_write(args, auth_headers, lambda plan: _cp.apply_step(
            plan, step=args.get("step"), note=args.get("note") or "", kind=kind,
            sha=args.get("sha") or "", pipeline_id=args.get("pipeline_id") or "",
            pipeline_url=args.get("pipeline_url") or "", at=at)[0])
    elif name == "plan_task":
        steps = [str(x).strip() for x in (args.get("steps") or []) if str(x).strip()]

        def extend(plan):
            if not steps:
                raise ValueError("steps must name at least one step")
            rows = [dict(x) for x in plan if isinstance(x, dict)]
            have = {str(x.get("step") or "").strip().lower() for x in rows}
            for step in steps:
                if step.lower() not in have:
                    rows.append({"step": step[:200], "done": False})
                    have.add(step.lower())
            return rows
        status, body = _plan_write(args, auth_headers, extend)
    elif name == "finish_task":
        payload = {"id": args["id"], "agent": args["agent"],
                   "token": args["lease_token"], "accept_note": args["note"],
                   "evidence_uri": args["evidence"]}
        if args.get("verification_run"):
            payload["verification_run"] = args["verification_run"]
        status, body = _seam("/hub/api/complete", payload, auth_headers)
    elif name == "ask_operator":
        payload = {"agent": args["agent"], "question": args["question"]}
        for key in ("context", "anyway", "to", "human_only", "idem_key", "hop"):
            if args.get(key):
                payload[key] = args[key]
        status, body = _seam("/hub/api/ask", payload, auth_headers)
    elif name == "answer_question":
        payload = {"question": args["question"], "text": args["text"]}
        if args.get("crystallize"):
            payload["crystallize"] = True
        status, body = _seam("/hub/api/answer", payload, auth_headers)
    elif name == "check_inbox":
        query = {"agent": args["agent"]}
        for key in ("session", "machine"):
            if args.get(key):
                query[key] = args[key]
        status, body = _seam("/hub/inbox.json", query, auth_headers, method="get")
    elif name == "ack_item":
        status, body = _ack_item(args, auth_headers)
    elif name == "list_problems":
        query = {k: args[k] for k in ("include", "app", "id") if args.get(k)}
        status, body = _seam("/hub/problems.json", query, auth_headers, method="get")
    elif name in ("claim_problem", "resolve_problem", "release_problem", "escalate_problem"):
        payload = {k: v for k, v in args.items() if v not in (None, "")}
        status, body = _seam("/hub/api/problem/" + name.split("_", 1)[0], payload, auth_headers)
    elif name == "app_health":
        status, body = _seam("/hub/app_health.json", {}, auth_headers, method="get")
    elif name == "diagnose_app":
        status, body = _seam("/hub/doctor.json", {"app": args["app"]}, auth_headers, method="get")
    elif name == "check_crossovers":
        query = {k: args[k] for k in ("agent", "session") if args.get(k)}
        status, body = _seam("/hub/overlap.json", query, auth_headers, method="get")
    elif name == "ack_directive":
        payload = {"agent": args["agent"], "directive": args["directive"]}
        if args.get("note"):
            payload["note"] = args["note"]
        if args.get("delivery_revision") is not None:
            payload["delivery_revision"] = int(args["delivery_revision"])
        if args.get("idem_key"):
            payload["idem_key"] = args["idem_key"]
        status, body = _seam("/hub/api/ack", payload, auth_headers)
    elif name == "send_message":
        payload = {"agent": args["agent"], "to": args["to"], "note": args["note"]}
        for key in ("title", "session", "machine"):
            if args.get(key):
                payload[key] = args[key]
        status, body = _seam("/hub/api/message", payload, auth_headers)
    elif name == "ack_message":
        payload = {"agent": args["agent"], "id": args["id"]}
        if args.get("via"):
            payload["via"] = args["via"]
        status, body = _seam("/hub/api/message/ack", payload, auth_headers)
    elif name == "post_update":
        payload = {"agent": args["agent"], "summary": args["summary"]}
        for key in ("kind", "evidence", "item"):
            if args.get(key):
                payload[key] = args[key]
        status, body = _seam("/hub/api/agent-update", payload, auth_headers)
    elif name == "list_components":
        query = {"kind": args["kind"]} if args.get("kind") else {}
        status, body = _seam("/hub/components.json", query, auth_headers, method="get")
    elif name == "register_component":
        payload = dict(args)
        payload.setdefault("maturity", "proven")
        status, body = _seam("/hub/api/capability", payload, auth_headers)
    elif name == "report_ci_failure":
        status, body = _seam("/hub/api/ci-failure", dict(args), auth_headers)
    elif name == "record_deploy":
        payload = dict(args)
        if not payload.get("at"):
            # The record needs `at`; a caller following this tool's schema may omit it.
            import datetime
            payload["at"] = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        status, body = _seam("/hub/api/deploy", payload, auth_headers)
    elif name == "search_board":
        status, body = _seam("/hub/search.json",
                             {"q": args["query"], "limit": int(args.get("limit", 10))},
                             auth_headers, method="get")
    elif name == "seat_distribution":
        status, body = _seam("/hub/distribution.json", {}, auth_headers, method="get")
    elif name == "built_by_person":
        query = {"person": args["person"]} if args.get("person") else {}
        status, body = _seam("/hub/built.json", query, auth_headers, method="get")
    elif name == "ci_events":
        query = {k: args[k] for k in ("pipeline", "job", "project", "limit") if args.get(k)}
        status, body = _seam("/hub/ci-events.json", query, auth_headers, method="get")
    elif name == "record_entity":
        fields = dict(args.get("fields") or {})
        if args.get("agent"):
            fields.setdefault("agent", args["agent"])
        path = "/hub/api/" + args["type"]
        if fields.get("id"):
            _entity, version = _entity_version(fields["id"])
            if version is not None:
                fields["expected_version"] = version
        status, body = _seam(path, fields, auth_headers)
        errs = (body.get("errors") or [{}]) if isinstance(body, dict) else [{}]
        current = next((e.get("current") for e in errs if isinstance(e, dict)
                        and e.get("current") is not None), None)
        if status in (409, 428) and current is not None:
            # One lost optimistic-concurrency race is retried with the version the refusal
            # named; a second miss is a live race and is returned as-is.
            status, body = _seam(path, dict(fields, expected_version=current), auth_headers)
    elif name == "list_collection":
        kind = re.sub(r"[^a-z]", "", str(args.get("type") or "").lower())
        status, body = _seam("/hub/%s.json" % kind, {}, auth_headers, method="get")
    elif name == "read_errors":
        query = {key: str(args[key]) for key in ("app", "include") if args.get(key)}
        status, body = _seam("/hub/errors.json", query, auth_headers, method="get")
    elif name == "share_lesson":
        status, body = _seam("/hub/api/lesson", {k: v for k, v in args.items() if v not in (None, "")},
                             auth_headers)
    elif name == "record_knowledge":
        kind = args.get("kind")
        if kind not in ("finding", "method", "review"):
            status, body = 400, {"errors": [{"code": "bad_kind", "msg": "finding | method | review"}]}
        else:
            payload = {k: v for k, v in args.items() if k != "kind" and v not in (None, "")}
            status, body = _seam("/hub/api/" + kind, payload, auth_headers)
    elif name == "record_gap":
        from hub_core.client_knowledge import gap_text
        payload = {k: v for k, v in args.items() if v not in (None, "") and k != "note"}
        text = gap_text(args.get("note"), args.get("evidence"))
        if text:
            payload["evidence"] = text
        payload.setdefault("status", "open")
        status, body = _seam("/hub/api/gap", payload, auth_headers)
    elif name == "recall_record":
        ref = str(args.get("ref") or "").strip()
        parts = ref.split(":")
        status, body = 404, {}
        if 2 <= len(parts) <= 3 and " " not in ref:
            type_, local = (parts[1], parts[2]) if len(parts) == 3 else (parts[0], parts[1])
            status, body = _seam("/hub/%s/%s.json" % (type_, local), {}, auth_headers, method="get")
        if status >= 400:
            status, body = _seam("/hub/search.json", {"q": ref, "limit": int(args.get("limit", 8))},
                                 auth_headers, method="get")
    elif name == "find_related":
        query = {k: args[k] for k in ("id", "text") if args.get(k)}
        status, body = _seam("/hub/related.json", query, auth_headers, method="get")
    elif name == "list_capabilities":
        query = {k: args[k] for k in ("kind", "q") if args.get(k)}
        status, body = _seam("/hub/capabilities.json", query, auth_headers, method="get")
    elif name == "board_guidance":
        query = {k: args[k] for k in ("agent", "focus", "memory_cap", "memory_full") if args.get(k) is not None}
        status, body = _seam("/hub/guidance.json", query, auth_headers, method="get")
    elif name == "app_feed":
        query = {"app": args["app"]}
        if args.get("name"):
            query["name"] = args["name"]
        status, body = _seam("/hub/app-feed.json", query, auth_headers, method="get")
    elif name == "app_fixes":
        status, body = _seam("/hub/app-fixes.json", {"app": args["app"]}, auth_headers, method="get")
    elif name == "list_hosted_components":
        status, body = _seam("/hub/components/", {}, auth_headers, method="get")
    elif name == "get_component_props":
        status, body = _seam("/hub/components/props/%s.json" % str(args["app"]).strip().lower(),
                             {}, auth_headers, method="get")
    elif name == "set_component_props":
        status, body = _seam("/hub/api/component-props",
                             {"app": args["app"], "props": args["props"]}, auth_headers)
    elif name == "person_profile":
        from urllib.parse import urlencode
        query = {"person": args.get("person") or ""}
        if args.get("app"):
            query["app"] = args["app"]
        path = "/hub/api/profile?" + urlencode(query)
        if isinstance(args.get("prefs"), dict):
            prefs = {"apps": {args["app"]: args["prefs"]}} if args.get("app") else args["prefs"]
            status, body = _seam(path, {"prefs": prefs}, auth_headers)
        else:
            status, body = _seam(path, {}, auth_headers, method="get")
    elif name == "ask_agent":
        payload = {"question": args["question"]}
        if args.get("app"):
            payload["app"] = args["app"]
        status, body = _seam("/hub/api/agent/ask", payload, auth_headers)
    elif name == "retire_record":
        payload = {key: args[key] for key in ("id", "type", "title", "status", "note",
                                              "addressed_by", "superseded_by", "agent")
                   if args.get(key)}
        status, body = _seam("/hub/api/retire", payload, auth_headers)
    elif name == "console_history":
        query = {key: args[key] for key in ("agent", "machine", "session", "limit") if args.get(key)}
        status, body = _seam("/hub/history.json", query, auth_headers, method="get")
    elif name == "record":
        status, body = _record(args, auth_headers)
    elif name == "read_doctrine":
        status, body = _seam("/hub/doctrine.json", {"doc": args.get("doc") or "doctrine"},
                             auth_headers, method="get")
    elif name == "report_app_error":
        payload = {key: args[key] for key in ("app", "message", "kind", "severity", "code",
                                              "details", "component", "operation", "path",
                                              "host") if args.get(key)}
        status, body = _seam("/hub/api/app-error", payload, auth_headers)
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
                    "root": request.headers.get("X-Write-Token", ""),
                    "presence": {name: request.headers.get(name, "")
                                 for name in ("X-Hub-Machine", "X-Hub-Session",
                                              "X-Hub-Cwd", "X-Hub-Focus",
                                              # an unattended caller's depth rides through,
                                              # so what it raises is stamped (hub_core.offer)
                                              "X-Hub-Unattended", "X-Hub-Hop")}}

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
