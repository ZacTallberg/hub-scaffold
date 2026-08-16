# Contributing

Contributions should preserve the scaffold's three defining properties: environment agnosticism,
truthful documentation, and fail-closed behavior at privileged boundaries.

## Before changing code

1. Read `AGENTS.md`, `SECURITY.md`, and `docs/ARCHITECTURE.md`.
2. Record and claim the work in the local Hub when operating an adopted instance.
3. Inspect the working tree and preserve unrelated changes; stage only files belonging to the
   change.
4. Treat `PROJECT/.hub/` as runtime state. Never commit a local ledger, lease, token, grant secret,
   consumed nonce, database, or test artifact.

## Design rules

- Keep `hub_core/` standard-library-only. Django-specific behavior belongs in the adapter.
- Validate merged entity state before append; preserve optimistic concurrency and idempotency.
- Never add a second way to mint terminal task state. `done` remains a completion transition.
- Keep general write routes behind `@writer`; a narrower capability needs an explicit threat model,
  marker, route-audit coverage, and a refusal proven to fire with a focused transient probe. Record
  the refusal and successful operation, then delete the probe before commit.
- Do not put the general write token in URLs, pages, browser storage, registry values, or process
  arguments.
- A new entity type requires a schema, projection/fold registration, write path where needed, read
  surface, audit behavior, seed/example coverage, and documentation.
- Patterns and templates must say when adopter wiring is required. Never describe a contract file as
  a running gate, monitor, backup, alert, or verifier.
- Keep examples and prose free of names, hosts, paths, or assumptions from one environment.

## Documentation rules

- Update the API and configuration references in the same change as behavior.
- If a `PROJECT/` template changes, regenerate `PROJECT-PLANE-BOOTSTRAP.md` with
  `python tools/build_bootstrap.py`.
- Keep `PROJECT/schema/` and `example/PROJECT/schema/` identical.
- Add a changelog entry for user-visible scaffold changes.
- Prefer links to canonical docs over copying a rule into several places; when duplication is useful,
  update every intentional copy.

## Proof policy

The real operation is the default proof. Use the changed command, workflow, API transition, or live
surface and record what happened. Do not create or run tests for copy, wording, spacing, color,
animation polish, or another non-critical fix. When the real behavior succeeds and no critical
boundary remains, stop.

An observed failure becomes fresh Hub task input with the operation and output attached. Leave it
for an explicit repair/error-fixing lane unless it is already the claimed task; delivery agents do
not speculate or preemptively widen their role into repair work.

Only security/authorization, destructive behavior, data integrity, migrations, public protocol
compatibility, or concurrency may justify a test. Create the smallest focused probe in temporary
space, run it once, record the exact result as the task receipt, and delete the probe and fixtures
before commit. No test is permanent, and no probe becomes a suite, verifier, hook, CI job, or
scheduled workflow.

Receipts compose upward. Parent work and releases inherit completed child-task proof and may exercise
only a newly created critical integration seam. Do not replay child proof or dispatch verifiers from
inside verifiers. See `docs/TESTING.md` for the complete policy.

## Pull requests and publication

- Explain the behavior and trust-boundary impact, not just the edited files.
- Include the real-operation receipt and, only when a critical transient probe was justified, its
  exact result.
- Do not weaken or bypass a failing check to make a change green.
- Generate required derived files when their source changes; do not add a second validation ritual.
- Use targeted commits and review `git diff --cached` before publication.

The repository currently grants no license. Public visibility does not itself permit reuse or
redistribution; coordinate an explicit license before accepting reuse-oriented contributions.
