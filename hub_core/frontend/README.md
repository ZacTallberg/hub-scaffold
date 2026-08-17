# Hub frontend contract

The renderer implements [`PROJECT/HUB-QUALITY.md`](../../PROJECT/HUB-QUALITY.md). The server-provided
snapshot/JSON island is canonical; HTML is a progressively enhanced projection, and the live event
stream carries ordered canonical patches directly into the renderer. The server coalesces a burst
to its highest folded cursor, and the client applies it immediately without an HTTP round trip or
secondary cadence. A delta catch-up or full snapshot read is recovery after reconnect or
a detected gap, never normal operation and never a timer.
There is no manual sync affordance because a connected Hub is already current. Keep DOM patching
idempotent, keep an open entity dialog live, preserve focus and scroll position, and never animate
a heartbeat, replay, or no-op. Connection status says exactly `Connected` or `Disconnected`.

Semantic tokens—not one-off colors and dimensions—carry project identity, hierarchy, spacing,
surface depth, state, and motion. Preserve the zero-CDN runtime floor unless an ADR records the
exception. Tabs and dialogs follow the keyboard/focus contracts in the canonical document; status
changes use appropriate live regions. Reduced motion, forced colors, narrow reflow, transport
degradation, and false-green delivery states are first-class renderer states, not cleanup work.

`PROJECT/project.json.visual` is the bounded authored layer: `mark`, `accent_h`,
`accent_pair_h`, `display_voice`, `surface`, and optional `motif`. The shell consumes those tokens;
adopters never fork component CSS to gain character. Accent pairing may tint navigation, surfaces,
and the ambient field, but the five semantic status hues are invariant. A motif is a resting-field
geometry only—its intensity and motion still answer canonical fleet and failure state.

`done` means the real operation completed. Do not weaken that success because a task correctly has
no `verification_run`: a receipt is expected only when the task explicitly declared a rare,
transient critical-boundary `verification_command`. Keep those exceptional receipts detailed and
actionable without turning them into a universal completion gate.

Judge copy, style, motion, hierarchy, rhythm, identity, density, and delight on the real rendered
surface—never through permanent copy snapshots or non-critical tests. If a structural critical
boundary truly needs a probe, make it transient, run it once, retain only its receipt, and remove
the probe before commit.
