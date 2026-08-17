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
   to rewrite an existing SHA's proof. An exact retry is idempotent. This immutable closure plus
   the running artifact identity makes every named task immediately `live` without Git or a
   polling cycle. A deploy nobody recorded did not happen as far as the board is concerned.
9. **Read the authenticated audit once more.** The closure must have removed `closure_pending` and
   the result must contain zero critical/high findings. If it does not, the immutable record remains
   truthful history with its exact inputs, the release stays visibly unhealthy, and repair begins
   from that observed finding rather than rewriting the record.
10. **Check your unauthenticated surface** — one request per invariant your project declares.
11. **Done means named:** the recorded event carries the live sha. Release the per-target lease only
    after the canary, immutable deploy record, and post-record audit succeed (or the attempt has
    failed closed).

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
