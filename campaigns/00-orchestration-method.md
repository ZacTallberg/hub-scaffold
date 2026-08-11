# The orchestration method (read first)

How to run any campaign in this directory well — whether you have a multi-agent orchestration tool,
a hand-rolled fan-out, or just one agent working sequentially. This is the engine; the other files
are the payloads.

## 1. Shape: fan-out → consolidate → boundary-verify when warranted → roll up

```
DISCOVER the work-list (inline: list the files/repos/entities in scope)
  → FAN OUT one worker per item / per expert        (parallel where independent)
    → each worker returns STRUCTURED findings      (never prose to be re-parsed)
  → CONSOLIDATE: dedupe and ground findings in the raw artifacts
  → VERIFY: at a meaningful boundary, one fresh disposable closer tests the risky claims
  → RECORD: the authorized writer persists only the intended artifact
  → ROLL UP: one synthesizer produces the cross-cutting view
```

Default to a **pipeline** (each item flows through all stages independently — no barrier) so a slow
item never blocks a fast one. Use a barrier only when a stage genuinely needs *all* prior results at
once (dedup across the whole set, an early-exit on zero findings).

**If you have only one agent / a tight budget:** run the same shape sequentially, one item at a time,
and **commit each item's record as you finish it** so a stopped run leaves durable partial progress.
Scale the fan-out to the budget — a huge parallel fleet is a cost decision the operator makes, not a
default. When usage is constrained, prefer sequential-with-checkpoints over a large fan-out.

## 2. Proportional independent verification

Do not spawn a checker for every item. Ground routine findings by reopening the cited artifact and
label uncertainty honestly. Invoke `verification-closer.md` once for a coherent batch when it reaches
a release, security/auth, migration/destructive, public-contract, concurrency/process, regression,
or sampled-audit boundary. Prompt that fresh closer to refute the claims and look for existing
mitigations. Escalate to N=3 only for irreversible or genuinely high-stakes conclusions, giving each
verifier a different failure angle. The closer returns one verdict and exits; it does not fix.

## 3. Structured output, not prose

Every worker returns a typed object (findings[], each with `title, severity, file, line,
failure_scenario, evidence, fix_sketch`). The orchestrator merges data, never re-parses paragraphs.
Force the schema at the tool layer if your harness supports it; otherwise demand strict JSON.

## 4. Persist as you go

Write each item's result to disk (or commit it) the moment it's done. A campaign that only reports at
the end loses everything if it's killed. If a run *is* stopped, the partial results are the salvage —
read them before re-running, and resume only the unfinished items.

## 5. The record writer commits only its own file

The authorized agent that writes a durable record commits **only that file** with a targeted `git add <file>`.
NEVER `git add -A`, `stash`, or `checkout`: repos routinely hold another session's uncommitted work
that must be read but never staged or disturbed. If a repo is a "separate world" (someone else's, or
holding a live session), write the record *outside* it and don't touch it at all.

## 6. Completeness critic

End a large campaign with one agent asking: *what did we miss?* — a scope not swept, a claim left
unverified, a source unread. What it finds is the next round, not an afterthought.

## 7. Scale to the ask

"Quick check" → one worker or a focused read. "Thoroughly audit / be comprehensive" → a larger
roster, synthesis, and one boundary closer. Use three independent votes only when consequence
justifies the coordination cost. Announce sampling or omitted coverage—silent truncation reads as
"covered everything" when it was not.
