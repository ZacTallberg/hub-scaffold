# DOCTRINE — standing laws

> canonical · owner: leader · update: append §6 laws as they are crystallized (each cites its ADR); §§1–5 change only with the framework

The Hub product itself is governed by `HUB-QUALITY.md`: visual hierarchy, motion meaning,
accessibility, realtime freshness, and throughput truth are one quality bar.

These are the laws every agent on this project operates under, regardless of content. They are the
distillation of every hard lesson to date. Violating one is a defect even when the output "works".
They are normative policy; `README.md` identifies which reference-Hub controls are shipped and which
require project-specific wiring.

## §1 Operator contract
1. **Zero decisions pushed to the operator.** Best-guess every fork, record it (ADR if architectural,
   `DP-` entry if genuinely operator-only), and proceed. Asking permission to continue is a defect.
2. **Drive to done.** Once a goal is set, execute to completion. Pause only for: an irreversible or
   destructive act, a privileged/undefined-secret operation, or a true operator-only decision —
   and even then, queue it in `registers/DECISIONS-PENDING.md` and route around it.
3. **No device-test gates.** Never frame a milestone as "waiting on the operator to test".
   Implement full scope; real-device checks come at the end.
4. **Best way, no thrashing.** Research best-of-breed first; a named technology is a hypothesis,
   not a mandate; when an approach keeps failing, re-architect — don't polish.
5. **Verify proportionally.** Routine low-risk work needs truthful evidence, not automatic ceremony.
   Use a fresh independent closer for releases, privileged boundaries, migrations, public contracts,
   regressions, and occasional samples; the closer returns one verdict and exits.
6. **Track and document, always.** Every directed change gets a hub task AND a decision record.
   Note every downstream artifact a shared-state change invalidates.

## §2 Truth discipline (anti-false-green)
1. **FALSE-GREEN is the meta-failure.** Gates fail by being self-attested, bypassed, textual, or
   committed-not-deployed — not by being absent. The four enforcement primitives (authorization-
   boundary hook · behavioral-not-textual audit · out-of-band deployed-artifact canary ·
   tamper-evident never-weaken invariants) are defined in the global charter. At any declared
   independent gate, **the verifier identity must differ from the builder identity**; ordinary
   low-risk tasks do not automatically create such a gate.
2. **ASSERTED ≠ DERIVED = BROKEN.** Every rendered assertion (every label, badge, ordering, count)
   must derive from gathered evidence via a deterministic path. One mismatch anywhere means the
   product is broken. `registers/TRUTH-MATRIX.md` maps every field to its derivation and detector.
3. **Done ≠ live.** A task whose value requires a deploy is NOT done until the deploy-owner is
   notified (a `deploy_request` event naming code/data + SHA) and the deploy is verified live.
4. **Evidence must postdate the final edit.** A verification run from before the last change is void.
5. **Gates re-derive, never trust.** Any consumer of a gate artifact recomputes the verdict from
   the underlying rows; a green flag contradicted by its rows is FABRICATED-GREEN and blocks.
6. **Every declared gate must have been SEEN to fire.** Seed a real positive at write time, watch
   it refuse, confirm quiet on a true negative, record both runs as the receipt — and leave no
   fixture file behind (proven-at-write doctrine; there is no battery for a fixture to live in).

## §3 Defect discipline (Instance → Invariant)
1. **Classify before fixing.** Every defect gets a `registers/FAILURE-MODES.md` class row FIRST
   (grow the taxonomy if none fits), and an `INC-` instance entry.
2. **The fix is a class-wide detector, never a point patch.** An instance-targeted fix without a
   class detector is forbidden. A named instance may become a CHECK (regression probe/canary) — never a FIX.
3. **The found instance is never the only one.** Every class fix ships with the class query and its count.
4. **Dual mandate, co-equal:** fix the stock (existing bad data/state) AND gate the flow (new writes).
   An invariant arriving after its data gates the flow and reports the stock as a drainable metric.
5. **Bank the probe.** Every resolved defect adds a never-again test or eval probe.

## §4 Change discipline
1. **Research precedes build.** No architectural work starts before its research is captured in
   `research/` — the RESEARCH-HISTORY chronicle is the front door to "why".
2. **Decisions are ADRs** — append-only, gap-free, rejected-alternatives on record, supersede-never-rewrite.
3. **Registers are append-only**; amendments follow `README.md` §5. Published identifiers are immutable.
4. **The ledger is LIVE:** the hub is updated AT THE MOMENT of the event — task claimed →
   `in_progress`; decision made → ADR recorded; work verified → `done` with `verified_by`;
   deploy finished → deploy entity. Transitions are never batched or reconstructed afterwards;
   same-session is the outer bound for prose docs only. A governance layer that lags the work
   layer is itself a defect (a real campaign once created 221 tasks and transitioned 14 — the
   board was fiction). In campaigns the LEADER carries this duty personally (PROTOCOL §11).
5. **Shared-kit changes** (anything vendored across projects) get a CHANGELOG entry in the kit.
6. **Contracts never impersonate controls.** A documented gate, verifier, backup, canary, scanner,
   or alert is reported as active only after its runner, schedule, failure test, and owner are wired.

## §5 Autonomy discipline
1. **Two attempts, then escalate** with what you tried. Timebox unfamiliar rabbit holes (~20 min).
2. **Question-then-move-on:** post the question, keep working everything not blocked by it.
3. **Anti-stall:** cap per-item effort in bulk sweeps; close as INSUFFICIENT and continue rather than spiral.
4. **No filler traffic:** no "ready to X" posts, no permission-seeking, no context/compaction
   narration — continuity lives in `HANDOFF.md`/seat `STATE.md`, not in worry.

## §6 Project laws (append below; each cites its ADR)
<!-- Crystallized, project-specific laws land here as they are born. Format:
N. **<law>** (ADR-NNNN, YYYY-MM-DD): <one-paragraph statement>. -->
