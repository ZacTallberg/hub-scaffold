# The orchestration method (read first)

How to run any campaign in this directory well — whether you have a multi-agent orchestration tool,
a hand-rolled fan-out, or just one agent working sequentially. This is the engine; the other files
are the payloads.

## 1. Shape: fan-out → verify → close → roll up

```
DISCOVER the work-list (inline: list the files/repos/entities in scope)
  → FAN OUT one worker per item / per expert        (parallel where independent)
    → each worker returns STRUCTURED findings      (never prose to be re-parsed)
  → VERIFY: an adversarial pass re-checks each finding before it is trusted
  → CLOSE: a per-item closer dedupes, writes the durable record, commits ONLY its file
  → ROLL UP: one synthesizer produces the cross-cutting view
```

Default to a **pipeline** (each item flows through all stages independently — no barrier) so a slow
item never blocks a fast one. Use a barrier only when a stage genuinely needs *all* prior results at
once (dedup across the whole set, an early-exit on zero findings).

**If you have only one agent / a tight budget:** run the same shape sequentially, one item at a time,
and **commit each item's record as you finish it** so a stopped run leaves durable partial progress.
Scale the fan-out to the budget — a huge parallel fleet is a cost decision the operator makes, not a
default. When usage is constrained, prefer sequential-with-checkpoints over a large fan-out.

## 2. The adversarial-verify rule (the thing that makes findings trustworthy)

Never record a finding or a "done" on a single agent's assertion. For each candidate:
- Spawn an independent checker **prompted to REFUTE it**, defaulting to "not real" when uncertain.
- For high-stakes or destructive conclusions, use **N=3, accept only if ≥2 confirm**. For a finding
  that can fail in more than one way, give each checker a *different angle* (does-it-reproduce /
  security angle / correctness angle) rather than three identical refuters.
- Mark **CONFIRMED** (the checker re-opened the exact `file:line` and the logic holds) vs **PLAUSIBLE**
  (real-looking but not fully grounded). Discard anything actively refuted; count refutations.
- Look for existing mitigations *before* confirming — half of plausible findings are already handled.

## 3. Structured output, not prose

Every worker returns a typed object (findings[], each with `title, severity, file, line,
failure_scenario, evidence, fix_sketch`). The orchestrator merges data, never re-parses paragraphs.
Force the schema at the tool layer if your harness supports it; otherwise demand strict JSON.

## 4. Persist as you go

Write each item's result to disk (or commit it) the moment it's done. A campaign that only reports at
the end loses everything if it's killed. If a run *is* stopped, the partial results are the salvage —
read them before re-running, and resume only the unfinished items.

## 5. The closer commits — and only its own file

The agent that writes a durable record commits **only that file** with a targeted `git add <file>`.
NEVER `git add -A`, `stash`, or `checkout`: repos routinely hold another session's uncommitted work
that must be read but never staged or disturbed. If a repo is a "separate world" (someone else's, or
holding a live session), write the record *outside* it and don't touch it at all.

## 6. Completeness critic

End a large campaign with one agent asking: *what did we miss?* — a scope not swept, a claim left
unverified, a source unread. What it finds is the next round, not an afterthought.

## 7. Scale to the ask

"Quick check" → a few workers, single-vote verify. "Thoroughly audit / be comprehensive" → a larger
roster, 3-vote adversarial pass, a synthesis stage. When unsure, lean thorough for review/audit work
and brief for spot-checks. Announce any coverage you drop (top-N, sampling, no-retry) — silent
truncation reads as "covered everything" when it isn't.
