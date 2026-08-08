# Pattern — a worker that keeps COMPLETING TASKS

**The green condition is not "the worker is running". It is "the worker is still completing
tasks."** Those are easy to conflate and the conflation is total: a seat can hold a claim, renew it
forever, re-read its instructions every cycle and finish nothing, while a pid check, a heartbeat
file, an open window and a fresh log line all report health for as long as it does.
**`is-active` is not `is-working`.**

On the board this pattern was extracted from, one seat converted 43 of 45 claims (96%) while two
others converted 31 of 153 (20%) and 17 of 97 (18%). All three were alive and claiming throughout.
A process check called all three healthy.

The only evidence that counts is a `done` transition on the ledger, which costs a receipt through
the write gate and therefore cannot be self-reported into existence.

## The nine ways a worker stops completing

Most designs defend only the first two, which is why most fleets quietly stop.

| # | It stops because | What holds it open |
|---|---|---|
| 1 | the agent's turn ends | the wrapper LOOPS — one invocation is not a lifetime |
| 2 | the process exits after that call | the loop IS the seat's lifetime; no terminal condition |
| 3 | context fills and quality degrades | a FRESH agent session per cycle — context is disposable, the board is the memory |
| 4 | the host dies or the seat is killed | peers restore the fleet; a supervisor is one more thing that can die |
| 5 | quota burned by spinning on failure | exponential backoff, capped — survive the outage, never exit |
| 6 | it blocks on a prompt or a lock | non-interactive agent mode, claim TTLs, hard timeouts |
| 7 | its claim or credential lapses | re-establish each cycle; reap stale claims |
| 8 | the queue is empty | an empty queue is a REFILL signal, not a finish line |
| 9 | **work exists and it finishes none** | **measure completions and CHANGE BEHAVIOUR** |

Nine is the one that matters, because the worker looks perfect while it happens.

## The barren ladder

`tools/seat_productivity.py --agent <id> --done-count` reads the ledger. The wrapper samples it
either side of every cycle; unchanged means the cycle produced nothing. The worker cannot remember
this itself — its context is fresh by design — so the wrapper passes the count in
(`HUB_BARREN_CYCLES`) and the instructions tell it what to do with it.

**Escalate, never repeat.** A worker merely told to try again will try the same thing again.

1. **One barren cycle** — re-read the claim. If a step is genuinely progressing, record it so the
   board can see movement; an unrecorded step is indistinguishable from a frozen worker.
2. **Two** — RELEASE the claim with a concrete handoff. Not giving up: a worker with fresh context
   often finishes in one cycle what this one could not finish in three, and a held claim starves
   every other worker through the concurrency ceiling. **Releasing is progress.**
3. **Three or more** — the blocker is probably the environment, not the task. Record a finding
   naming the evidence (what ran, what it produced, the exact error), then take work from a
   different source. **An honest finding is a completion.**

A worker never decides it is finished. It cannot see the fleet, and "everything is done" is a
fleet-level judgement made with fleet-level information. The operator ends a run.

## Reading the numbers honestly

- **Conversion below ~25% over a dozen-plus claims** is thrashing; over two or three it is noise.
- **Staleness is measured from the last COMPLETION**, never the last log line — a chatty worker is
  not a working worker.
- **Liveness is a PID, never a clock.** A worker inside a long cycle is alive however old its
  stamp; a fresh stamp whose process is gone is dead. Reaping on age kills workers for thinking.
- **An empty ledger is UNKNOWN, not healthy.** No workers and no completions is a fresh board;
  live claims and no completions is a sick one. The difference is whether anyone holds a claim.

## Verifying it

Reading `while` in your wrapper proves nothing. Run the generated child with the agent command
replaced by a stub and watch the **cycle counter advance** across distinct sessions, survive a
non-zero cycle, and never exit. A worker printing "cycle 1" forever is restarting while wearing a
loop's clothes — and that is a real observed failure, not a hypothetical.

Adapters: `adapters/windows/launch-worker.ps1` implements this. Any wrapper on any platform can:
the contract is loop, fresh session, measure completions from the ledger, escalate when barren,
back off on failure, and never exit on your own.
