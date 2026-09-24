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

## A green signal is not evidence

An exit code, a green pipeline and a passing assertion share one weakness: their mechanism cannot
tell "it worked" from "it never ran". `git push` can exit 0 having pushed nothing (a credential
prompt blocked in the background and the shell's status was read as git's); a readiness check can
assert exactly the defect it should catch; a check that a guard exists can pass with the guard
deleted because a comment names it. So, whenever proof is gathered — the ordinary real operation
or a rare critical probe:

- **Real cases, on data nobody authored for the check.** A check written by the person who wrote
  the code encodes the same belief and hands it back in green. Prefer inputs that already exist:
  real questions and the answers that resolved them, recorded failures with their causes, a real
  document through the real model. Three hand-written cases labelled by the author are an opinion
  with numbers attached. If real data says the work does not help, that is the finding.
- **Probe the deployed system and READ what came back.** Drive the surface a person or an agent
  actually uses and put its output in the receipt — the served bytes, the deployed sha, the row
  that was written, the rendered page. A transcript of the system answering is evidence; an
  assertion that passed is not.
- **Fix what breaks; do not pre-anticipate it.** A check written to confirm code you just wrote is
  a guess about which failure might occur. Make failure audible instead: wire it to the operational
  error stream (`patterns/error-visibility.md`) so a real break arrives with its input and
  traceback, then repair that.

For the rare critical probe (the boundaries above), four more rules:

1. **Write MUST-NOT assertions.** "Readiness must NOT answer 200 with the database down" cannot be
   satisfied by agreeing with the implementation, which is the only shape that catches a check
   that pinned the bug.
2. **The acceptance criterion comes from someone other than the implementer**, written as a
   runnable command before the code, and the implementer does not edit it. Checks generated after
   faulty code find markedly fewer of its faults than checks written independently.
3. **Falsification is necessary, never sufficient.** Disabling the mechanism and watching the probe
   fail proves the probe is COUPLED to the mechanism, not that it asserts the right thing: a probe
   that pins the defect goes red when you disable the mechanism too, and reads as strong.
4. **Verify the instrument before the result.** A harness that mutates code to falsify a probe must
   print what it changed and how many anchors it matched; a zero-match mutation is a HARNESS
   failure, never "the probe survived". Confirm the fixture is healthy, then break exactly one
   thing. And never let an agent type the count — the runner prints it.

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
