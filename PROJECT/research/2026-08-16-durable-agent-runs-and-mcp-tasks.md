# Durable agent runs and current MCP Tasks

> 2026-08-16 · task `example:task:0014` · decision input for ADR-0005

## Question

What is the smallest production-grade execution record that survives worker loss, supports safe
handoff/cancellation/resume, and maps truthfully onto the MCP 2026-07-28 Tasks extension without
claiming an A2A transport that does not exist?

## Primary sources

- [MCP Tasks extension](https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks)
  defines server-directed task creation for `tools/call`, durable task handles, `tasks/get`,
  `tasks/update`, `tasks/cancel`, status-specific payloads, `resultType`, timestamps, TTL and polling
  metadata, per-request extension negotiation, and `Mcp-Name` routing. Its polling interval is an
  optional interoperability hint, so the Hub omits it: committed-event push remains the sole
  freshness path for coordination and the UI.
- [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
  moves Tasks out of experimental core into `io.modelcontextprotocol/tasks` and introduces
  `tasks/update` under the stateless, header-routed protocol core.
- [A2A v1.0 specification](https://a2a-protocol.org/latest/specification/)
  requires supported interfaces and optional streaming/push capabilities to correspond to callable
  protocol operations; task-shaped local state alone is not an A2A transport.

## Findings

⚑ **A board task and an execution attempt are different durable nouns.** The task defines owed
work; an AgentRun records one resumable attempt, including commands, messages, checkpoints,
handoffs, cancellation, input requests/responses, and outcome.

⚑ **MCP Tasks are created by a task-augmented request, not by relabeling the backlog.** A server may
return `resultType: "task"` only to a declaring client after the task handle is durable. Subsequent
`tasks/get`, `tasks/update`, and `tasks/cancel` return `resultType: "complete"` and status-specific
fields on the top-level result.

⚑ **Resume needs an explicit checkpoint and an immutable receipt chain.** A replacement worker must
receive the latest checkpoint, completed step IDs, and accepted child outcomes so it never replays
already completed work merely to regain context.

⚑ **Cancellation is cooperative.** MCP cancellation acknowledges intent; the run records
`cancel_requested` until the worker checkpoints and acknowledges cancellation or finishes first.

⚑ **A2A discovery remains empty.** AgentRun/Message/Handoff nouns do not become A2A support until a
real SendMessage/GetTask/CancelTask transport exists at an advertised interface.

⚑ **MCP task notifications are optional and transport-backed.** This stateless view exposes the
real `tasks/get/update/cancel` controls but no `subscriptions/listen` stream, so it does not
advertise task notifications. Hub SSE already ships each committed canonical patch immediately;
that is the live coordination rail, not a periodic MCP read loop.

## Chosen design

Add one first-class `run` aggregate. It embeds bounded, typed Command, Message, Checkpoint, and
Handoff records so each lifecycle mutation is a canonical event and the folded aggregate is a
complete recovery envelope. A run references its board task, parent run, immutable owner subject,
trace ID, attempt, current status, input requests/responses, result/error, and composed receipts.

The run writer requires scoped authentication plus the current fenced task lease. Handoff records
the target and latest checkpoint; resume requires the target/new owner to hold the current task
lease. Completion composes already-completed child-run receipts into the parent without replaying
their proof.

MCP exposes explicit run tools. `create_run` may return a compliant CreateTaskResult only when that
individual `tools/call` declares the Tasks extension; otherwise it returns a normal tool result.
Task extension methods operate only on durable run handles, require per-request capability metadata
and matching `Mcp-Name`, and return the current official shapes.

## Rejected alternatives

- **Map every backlog task directly to MCP Task:** rejected because those handles were not created
  by task-augmented requests and carry no durable execution result/input state.
- **One schema/entity per Command, Message, Handoff, and checkpoint:** rejected for the minimal base;
  atomic run updates need one recovery envelope and one OCC version, while typed embedded records
  retain every noun and can be split later without changing their IDs.
- **In-memory run registry:** rejected because process loss is exactly the boundary this task closes.
- **Advertise A2A now:** rejected because no callable A2A interface exists.
