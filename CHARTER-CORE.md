# CHARTER-CORE — the invariants that survive context compaction

This is the distilled, hash-pinned core of the worker discipline. A worker whose context was
compacted re-grounds HERE (`python -m hub_core.client reground`); the loop's finish verb refuses
a completion whose held charter sha does not match this file's current sha256 — holding a stale
or absent core is a DETECTABLE condition, not a silent drift. Seat charters under
`PROJECT/pm/seats/` bind roles; nothing here supersedes an operator ruling.

1. COMPLETION IS THE REAL OPERATION — `done` exists only via complete(): a held lease, an
   accept note, and dereferencing evidence. A typed exit-0 verification_run receipt is required
   ONLY when the task itself declared a rare critical-boundary verification_command; ordinary
   work is never downgraded for correctly having no receipt. Never self-attest a status flip.
2. ACCEPTANCE IS THE CONTRACT — work to the task's OWN acceptance text at full scope; thin or
   absent acceptance cannot enter the executing states; discovered work becomes new hub
   entities the same turn, never a silent scope change.
3. ONE MUTATION ENTRANCE — an active hub is mutated through its served write API, so append,
   validation, lease fencing, and realtime publication stay indivisible. Direct ledger writes
   are offline recovery only, with live writers drained.
4. LEASES FENCE EVERYTHING — one active task per agent; active/complete require the holder's
   fencing token; a leased task's spec belongs to its holder. Heartbeat while you hold.
5. PROOF WITHOUT TEST ACCUMULATION — exercise the changed path on the real surface and record
   what happened. A transient probe is justified only for a named critical boundary
   (authorization, destructive data, migration, protocol, concurrency); run it once, keep the
   receipt, delete the probe before commit. Never add permanent tests, snapshots, or verifier
   ladders for copy, style, or ordinary changes.
6. COMMITS ARE FENCED AND ATTRIBUTED — commit only the paths your task touched in a shared
   tree; carry the task id in the message; author as your agent identity.
7. NO SILENT DESCOPE — BLOCKED is scheduling, not resolution; ask through the delivered ask
   loop instead of stalling in silence; capture everything; a context limit is not terminal —
   re-ground from this file and continue.
