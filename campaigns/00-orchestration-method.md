# The orchestration method (read first)

How to run any campaign in this directory well — whether you have a multi-agent orchestration tool,
a hand-rolled fan-out, or just one agent working sequentially. This is the engine; the other files
are the payloads.

## 1. Shape: fan out → consolidate → attempt → record → roll up

```
DISCOVER the work-list (inline: list the files/repos/entities in scope)
  → FAN OUT one worker per item / per expert        (parallel where independent)
    → each worker returns STRUCTURED findings      (never prose to be re-parsed)
  → CONSOLIDATE: dedupe and ground findings in the raw artifacts
  → ATTEMPT: use the changed operation on the real target
  → RECORD: the authorized writer persists the intended artifact and receipt
  → ROLL UP: one synthesizer composes child receipts into the cross-cutting view
```

Default to a **pipeline** (each item flows through all stages independently — no barrier) so a slow
item never blocks a fast one. Use a barrier only when a stage genuinely needs all prior results at
once, such as whole-set deduplication.

With one agent or a tight budget, run the same shape sequentially and commit each item's record as it
finishes. Scale fan-out to real independent breadth; more agents are not proof and should not consume
the budget needed to build.

## 2. Proof budget: actual use first

The default proof is the real attempt. If the changed operation works, retain that receipt. If it
breaks, the observed failure is sufficient notice and becomes fresh task input. The delivery worker
records the failure and stops; it does not speculate, silently expand scope, or preemptively turn
itself into a repair worker. The operator may route the task to a dedicated repair/error-fixing lane.

Do not create tests or verification tasks for copy, wording, formatting, color, spacing, ordinary UI
style/animation work, or another non-critical fix. Do not validate page copy. Do not spawn a checker
for each item.

Only a rare critical boundary — security/authorization, destructive data integrity, migration,
public protocol compatibility, or concurrency/fencing — can justify a test. Make it a one-shot probe
in temporary storage, run it only against the named risk, retain the result as a receipt, and delete
the probe before commit. It must never become a permanent test, workflow, or suite.

Child receipts compose upward. A parent or release inherits completed task receipts and does not
rerun their proof. At release, exercise only the new integration seam introduced by composition. A
verification closer cannot invoke another closer or nest a verifier/suite ladder.

**Stop rule:** when the actual changed path succeeds and no critical boundary was crossed, record it
and stop. Do not manufacture another check because one could be written.

## 3. Structured output, not prose

Every worker returns a typed object (`findings[]`, each with `title, severity, file, line,
failure_scenario, evidence, fix_sketch`). The orchestrator merges data, never re-parses paragraphs.
Force the schema at the tool layer if the harness supports it; otherwise demand strict JSON.

## 4. Persist as you go

Write each item's result to disk or commit it the moment it is done. A campaign that only reports at
the end loses everything if it is killed. If a run is stopped, read the durable partial results and
resume only unfinished items.

## 5. The record writer commits only its own file

The authorized agent that writes a durable record commits only that file with a targeted stage.
Never stage everything, stash, or discard: repositories routinely hold another session's work that
must be read but not disturbed. If a repository is a separate world, write the record outside it.

## 6. Completeness critic

For a large discovery campaign, one final critic may ask what scope was missed or what claim is still
unsupported. Its findings become normal board tasks. It is not a verifier, does not rerun completed
work, and cannot spawn another critic.

## 7. Scale to the ask

“Quick check” means one worker or a focused read. “Thoroughly audit” can justify a broader roster and
synthesis. Use independent review only for a named critical boundary, never as a default finishing
ceremony. Announce sampling or omitted coverage so the record remains honest.
