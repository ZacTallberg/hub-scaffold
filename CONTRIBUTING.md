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
  marker, route-audit coverage, and a refusal PROVEN to fire (seeded positive red, true negative
  quiet, both runs recorded).
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
  update every copy and let `tools/docs_check.py` guard the file relationships.

## Proportional verification

```bash
bash tools/check.sh
```

This impact-aware sanity pass is sufficient for ordinary work unless the changed boundary demands
more. Use `bash tools/check.sh --all-fast` for the complete cheap set. A release, security/auth,
migration/destructive, public API/schema, concurrency/process-launch, regression, or sampled audit
should dispatch the disposable `verification-closer` and may justify `bash tools/selftest.sh`.

See `docs/TESTING.md` for exact selection guidance. A new declared gate/refusal must be PROVEN to
fire before it counts: seed a real positive, watch it go red, confirm it stays quiet on a true
negative — in the session, at the time you write it, with both runs recorded as the receipt.
Leave no test file behind; this repo ships no battery, and the write API refuses a bare suite
runner as a task's proof for the same reason the battery is gone.

## Pull requests and publication

- Explain the behavior and trust-boundary impact, not just the edited files.
- Include exact verification results.
- Do not weaken or bypass a failing check to make a change green.
- Verify generated files and the scrub before pushing.
- Use targeted commits and review `git diff --cached` before publication.

The repository currently grants no license. Public visibility does not itself permit reuse or
redistribution; coordinate an explicit license before accepting reuse-oriented contributions.
