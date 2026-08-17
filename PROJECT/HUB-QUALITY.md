# HUB QUALITY — the construction contract

> canonical contract · owner: project operator · update: whenever the Hub's product, truth, flow, or coordination bar changes

This is the minimum and aspirational bar for every Hub surface. A Hub should be phenomenally useful,
visually unmistakable, alive with truthful realtime feedback, and tuned for extraordinary task
throughput. Quality is established by using the real thing. Permanent tests, copy assertions, and
ceremonial verifier ladders are not substitutes for an authored product.

## 1. Product and visual excellence

A Hub must have a distinctive project identity, deliberate hierarchy, strong spatial rhythm, clear
information density, and coherent depth, color, type, motion, and interaction. It must feel authored
for its project rather than like an interchangeable admin template. Delight is welcome when it makes
state, causality, or attention easier to understand; decoration must never compete with truth.

Every viewport has one dominant above-the-fold operational decision. A metric appears only once
above the fold. Each visual region has at most one persistent animated signal; all other motion is
caused by meaningful state change. Every generated Hub declares a project-specific mark, accent
pairing, display voice, surface character, and optional visual motif before visual construction.

When constructing a new Hub or making a material redesign, use the rendered product at its empty,
ordinary, dense, loading, live, degraded, and error states. Ask: where does the eye land, what needs
action, what changed, what is trustworthy, and can the next useful action be taken without hunting?
This is product work, not a demand for a permanent visual test suite.

## 2. Required invariants

### 2.1 Truth-derived UI

- The canonical snapshot/JSON island is the source of rendered assertions. DOM labels, totals,
  progress, delivery state, and animation derive from it; markup is not a second ledger.
- `done`, `landed`, `deployed`, and `live` remain separate claims with separate evidence. Unknown or
  unavailable evidence renders **unmeasured**, never zero, success, or false green.
- Every metric names its denominator, window, and freshness. Empty denominators remain unmeasured.

### 2.2 Realtime truth

- Realtime starts with a complete snapshot and monotonic cursor. Every canonical mutation publishes
  once after commit into a persistent push stream; the connected client reconciles immediately to the
  highest announced cursor. Normal operation has no interval polling and no manual sync control.
- The UI names one transport truth: **Connected** or **Disconnected**. A disconnect never masquerades
  as freshness; reconnect performs one ordered, deduplicated cursor catch-up and then returns to push.
  Recovery reads are recovery only, not an alternate steady-state synchronization loop.
- Serve long-lived streams through ASGI and a shared pub/sub source wherever multiple server processes
  can write. An in-process signal bus is valid only for an explicitly single-process reference Hub.
- The live mutation path does not replay avoidable history, spawn Git, or rerun repository audit.
  Maintain a cursor-keyed materialized fold; compute heavyweight integrity views outside delivery.
- Heartbeats, replays, and no-op deltas do not animate as work. Motion follows a real state transition.

### 2.3 Accessible, responsive interaction

- Target WCAG 2.2 AA. Preserve content and function at 320 CSS px without two-dimensional scrolling
  except where the content intrinsically requires it; never encode meaning by color alone.
- Every action is keyboard-operable with visible focus. Tabs use `tablist`/`tab`/`tabpanel`, one
  selected tab, roving focus, arrow navigation, Home/End, and Enter/Space where activation is manual.
- Modal dialogs put focus inside, contain Tab/Shift+Tab, close on Escape, make the background inert,
  have an accessible name, and restore focus to the invoker.
- Status changes are announced without stealing focus. Reduced-motion and forced-color modes retain
  equivalent meaning and functionality.

### 2.4 Motion grammar and layout stability

Animation communicates entry, transition, dependency, progress, or attention. It is interruptible,
does not endlessly celebrate ordinary activity, does not cause layout shift, and has a meaningful
reduced-motion form. Live patches preserve reading position and focus.

### 2.5 Performance and dependency floor

At the 75th percentile of field visits, target LCP <= 2.5 s, INP <= 200 ms, and CLS <= 0.1 on mobile
and desktop. Label field and lab evidence honestly; a lab run cannot establish a field pass. Record
project-specific bundle, request, and realtime budgets. The base Hub has no runtime CDN dependency;
an adopter exception requires an ADR, failure behavior, and offline/degraded proof.

