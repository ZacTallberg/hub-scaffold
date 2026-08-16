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

Any change to this directory is reviewed against the contract's 320/768/1440 rendered-state proof
matrix. Automated checks establish wiring and objective invariants; a human design review decides
whether hierarchy, rhythm, identity, density, and delight meet the bar.

