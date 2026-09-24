# The deploy runbook — an agent executes it, reading real output at every step

`deploy-contract.md` states the four laws that make "deployed" a fact. This file is how you
SATISFY them without a deploy script, and it is the recommended default.

## Why a runbook and not a script

A deploy script encodes one environment's assumptions and then rots against the platform it
drives. When it breaks it usually breaks *silently or half-way*: a step is skipped, an exit code
is swallowed, a pipe kills a push while reporting success — and the operator is left holding a
green result and no idea what actually happened. The failure that motivates this pattern was
exactly that shape: a helper returned a status word instead of what the server said, and a
divergent-ledger error naming the exact problem was discarded 495 times in a row.

An agent working from a runbook reads the real output of each step and adapts. The runbook's job
is to make each step's EXPECTED OBSERVATION explicit, so "it worked" is a thing you saw rather
than a thing you assumed.

**This is not an argument against code.** Keep as code anything that must REFUSE when nobody is
watching — a server-side push refusal (`pre-receive-gate.sh`), an agent guard. A refusal that is
prose does not refuse. What leaves is the thing that *does the work silently* — and the watcher
whose own death reads as green: the standing re-check is a by-hand procedure with a receipt
(`standing-canary.md`), not a cron.

## The binding table — fill this in once, at adoption

The runbook stays platform-neutral by naming capabilities, not commands. Write your platform's
command for each, keep it beside the runbook, and every step below becomes literal.

| Binding | What it must do |
|---|---|
| `PUSH_CMD <sha>` | Ship exactly that revision. Not a branch name — see step 4. |
| `SET_BUILD_ID <sha>` / `READ_BUILD_ID` / `CLEAR_BUILD_ID` | If your platform stamps build identity through a mutable setting rather than carrying it in the revision, you need all three. **Prefer a platform that derives identity from the revision it checked out** — then steps 3 and 7 disappear and so does an entire class of concurrency bug. |
| `LIVE_ID_CMD` | Read the identity the RUNNING artifact reports — from the artifact itself, never from a record your own deploy wrote. |
| `ORIGIN_PIN` | Address the host directly, bypassing any cache or proxy, while keeping certificate validation on. |

## Steps

1. **Stand on the revision you mean to ship.** Record its sha. A dirty working tree ships nothing
   you can see: what deploys is a commit. Print the dirty files rather than only noting they exist.
2. **No standing validation ceremony.** The committed task receipt is inherited; do not replay a
   suite, audit ladder, copy check, screenshot ritual, or generic "fast checks" bundle because a
   release is happening. Only when this release crosses an explicitly identified critical
   integration seam, exercise that one seam with a transient probe, retain its receipt, and delete
   the probe artifact before continuing.
3. **Stamp the build identity, then read it back** (only if your platform needs `SET_BUILD_ID`).
   Expected: the read-back names YOUR sha. A build that fails closed on a missing stamp is correct
   behaviour — it is refusing to produce an artifact that cannot say what it is.
4. **Acquire one real per-target release lease, then ship the EXACT SHA, never a branch name.**
   Builds for different artifacts may overlap, but the swap-through-canary boundary for one front
   door is serialized. Give every release attempt its own remote script, log, and verdict identity;
   per-app and per-tag paths both collide under retries. You stamped a
   specific revision in step 3;
   pushing a branch ships whatever that branch points at now, which on a detached checkout or a
   worker branch is something else entirely — an artifact whose identity names bytes it was not
   built from, the one lie this whole contract exists to prevent.
   Watch the output. Never pipe it through `tee | tail` or similar: a broken pipe can kill the
   push while the pipeline reports success. Redirect to a file and read the file.
   **A dead transport is an OBSERVATION, not the outcome** — the platform may have finished
   building and swapped in the new release before the connection dropped. Do not conclude
   anything here; let step 5 tell you what is actually live.
   If the platform reports "no changes" because its generated release source already names this
   image while the front door still serves another SHA, immediately invoke the platform's
   rebuild/release-from-current-source operation. Waiting on the canary cannot perform a missing
   swap.
   If the platform itself accepts only one import/swap operation across all apps, acquire a second,
   short platform-capacity slot immediately around that operation. Release it as soon as the swap
   command exits; do not serialize builds or another app's independent canary behind a capacity
   constraint they do not consume.
   An indirect release is still a release. Any configuration command that automatically restarts,
   rebuilds, imports, or swaps an app must use that same capacity slot; otherwise the platform's
   background event listener can race a correctly leased release. Prefer a no-restart configuration
   mutation, then acquire the capacity slot and invoke the required restart or rebuild explicitly.
   Configuration proven not to trigger a release does not consume the slot.