### 2.6 Flow that improves throughput

Expose work in progress (started, not finished), throughput (finished per stated unit of time), work
item age, cycle-time distribution, and a service-level expectation expressed as period plus
probability. Also expose readiness, stalled/expired work, the dependency frontier, and arrival versus
departure pressure where measurable. Missing data is **unmeasured**. Do not turn raw worker counts or
ticket volume into a leaderboard; optimize finished value and bottleneck removal, not activity theater.

### 2.7 Durable agent coordination

Use one durable task lifecycle with atomic leases/fencing, idempotent mutation, heartbeats, explicit
ownership, resumable plans, and attached evidence. Bound work in progress by the ready frontier;
parallelize only independent work and return structured results. Carry task, agent, lease, and trace
correlation across boundaries. Advertise MCP, A2A, streaming, or other capabilities only when the
callable transport and behavior actually exist.

## 3. Proof without test accumulation

The default proof is the actual operation: make the change, exercise the changed path on the real
surface, and observe whether it works. If it breaks, that observed failure is the notice and becomes
fresh board input. The delivery agent records it without speculative repair or silently changing
roles; the operator may later route it to a dedicated repair/error-fixing lane.

- Do not create or run a test for copy, wording, spacing, color, ordinary style or animation tuning,
  or another non-critical narrow fix. Do not validate page copy with assertions, snapshots, pixel
  comparisons, screenshots-as-gates, or a second agent. Implement it on the page and move on.
- A test is justified only for a rare critical boundary such as security or authorization, destructive
  data integrity, a migration, public protocol compatibility, or concurrency/fencing. That test must
  be a one-shot transient probe in temporary storage, run only for the named risk, and deleted before
  commit. Retain its result as the task receipt; never retain the test artifact.
- A completed child task's receipt composes into its parent. Parents and releases inherit those
  receipts rather than rerunning child proof. A release may probe only the genuinely new integration
  seam created by composing the children.
- Never nest verifiers. A closer may not dispatch another closer, suite, or proof ladder. One boundary,
  one smallest decisive operation, one receipt, then it exits.

For a new Hub or material redesign, the following is a design-coverage guide, not a standing suite or
a requirement for every edit:

| Dimension | States to use when materially affected |
|---|---|
| Width | 320, 768, and 1440 CSS px |
| Theme | light and dark, where both are supported |
| User preference | normal motion, reduced motion, forced colors |
| Input | keyboard and pointer paths |
| Data/transport | empty, ordinary, dense, live update, degraded, and error |

**Stop rule:** once the real changed behavior succeeds and no critical boundary was crossed, record
the work and stop. Do not add a check, test, screenshot ritual, independent verifier, or release rerun
merely to make simple work look more proven.

## 4. Elevation workflow

1. Research current primary standards and the project's audience and visual identity.
2. Audit rendered states and live behavior; record concrete product defects.
3. Write a design brief naming hierarchy, tokens, motion grammar, state semantics, and budgets.
4. Implement from canonical data through the renderer, preserving local identity.
5. Use the affected real paths. Keep their receipts; create a transient probe only for a critical
   boundary, and remove that probe before commit.
6. Curate generally useful improvements back into `hub-scaffold`; never bulk-merge an instance.

Use `campaigns/elevate-hub.md` for the executable campaign. Exceptions belong in an ADR with owner,
expiry/revisit trigger, user impact, and evidence. Upgrades preserve project identity and local theme,
diff this contract explicitly, and upsert generic units without imposing redundant proof work.

## 5. Primary standards

- [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
- [WAI-ARIA tabs](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/)
- [WAI-ARIA modal dialogs](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)
- [Core Web Vitals thresholds](https://web.dev/articles/defining-core-web-vitals-thresholds)
- [WHATWG server-sent events](https://html.spec.whatwg.org/dev/server-sent-events.html)
- [The Kanban Guide](https://kanbanguides.org/the-kanban-guide/)
- [DORA metrics](https://dora.dev/guides/dora-metrics/)
- [OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- [A2A specification](https://a2a-protocol.org/dev/specification/)
- [OpenTelemetry overview](https://opentelemetry.io/docs/specs/otel/overview/)
