# verify/ — transient critical-boundary proof contract

> canonical contract · owner: leader · update: only when the critical-boundary policy changes

This directory defines an exceptional lane, not a standing test system. The successful real
operation is the default proof for every task. Copy, wording, style, spacing, color, animation
polish, routine fixes, and other non-critical changes never activate this lane and receive no
automated validation.

Permanent tests, fixtures, verifier scripts, calibration sets, generated manifests, scheduled
runs, and CI verification workflows are prohibited. A release is not automatically a reason to
verify completed work again.

## 1. Activation boundary

Activate a transient verifier only when the leader names a concrete risk at one of these critical
boundaries:

- security or privilege;
- destructive writes or data integrity;
- schema or data migration;
- public protocol compatibility;
- concurrency, leases, or fencing.

If the changed behavior can be safely exercised through its real operation, do that and stop. A
temporary probe is justified only when the real operation cannot expose an unacceptable failure
clearly enough.

## 2. One-shot procedure

1. Inherit the accepted receipts of every completed dependency. Never rerun child proof.
2. Name only the newly created critical integration seam, if one exists.
3. Prefer the real protected operation. If necessary, create one probe in system temporary space
   or explicitly disposable task scratch—never as a tracked project file.
4. Run it once against the final change and record a durable receipt containing the task, boundary,
   action or exact command, target SHA/state, verifier identity, observed outcome, and timestamp.
5. Delete the probe, fixture data, copied database, and all scratch before commit. Confirm only the
   receipt remains, then fold the verifier seat.

Receipts compose upward. A parent task or release accepts completed child receipts and, at most,
observes the one new critical seam created by composition. A verifier must never launch another
verifier or create checker fan-out.

## 3. Failure routing

An observed real failure is all the notice the project needs. Open a fresh repair task with the
failed action and observed outcome. Route it to a dedicated error-fixing agent when the project has
one so delivery agents continue unrelated work. After the repair, retry the failed real operation;
its successful result closes the task. Do not preserve the diagnostic as a regression test.

## 4. Stop rule

When the changed real behavior succeeds, no critical boundary remains unobserved, the durable
receipt is filed if one was required, and all temporary proof artifacts are gone, stop. More checks
reduce throughput and are a process defect.

`MANIFEST-CONTRACT.md` is dormant reference material unless a leader explicitly scopes it for a
single critical boundary. It does not authorize a standing generator, sweep, harness, or gate.