5. **Canary: wait boundedly for the artifact, and judge by CONTAINMENT.** A cold start can outlast
   one request's patience, so repeat the observation a bounded number of times with a short pause.
   This transient release wait is not application synchronization; the live Hub remains push-only.
   Read the identity the artifact reports about ITSELF — never a label your own deploy record
   supplied, which would let the release confirm itself.
   Capture each response body completely before comparing it. Under `pipefail`, piping `curl`
   directly into an early-exit matcher such as `grep -q` can close the pipe after a successful
   match and misclassify curl's resulting broken pipe as a failed canary.
   Three verdicts, not two:
   - **OK** — the live identity is, or contains, your sha. Containment matters because a
     teammate's deploy can land between your push and your read, and a canary that reports red
     every time that happens is a canary people learn to ignore.
   - **FAIL** — the live identity is known and does NOT contain yours. This is the only red that
     may trigger a rollback.
   - **UNVERIFIED** — you could not read an identity at all, or read one you cannot place.
     Containment is unknown. **Do not roll back on an unknown** — say so and stop for a human.

   **Every post-release probe runs at the coldest moment there is.** It fires seconds after the
   release restarted the service, so its first read pays for the whole warm-up (a large board
   snapshot measured 14–26 s cold and ~1.5 s warm). A single 30 s attempt at that moment measures
   cache temperature and reports it as a dead app. So each post-release probe (the board page, a
   heavy JSON feed, a rendered-page pass):
   - gets a small bounded number of attempts on a generous per-attempt timeout (three at 60 s is
     a reasonable start), with a short pause between them;
   - **prints the elapsed time of every attempt**, and a `SLOW` line above a threshold (10 s) — a
     bigger timeout on its own trades a false red for a silent regression; printed latency keeps a
     creeping endpoint audible;
   - still fails, with every attempt's timing attached, when the endpoint genuinely never answers.

   **A headless rendered-page pass has two waits, and one deadline must cover both.** Navigation
   does not report until it COMMITS (response headers arrive); only then does the load event
   follow. A harness that bounds the load event at 30 s but leaves the navigate call on its socket's
   10 s default fails every slow first byte without ever reaching the budget you set. Run
   commit + load under ONE deadline, print `NAVIGATED path=<path> commit=<s> load=<s>` (the path
   only — never a query string that may carry a grant), name which half timed out, and allow one
   retry of the whole pass after a short pause.

   **A wrapper that forwards arguments must prove it forwarded them.** A retry wrapper added to a
   rendered-page pass once splatted a variable that the shell treats as a reserved automatic name
   (always empty), so the harness ran with NO arguments against its own default base URL — turning
   a flaky red into a deterministic one. Have the harness print its effective base URL and mode as
   its first line, so a dropped flag reads as plumbing, not as a broken page.

   **Assert the property, not the label.** A rendered-page check that requires a literal heading
   ("Message") fails the day the copy becomes "Newest message", on a dialog that opened and showed
   the right row. Assert structure (the dialog opens and carries the row) and accept either
   vocabulary; stay fail-closed so the check still fails when the row is genuinely gone.
6. **Retract the stamp** (if you set one). A persistent build-identity setting means any later
   out-of-band rebuild bakes a now-stale identity with no canary behind it. Clear ONLY the stamp
   you set: read it first and leave it alone if it names someone else's sha, or you will disarm a
   concurrent deploy and their build will fail on the missing stamp.
7. **Resolve the one release-closure dependency without lying about it.** Run the authenticated
   audit after the exact front-door canary. Before the immutable deploy entity exists, a freshly
   swapped artifact may have exactly one high finding: `coherence:repo`, naming the prior recorded
   SHA and the new running head. Treat that as `closure_pending` only when the canary observed the
   exact new head and there are zero other critical/high findings. Any other finding blocks.
   Complete only the explicitly carried task(s) from their real-operation evidence. This makes
   them eligible for `tasks_closed`; it does not itself claim a release exists.
