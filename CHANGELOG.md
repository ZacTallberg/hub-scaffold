# Changelog

**This repository IS the canonical template** (since 2026-08-09; from 2026-08-07 to then it was a
generated export of a private upstream, now retired — its engine history is fully carried here).
Improvements are committed directly, gated by `tools/scrub_check.sh` (nothing project-, host-, or
person-specific) and proven by `tools/selftest.sh` against the real example app. Changes born
inside a working instance arrive as curated, scrubbed upserts after the instance has proven them
in production — never as a bulk merge. The 2026-08-07 consolidation resolved a three-way engine
fork; committing anything here that an instance also carries divergently is how that fork starts
again, so upsert whole units, not fragments.

This file records changes to the scaffold itself. Project changelogs generated from adopted Hub
deploy events are a different artifact (`hub_core.projections.render_changelog_md`).

## Unreleased

### 2026-08-09 — re-export: the no-battery regime, the operator off-switch, and the last ops scripts out

- **The unit battery is gone, with the machinery that would regrow it** (upstream ruling
  2026-08-08). `hub_core/tests/` deleted; `selftest.sh` step 2 and `check.sh --all-fast` re-subjected
  to a compile/import floor; `docs/TESTING.md` rewritten as the verification doctrine (a guard is
  proven by watching it fire; a feature against the real example app — step 5 is unchanged and is
  the model); CONTRIBUTING/campaign prompts no longer assign test-writing obligations.
- **The write API refuses a bare suite runner as a task's proof** (`verification_command_is_a_suite`,
  422): a suite is green whenever the repo is healthy, whether or not the task's work happened.
  Proven both directions against the running example app before export.
- **`hub_core/audit.py` regenerated from canonical** (same transform: copy, LF, scrub vocabulary);
  the in-module oracle-tamper selftest left with the battery.
- **Every seat now has an operator off-switch**: `adapters/windows/launch-worker.ps1` polls
  `PROJECT/.hub/fleet-target` each cycle and disarms on ≤0; `patterns/worker-longevity.md` opens
  with the stop procedure — a fleet designed never to stop must still be stoppable, deliberately.
- **`deploy.sh.example` and `standing-canary.sh` are out; `standing-canary.md` replaces the cron.**
  The runbook is the deploy path; the standing re-check is a by-hand procedure with a receipt — a
  scheduled watcher's own death reads identical to "all green". `pre-receive-gate.sh` alone stays
  code, because a refusal that is prose does not refuse.

- **Engine forward-ported from the consolidated canonical (2026-08-07).** The July engine was a
  fossil relative to upstream: `store.py` gained the whole cross-process serialization layer it
  never had (`LedgerLock`, durable replace + fsync, jsonl tail hashing, fork linearization, and
  the heal that takes the write lock before dropping its append-only triggers); `audit.py` went
  from the generic core to the full guard suite; `ids/project/projections` gained the ADR-9
  entity types (commit, finding, lesson, method, review, telemetry) and the task-bed ordering;
  new engine modules `upcast`, `ownership`, `grandfather`, `wip`, `schedule`; the app shell
  gained its accessibility wrapper and live region. All under the agnosticism gate.

- Closed request-scoped EventStore handles across state, audit, read-snapshot, append, and decision
  paths while preserving caller ownership of explicitly supplied stores; added focused regression
  coverage for normal and exceptional exits.
- Replaced the per-change full-test expectation with impact-aware fast checks and a manually invoked,
  isolated full verifier.
- Added a reusable disposable `verification-closer` skill and campaign prompt with risk-based trigger,
  evidence, read-only, terminal-verdict, and exit contracts.
- Isolated the Django refusal ladder in a unique temporary ledger/database so repeated or concurrent
  verification cannot grow or contend on shared example runtime state.
- Completed a repository-wide documentation truth pass.
- Added canonical architecture, security, operations, testing, and contribution guides.
- Documented the command-execution and server-side-fetch authority of the general write token.
- Distinguished unauthenticated reads from automatically sanitized/public-safe data.
- Reconciled tracked and strict completion behavior across policy, API, templates, and examples.
- Closed the tracked-mode empty-evidence gap with input/schema enforcement and a refusal test.
- Made done-task evidence and accepted-ADR prose substantive schema requirements, not presence-only fields.
- Added automated documentation-link and mirrored-schema checks to the self-test.
- Made queue ownership truthful: claims now validate availability, atomically record `in_progress`,
  disappear from discovery while leased, reappear for stale reclaim, renew idempotently for the
  same owner, and release on completion.
- Fenced completion against lease-expiry and concurrent-task-edit races, and added an end-to-end
  queue refusal/recovery ladder.
- Added a Python 3.10/Django 5.2 and Python 3.12/Django 6.0 CI matrix to prove the documented
  compatibility range while excluding untested future Django feature series.

## 2026-08-03

- Added the optional fail-closed Windows local-worker launch bridge.
- Removed browser write-token/unlock UI and replaced it with a same-origin CSRF mint plus
  token-gated, issuer-bound, single-use consume.
- Added cross-process launch state locking and wrapper-lifecycle cleanup.
