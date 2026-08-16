# HUB QUALITY — the construction contract

> canonical contract · owner: project operator · update: whenever the Hub's product, truth, flow, or coordination bar changes

This is the minimum bar for every Hub surface. It is both enforceable and aspirational: automation
may verify that the contract is present and that objective invariants have evidence, but visual taste
requires a rendered-state design review. A linter, passing API test, or screenshot alone cannot certify
that a Hub is beautiful, legible, alive, and useful.

## 1. Product and visual excellence

A Hub must have a distinctive project identity, deliberate hierarchy, strong spatial rhythm, clear
information density, and coherent depth, color, type, motion, and interaction. It must feel authored
for its project rather than like an interchangeable admin template. Delight is welcome when it makes
state, causality, or attention easier to understand; decoration must never compete with truth.

Review the rendered product at its empty, ordinary, dense, loading, live, degraded, and error states.
Ask: where does the eye land, what needs action, what changed, what is trustworthy, and can the next
useful action be taken without hunting?

## 2. Required invariants

### 2.1 Truth-derived UI

- The canonical snapshot/JSON island is the source of rendered assertions. DOM labels, totals,
  progress, delivery state, and animation derive from it; markup is not a second ledger.
- `done`, `landed`, `deployed`, and `live` remain separate claims with separate evidence. Unknown or
  unavailable evidence renders **unmeasured**, never zero, success, or false green.
- Every metric names its denominator, window, and freshness. Empty denominators remain unmeasured.

### 2.2 Realtime truth

- Realtime starts with a complete snapshot and monotonic cursor. SSE carries event identity, honors
  `Last-Event-ID`, and triggers an exact delta/snapshot read; reconnects are ordered and deduplicated.
- The UI names its current mode: **live**, **degraded polling**, or **manual refresh**. Silence is not
  proof of freshness. A local reference integration should visibly converge within two seconds unless
  the project records another SLO.
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

## 3. Proof matrix

Before calling a Hub-facing change complete, retain evidence for the affected cells:

| Dimension | Required states |
|---|---|
| Width | 320, 768, and 1440 CSS px |
| Theme | light and dark, where both are supported |
| User preference | normal motion, reduced motion, forced colors |
| Input | keyboard-only path plus pointer path |
| Data/transport | empty, ordinary, dense, live update, degraded, and error |

Use screenshots or short recordings for visual/interaction claims and receipts for state, transport,
accessibility, and performance claims. Reviewers must inspect hierarchy, rhythm, density, identity,
motion meaning, focus continuity, and false-green risk—not merely compare pixels.

## 4. Elevation workflow

1. Research current primary standards and the project's audience and visual identity.
2. Audit rendered states and live behavior; record the concrete defects.
3. Write a design brief that names hierarchy, tokens, motion grammar, state semantics, and budgets.
4. Implement from canonical data through the renderer, preserving local identity.
5. Prove the affected matrix, realtime convergence, flow semantics, and agent lifecycle.
6. Curate generally useful improvements back into `hub-scaffold`; never bulk-merge an instance.

Use `campaigns/elevate-hub.md` for the executable campaign. Exceptions belong in an ADR with owner,
expiry/revisit trigger, user impact, and evidence. Upgrades preserve project identity and local theme,
diff this contract explicitly, upsert generic units, and re-run the matrix.

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