8. **Record the deploy through the typed writer**, with the sha and the identity the canary
   actually observed. `tasks_closed` is the explicit set of already-done tasks carried by this
   release, not merely the tasks completed during this deploy. Send this only after the front-door
   canary observed the exact SHA and step 7 found either a normal passing audit or solely the exact
   `closure_pending` condition. In the latter case `audit_ok: true` names the deterministic
   postcondition of appending this matching closure; it is not permission to excuse another red:

   ```http
   POST /hub/api/deploy
   X-Write-Token: <HUB_WRITE_TOKEN>
   Content-Type: application/json

   {
     "sha": "<shipped-sha>",
     "served_sha": "<the-same-sha-observed-by-the-canary>",
     "tasks_closed": ["<project>:task:0001", "<project>:task:0002"],
     "at": "<ISO-8601 timestamp>",
     "method": "<platform/deploy path>",
     "audit_ok": true,
     "agent": "<operator id>"
   }
   ```

   The writer refuses mismatched SHAs, unknown/non-done task ids, duplicate task ids, and attempts
   to rewrite an existing SHA's proof. An exact retry is idempotent — which is what makes it safe
   to **retry this post against a cold hub**, and you should: the record is posted seconds after a
   restart, a single short timeout there silently loses it, and a lost record costs the board its
   rollback target and its attribution while the pipeline stays green. `python -m hub_core.client
   deploy --sha <sha> --served-sha <sha> --task <id> ...` does exactly this: growing per-attempt
   timeouts (20 s, 45 s, 90 s by default), one `DEPLOY_RECORD_RETRY` line per transport failure,
   and never a retry of a refusal (a 4xx is the Hub's verdict, not the network's). An attempt that
   landed but whose reply was lost comes back as `idempotent: true` on the next try, not as a
   second record or a false failure. This immutable closure plus
   the running artifact identity makes every named task immediately `live` without Git or a
   polling cycle. A deploy nobody recorded did not happen as far as the board is concerned.
9. **Read the authenticated audit once more.** The closure must have removed `closure_pending` and
   the result must contain zero critical/high findings. If it does not, the immutable record remains
   truthful history with its exact inputs, the release stays visibly unhealthy, and repair begins
   from that observed finding rather than rewriting the record.
10. **Check your unauthenticated surface** — one request per invariant your project declares, and
    one ANONYMOUS request to the app's root and to `/hub/`: each must be refused (401/403, or a
    redirect to sign-in). An anonymous 200 fails the deploy unless the project has DECLARED that
    surface public — a verifier that only ever sends credentials cannot tell an authenticated app
    from an open one.
11. **Done means named:** the recorded event carries the live sha. Release the per-target lease only
    after the canary, immutable deploy record, and post-record audit succeed (or the attempt has
    failed closed).

## Protecting durable data across a release

If a release step can disturb a file the Hub or the app depends on (a checkout reset that sits
beside the ledger, a migration that rewrites a database), copy it to a vault first; the copy is what
a rollback restores. Two rules keep that protection cheap enough to run on every release and
impossible to erode:

- **Skip bytes that did not change — and check BEFORE the prune.** An append-only ledger whose
  length and write time equal the newest vault entry is already in the vault, verbatim. A SQLite
  database is unchanged only when the main file AND its `-wal` and `-shm` side files all match
  (length alone is not enough: a same-size page rewrite changes content without changing size).
  Print what stands in for the skipped copy (`LEDGER_BACKUP_CURRENT`, `DB_SNAPSHOT_CURRENT`
  naming the entry) — a silent skip and a protection that never ran leave identical logs. Run the
  check before pruning: a prune that keeps `retain - 1` assumes a new copy is about to join, and
  skipping after it would erode the vault one release at a time.
- **Prune (or check free space) before you write, never only after a successful write.** A prune
  that runs only after a successful copy never runs again once the disk is full — every copy then
  fails, every release that depends on the snapshot rolls back, and the only evidence is a
  snapshot-failed line deep in each job log. A fallback vault on the same disk as the data it
  protects keeps fewer copies than the real one.
- **Count lines lazily.** Streaming a large ledger through a shell pipeline as one object per line
  to print a count costs more than the copy; use a lazy line enumerator, and count blank lines the
  same way on both sides of any before/after comparison.

## Rollback

Find the last deploy the canary confirmed, and prefer a shared source (the board) over a local
file, so it does not matter which clone is rolling back. Then repeat steps 3–9 with that sha.
A rollback moves the target backwards, which an ordinary push refuses — force is required and
deliberate. If the last-good sha IS the failed sha, stop: re-shipping it cannot help.

## Concurrency — the honest limit

Two agents releasing one front door can overwrite each other even when identity travels inside the
artifact: the older, slower swap may land last. A real per-target lease must span swap, canary,
blessing, and deploy-record append; per-attempt logs prevent cross-talk between observers. If a
mutable app-scoped build setting also exists, acquire the lease before setting it and release it
only after clearing your own value. Do not pretend a documented convention is mutual exclusion.
