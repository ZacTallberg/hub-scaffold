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
   Implement full scope; when a feature inherently requires a device, use it through the real
   operation and record the outcome.
4. **Best way, no thrashing.** Research best-of-breed first; a named technology is a hypothesis,
   not a mandate; when an approach keeps failing, re-architect — don't polish.
5. **Finish first; prove only what is at risk.** The successful real operation is the default proof.
   Copy, wording, style, animation polish, and other non-critical changes get no test or validation
   ritual. Permanent tests, fixtures, verifier scripts, and always-on verification workflows are
   forbidden. A security, destructive-data, migration, protocol, or concurrency boundary may earn
   one temporary probe: create it outside the durable product tree, run it once, retain its receipt,
   and delete it before commit. Completed receipts compose upward; a release checks only a newly
   created critical integration seam and never reruns accepted child work.
6. **Track and document, always.** Every directed change gets a hub task AND a decision record.
   Note every downstream artifact a shared-state change invalidates.

## §2 Truth discipline (anti-false-green)
1. **FALSE-GREEN is the meta-failure at a declared critical boundary.** A boundary receipt must
   describe what actually ran and what happened. When independent proof is explicitly warranted,
   **the verifier identity must differ from the builder identity**. Ordinary work creates no gate,
   standing verifier, or scheduled proof burden.
2. **ASSERTED ≠ DERIVED = BROKEN.** Machine-derived factual claims such as status, ordering, and
   counts must trace to their source of truth. Editorial copy, visual style, and motion are not
   verification targets. `registers/TRUTH-MATRIX.md` records only factual derivations that matter.
3. **Done ≠ live.** A task whose value requires a deploy is NOT done until the deploy-owner is
   notified (a `deploy_request` event naming code/data + SHA) and the real deploy outcome is
   observed live.
4. **Evidence is the completed operation.** Record the attempted action and observed result after
   the final edit. If a rare critical probe is used, its durable receipt must postdate the edit;
   the probe itself must not survive the commit.
5. **Receipts compose.** Consumers inherit accepted dependency receipts. They do not replay them;
   a release examines only a new critical integration seam introduced by composition.
6. **Stop when the changed behavior works.** Once the real operation succeeds and no critical
   boundary remains unobserved, completion is earned. Adding another check is process bloat.
7. **Migrations label history; they never counterfeit it.** When a stricter receipt contract arrives,
   preserve immutable pre-contract facts behind an exact ledger sequence + hash cutoff and account
   for their debt explicitly. Never synthesize `verified_by`, evidence, or a passing receipt for work
   that predates the contract. New writes remain strict from the adoption boundary forward.

## §3 Defect discipline (Instance → Invariant)
1. **Observed failure becomes work.** Record the concrete failure as an `INC-` instance and open a
   fresh repair task; classify it in `registers/FAILURE-MODES.md` when the class is useful for routing.
2. **Restore the real operation.** Fix the causal path and retry the action that failed. The
   successful retry is the ordinary completion receipt.
3. **Do not bank tests.** A failure does not automatically create a regression suite, fixture, or
   permanent checker. A rare critical recurring boundary may use a one-shot temporary diagnostic
   probe under §1.5, deleted before commit.
4. **Repair can be its own lane.** Projects may route observed failures to a dedicated error-fixing
   agent so delivery agents keep completing planned work; the Hub keeps both lanes visible.
5. **Stop after recovery.** Once the failed operation succeeds, close the repair task and return
   throughput to the delivery queue.

## §4 Change discipline
1. **Research precedes build.** No architectural work starts before its research is captured in
   `research/` — the RESEARCH-HISTORY chronicle is the front door to "why".
2. **Decisions are ADRs** — append-only, gap-free, rejected-alternatives on record, supersede-never-rewrite.
3. **Registers are append-only**; amendments follow `README.md` §5. Published identifiers are immutable.
4. **The ledger is LIVE:** the hub is updated AT THE MOMENT of the event — task claimed →
   `in_progress`; decision made → ADR recorded; real operation completed → `done` with `verified_by`;
   deploy finished → deploy entity. Transitions are never batched or reconstructed afterwards;
   same-session is the outer bound for prose docs only. A governance layer that lags the work
   layer is itself a defect (a real campaign once created 221 tasks and transitioned 14 — the
   board was fiction). In campaigns the LEADER carries this duty personally (PROTOCOL §11).
   While the Hub is serving, every mutation crosses its authenticated HTTP write seam; direct
   `EventStore`, JSONL, or SQLite writes are offline recovery operations only. A side-process append
   that bypasses realtime publication makes “Connected” untrue and is therefore a product defect.
5. **Shared-kit changes** (anything vendored across projects) get a CHANGELOG entry in the kit.
6. **Contracts never impersonate controls.** A documented gate, verifier, backup, canary, scanner,
   or alert is reported as active only while its real critical boundary, trigger, and owner exist.
   Documentation never creates a standing test obligation.

## §5 Autonomy discipline
1. **Two attempts, then escalate** with what you tried. Timebox unfamiliar rabbit holes (~20 min).
2. **Question-then-move-on:** post the question, keep working everything not blocked by it.
3. **Anti-stall:** cap per-item effort in bulk sweeps; close as INSUFFICIENT and continue rather than spiral.
4. **No filler traffic:** no "ready to X" posts, no permission-seeking, no context/compaction
   narration — continuity lives in `HANDOFF.md`/seat `STATE.md`, not in worry.

## §6 Project laws (append below; each cites its ADR)
<!-- Crystallized, project-specific laws land here as they are born. Format:
N. **<law>** (ADR-NNNN, YYYY-MM-DD): <one-paragraph statement>. -->
