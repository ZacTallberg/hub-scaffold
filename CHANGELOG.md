# Changelog

This file records changes to the scaffold itself. Project changelogs generated from adopted Hub
deploy events are a different artifact (`hub_core.projections.render_changelog_md`).

## Unreleased

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
