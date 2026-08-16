# BUILD — drive real work off the board (the Hub loop + roles)

When work changes the Hub itself, execute `elevate-hub.md` against `PROJECT/HUB-QUALITY.md` inside
the same claimed task.

Turn tasks into shipped work. This is the day-to-day execution loop plus the leader/worker/closer
roles for long multi-session campaigns where state cannot live in one context.

Read `00-orchestration-method.md` first. Substitute `{{DEPLOY_CMD}}`, `{{LIVE_URL}}`.

---

## The loop every unit of work runs

**DISCOVER → CLAIM → IMPLEMENT → ATTEMPT → RECORD.** Give a working agent this contract:

> Work only from the Hub board. For each unit of work:
> 1. **DISCOVER** — read the top unblocked, unclaimed task ranked by dependency and priority.
> 2. **CLAIM** — acquire its lease before touching anything. Keep one task in progress at a time.
> 3. **IMPLEMENT** — do only that task. New work becomes a new board task before it is attempted.
> 4. **ATTEMPT** — perform the changed operation on the real target. Success is the default proof;
>    an observed failure is all the notice needed and becomes fresh task input. Record it; do not
>    speculate, silently expand delivery scope, or preemptively become the repair agent. The operator
>    may route it to a dedicated repair/error-fixing lane later.
> 5. **RECORD** — mark it `done` with an honest receipt such as a commit, live SHA, or operation result.
>    The Hub records evidence; it does not demand a verification command for routine work.
> 6. **PROBE only a critical boundary** — security/authorization, destructive data integrity,
>    migration, public protocol compatibility, or concurrency/fencing. Any justified test is a
>    one-shot temporary probe, deleted before commit while its receipt remains.
>
> Never create a test, copy assertion, screenshot gate, or independent review for page copy,
> formatting, color, spacing, ordinary animation/style work, or another non-critical fix. Completed
> child receipts compose into the parent; releases probe only a newly created integration seam. No
> worker or closer may nest another verifier.
>
> **Stop rule:** once the real changed path succeeds and no critical boundary was crossed, record the
> task and stop. Do not turn completion into a second project of proving completion.

Nothing happens off-list. Decisions are append-only ADRs. Do not delegate a single task to a lone
sub-agent and wait; fan out only genuine independent work.

## Deploy is part of “done”

A deploy-dependent task is not done until its record names the live SHA. Deploy via
`{{DEPLOY_CMD}}`, then use `{{LIVE_URL}}` to exercise the changed live path and record the observed
build identity. That live attempt is the proof. Do not add a general test run or re-prove child tasks;
at most, exercise the new integration seam introduced by this release.

## Roles for long or multi-session campaigns

- **LEADER** — plans and arbitrates, owns priority and deploys, and composes completed task receipts.
  It never reruns child proof or creates proof work to fill the board.
- **WORKER** — claims and implements one task, attempts the real operation, records its receipt, and
  records observed failures as fresh task input instead of guessing, thrashing, or switching itself
  into a repair role.
- **CLOSER** — an exceptional, transient independent reviewer for one named critical boundary. It
  performs the smallest decisive operation, returns `PASS`/`FAIL`/`INCONCLUSIVE`, and exits. If a
  one-shot test is essential, it deletes the temporary artifact before handoff. It never fixes the
  finding, spawns another closer, or becomes a standing worker.

Each seat reads the board and channel tails on boot; every claim is an event and every `done` is
server-granted. Durable state lets a campaign pause and resume without losing or duplicating work.

## Anti-patterns

- **Lone background delegation.** Reserve fan-out for real parallel breadth.
- **Substring status flips.** Change status only by explicit task ID.
- **Off-board quick fixes.** Put the work on the board first.
- **Proof inflation.** Do not attach tests, copy validation, verifier fan-out, or release reruns to
  simple completed work.
- **Permanent test residue.** Critical probes are temporary and gone before commit; only receipts stay.
- **Speculative repair.** Delivery agents record observed failures for later routing; they do not
  preemptively diagnose or fix a new scope under the original delivery task.
