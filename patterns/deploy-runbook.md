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

**This is not an argument against code.** Keep as code anything that must hold when nobody is
watching — an out-of-band canary (`standing-canary.sh`), a server-side push refusal
(`pre-receive-gate.sh`), an agent guard. Those observe or refuse; they do not perform the work.
What leaves is the thing that *does the work silently*.

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
2. **Advisories.** Run your dependency/supply-chain and fast checks and READ them. Under this
   template they surface, they do not block — but an advisory you skip is one you own, so name it
   in the deploy record.
3. **Stamp the build identity, then read it back** (only if your platform needs `SET_BUILD_ID`).
   Expected: the read-back names YOUR sha. A build that fails closed on a missing stamp is correct
   behaviour — it is refusing to produce an artifact that cannot say what it is.
4. **Ship the EXACT SHA, never a branch name.** You stamped a specific revision in step 3;
   pushing a branch ships whatever that branch points at now, which on a detached checkout or a
   worker branch is something else entirely — an artifact whose identity names bytes it was not
   built from, the one lie this whole contract exists to prevent.
   Watch the output. Never pipe it through `tee | tail` or similar: a broken pipe can kill the
   push while the pipeline reports success. Redirect to a file and read the file.
   **A dead transport is an OBSERVATION, not the outcome** — the platform may have finished
   building and swapped in the new release before the connection dropped. Do not conclude
   anything here; let step 5 tell you what is actually live.
5. **Canary: POLL, and judge by CONTAINMENT.** A cold start can outlast one request's patience, so
   a single check reports a false red on a healthy release. Poll a few times with a short pause.
   Read the identity the artifact reports about ITSELF — never a label your own deploy record
   supplied, which would let the release confirm itself.
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
7. **Record the deploy through the typed writer**, with the sha and the identity the canary
   actually observed. This is the artifact that makes the release reviewable later; a deploy
   nobody recorded did not happen as far as the board is concerned.
8. **Check your unauthenticated surface** — one request per invariant your project declares.
9. **Done means named:** the recorded event carries the live sha.

## Rollback

Find the last deploy the canary confirmed, and prefer a shared source (the board) over a local
file, so it does not matter which clone is rolling back. Then repeat steps 3–7 with that sha.
A rollback moves the target backwards, which an ordinary push refuses — force is required and
deliberate. If the last-good sha IS the failed sha, stop: re-shipping it cannot help.

## Concurrency — the honest limit

If your platform's build identity lives in a mutable, app-scoped setting, two agents deploying at
once can overwrite each other's stamp between steps 3 and 6, and prose cannot prevent that. Either
adopt a platform that carries identity in the revision, or pair this runbook with a real lease
that a deploying agent must hold. Do not pretend a documented convention is mutual exclusion.
