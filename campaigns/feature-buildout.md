# BUILD — drive real work off the board (the hub loop + roles)

When work changes the Hub itself, execute `elevate-hub.md` against `PROJECT/HUB-QUALITY.md` inside
the same claimed task.

Turn tasks into shipped, proven work. This is the day-to-day execution loop, plus the leader / worker /
verifier roles for long multi-session campaigns where state can't be held in one context.

Read `00-orchestration-method.md` first. Substitute `{{DEPLOY_CMD}}`, `{{LIVE_URL}}`.

---

## The loop every unit of work runs

**DISCOVER → CLAIM → IMPLEMENT → RECORD → VERIFY.** Give a working agent this contract:

> Work ONLY off the hub board. For each unit of work:
> 1. **DISCOVER** — read the top *unblocked, unclaimed* task (ranked by dependency + priority). Don't
>    pick by gut; take the top of the queue.
> 2. **CLAIM** — mark it in-progress (acquire the lease) BEFORE touching anything. Exactly one task in
>    progress at a time.
> 3. **IMPLEMENT** — do ONLY that task. If you discover new work mid-task, add it as a NEW task first —
>    never silently expand scope. Find the root cause before fixing; no speculative patches.
> 4. **RECORD** — mark it `done` WITH evidence (a commit SHA, a passing check, a live URL, a screenshot).
>    In `strict` mode the server won't grant `done` without dereferencing evidence and a typed
>    exit-0 receipt for the task's `verification_command`. Run it yourself; the Hub never does.
> 5. **VERIFY proportionally** — the Hub re-runs its cheap audit. Run focused checks when the change
>    has a plausible failure mode. Dispatch a fresh independent closer only for a release/risky
>    boundary or an occasional sample; do not attach the full selftest ladder to every small task,
>    and never offer a suite runner as a task's proof — the write API refuses it.
> Nothing off-list: a bug, a missing step, a new idea all become tasks on the board FIRST, then get
> claimed. Decisions are ADRs (append-only). Never delegate your one task to a lone sub-agent — do your
> own work; fan out only for genuine parallelism.

## Deploy is part of "done"

A deploy-dependent task is not done until its record NAMES the live SHA. Deploy via `{{DEPLOY_CMD}}`
(which should build from a clean tree of HEAD, bake the build SHA, and prove it live — see
`patterns/deploy-contract.md`), then confirm `{{LIVE_URL}}` serves `build-<that sha>` before recording.
Exercise the changed path yourself before you trust it — "ship without waiting for approval" never
means "ship without verifying."

## Roles for long / multi-session campaigns

When an arc spans many sessions and one agent can't hold the state, split into three seats over the
same event-sourced board (state survives in the events, not in any agent's context):

- **LEADER** — plans and arbitrates. Reads the board, decides priority, writes directives to an
  append-only channel, records ADRs, and owns the deploy of code changes. Does NOT rubber-stamp its own
  work as verified. Keeps the resume anchor current (see `maintain-audit-reconcile.md` Prompt B).
- **WORKER** — executes. Runs the loop above, lands gated commits, does data/content deploys, posts
  status events. Escalates a genuine block as a `blocked` event with the exact question rather than
  guessing or thrashing — and asks the leader for guidance instead of spinning.
- **VERIFIER** — a transient independent closer for a declared boundary or sampled batch. Re-derives
  the result, re-reads the evidence, reports `PASS`/`FAIL`/`INCONCLUSIVE`, and exits. It is never the
  builder and never fixes the findings it is judging. Routine low-risk tasks stay in the solo lane.

Each seat reads the board + the channel tails on boot; every claim is an event; every `done` is
server-granted. That's what lets the campaign pause and resume across days without losing or
double-doing work.

## Anti-patterns (learned the hard way)
- **Lone background delegation.** Handing your single task to one background agent and waiting defeats
  the purpose — do the work yourself; reserve fan-out for real parallel breadth.
- **Substring status flips.** Never bulk-close tasks by matching titles/substrings — you'll over-match
  and self-inflict false-green. Flip status by explicit ID only.
- **Off-board "quick fixes."** The quick fix that isn't on the board is the drift the audit gate exists
  to catch. Board first, always.
- **Whack-a-mole deploys.** Ship complete, validated units — not a broken piece you'll chase across
  three more deploys.
