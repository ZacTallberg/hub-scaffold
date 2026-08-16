# Hub frontend contract

The renderer implements [`PROJECT/HUB-QUALITY.md`](../../PROJECT/HUB-QUALITY.md). The server-provided
snapshot/JSON island is canonical; HTML is a progressively enhanced projection, and live event
identity only prompts an ordered delta or snapshot read. Keep DOM patching idempotent, preserve
focus and scroll position, and never animate a heartbeat, replay, or no-op.

Semantic tokens—not one-off colors and dimensions—carry project identity, hierarchy, spacing,
surface depth, state, and motion. Preserve the zero-CDN runtime floor unless an ADR records the
exception. Tabs and dialogs follow the keyboard/focus contracts in the canonical document; status
changes use appropriate live regions. Reduced motion, forced colors, narrow reflow, transport
degradation, and false-green delivery states are first-class renderer states, not cleanup work.

`done` means the real operation completed. Do not weaken that success because a task correctly has
no `verification_run`: a receipt is expected only when the task explicitly declared a rare,
transient critical-boundary `verification_command`. Keep those exceptional receipts detailed and
actionable without turning them into a universal completion gate.

Judge copy, style, motion, hierarchy, rhythm, identity, density, and delight on the real rendered
surface—never through permanent copy snapshots or non-critical tests. If a structural critical
boundary truly needs a probe, make it transient, run it once, retain only its receipt, and remove
the probe before commit.
