# Atomic failure-to-repair lane

## Question

How should a worker report a real failed attempt without creating retry storms, stranded leases,
duplicate repair work, or an operator-interruption queue for routine recoverable faults?

## Findings

⚑ **Failure is one state transition, not a sequence of best-effort calls.** Recording the attempt,
opening or extending backoff, linking repair work, and returning the lease must share one serialized
operation. Otherwise a crash can leave the board claiming that work is active after its worker has
already stopped, or can create many repair twins for one cause.

⚑ **Canonical entities need an all-or-none append primitive.** The JSONL remains truth, so an atomic
same-directory replacement containing the complete event batch is the durable commit point. The
SQLite index can then commit all batch rows together and recover from the canonical file after a
crash, preserving the store's existing file-first discipline.

⚑ **Retry pressure must be bounded and cause-specific.** Consecutive repeats of the same stable
failure signature grow exponential backoff only to a configured ceiling. A changed signature starts
a fresh repeat count. At the configured threshold the circuit opens instead of feeding the same
broken unit back to every available worker.

⚑ **Repair work needs a deterministic address and a specialist route.** A repair-task id derived
from source task plus failure signature makes concurrent/retried reports converge. Its hard routing
requirement names the repair capability; no general worker receives it accidentally.

⚑ **Human attention is a consequence boundary, not a failure default.** Routine failures remain in
the repair lane even after their circuit opens. Only a caller's explicit consequential flag puts the
source and repair task on an operator-cleared route.

## Design consequence

`POST /hub/api/fail` will require the authenticated lease owner, fencing token, stable signature,
and concrete failure note. Under the claims lock it validates both the failed-task update and the
deterministically addressed repair task, appends them as one durable event batch, and expires the
exact lease before acknowledging. The source task returns to `todo` with bounded `not_before`,
repeat counters, and circuit fields. The repair task requires `hub.repair` capability (plus
`hub.operator` when consequential), links back through `repair_for`, and is reused rather than
duplicated on repeat reports.

