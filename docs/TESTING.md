# Proof without test accumulation

The default proof is the real operation the task exists to change. Run the command, use the
workflow, open the live surface, or perform the transition. If it fails, that failure is the notice
to capture as fresh task input for a repair lane. If it succeeds and the task did not cross a
critical boundary, record the result and stop.

This repository does not accumulate permanent tests, verifier scripts, fixture suites, or automatic
test workflows. A standing battery makes every future task pay for old proof and encourages teams
to validate the checker instead of finishing the work. Proof belongs to the task that needs it.

## The default

- Exercise the changed artifact through its real operation.
- If that operation exposes a failure, record the failure as fresh Hub task input. It may later route
  to a dedicated repair/error-fixing lane; the delivery agent does not speculate about unrelated
  causes or preemptively become the repair agent.
- Record a truthful receipt: the resulting commit, live URL, screenshot, command output, or Hub
  event, whichever naturally demonstrates the outcome.
- Do not create or run a test for copy, wording, spacing, color, animation polish, routine styling,
  or another non-critical fix. Looking at and using the real page is sufficient.
- Do not add a test merely because code changed, a release is approaching, or a task asks for
  "verification." The plausible consequence must justify it.

## The rare critical exception

A focused test is justified only when the task crosses a boundary where an unnoticed failure could
cause material harm: security or authorization, destructive behavior, data integrity, a migration,
public protocol compatibility, or concurrency.

When one is justified:

1. Create the smallest probe that exercises only that boundary in a temporary directory or other
   disposable work area.
2. Run it once against the real changed artifact.
3. Record the exact operation and result as the task receipt.
4. Delete the probe, fixture, generated database, and every other test artifact before committing.

The receipt is durable; the test is not. Do not promote the probe into a repository test, verifier,
fixture, package script, pre-commit hook, CI job, or scheduled workflow.

## Proof composes

A completed dependency contributes its receipt to every parent task, milestone, and release that
contains it. Parents inherit that proof; they do not rerun it. A release may exercise only the new
integration seam created by combining already-proven work, and only when that seam is itself a
critical boundary.

Never build a verifier that launches other verifiers, never make one task replay every child's
checks, and never expand a focused probe into a general suite. Nested verifier fan-out is forbidden
by default because it multiplies latency without producing new information.

## Stop rule

Once the real changed behavior succeeds, the receipt is recorded, and no critical boundary remains
unproven, stop. Do not add another check to increase confidence cosmetically. Copy and visual polish
receive no automated validation. A critical transient probe is complete when it has produced its
receipt and has been deleted.

When the operation fails, stop the proof attempt after capturing enough evidence to create the new
task. Do not fan out speculative diagnostics inside the delivery task. A repair lane can claim and
resolve that failure through the same actual-operation-first loop.

## What remains adopter-owned

Production TLS and read-auth boundaries, deployment providers, canaries, alert delivery, backup
restoration, external-protocol prompts, worker wrappers, and project business behavior can only be
proven in the adopting environment. Perform the real operation there when a task changes one of
them; use a transient probe only for the critical boundaries named above.

There is intentionally no standing test workflow. Repository maintenance utilities may be invoked
manually when their artifact is the subject of the task, but they are not a completion ladder and
must never be run to validate page copy or routine visual work.
