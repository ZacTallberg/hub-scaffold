# Campaigns — the robust agent prompts that maintain, improve, and augment the Hub

Use `elevate-hub.md` whenever constructing or materially upgrading a Hub experience.

The Hub and plane are the nouns of this system; campaigns are the verbs. These playbooks coordinate
single-agent or multi-agent work without losing durable state, while protecting the development
budget for actual implementation instead of permanent test infrastructure or redundant proof.

| File | Verb | Use it to… |
|---|---|---|
| `00-orchestration-method.md` | ENGINE | fan out independent work, persist results, compose receipts, and stop cleanly. |
| `maintain-audit-reconcile.md` | MAINTAIN | reconcile code, Hub, live state, and docs; catch drift and regenerate the state anchor. |
| `improve-moe-review.md` | IMPROVE | run a multi-expert review and commit a grounded report. |
| `augment-hub.md` | AUGMENT | add an entity type or backfill governance across repositories. |
| `feature-buildout.md` | BUILD | run the DISCOVER → CLAIM → IMPLEMENT → ATTEMPT → RECORD loop. |
| `elevate-hub.md` | ELEVATE | build a visually exceptional, truthful, realtime, high-throughput Hub. |
| `verification-closer.md` | CLOSE | review one rare, named critical boundary and exit. |

## The non-negotiables

1. **The real attempt is the default proof.** If the changed operation succeeds, record it. If it
   breaks, that is sufficient notice and becomes work.
2. **No tests for non-critical or UI copy/style work.** Do not validate page copy, formatting,
   spacing, color, ordinary animation, or minor fixes with assertions, screenshots, or a verifier.
3. **Every justified test is transient.** Only rare critical boundaries merit a one-shot probe. Keep
   its receipt and delete its temporary artifact before commit; no permanent test or workflow remains.
4. **Receipts compose.** Parents inherit child receipts. Releases exercise only a new integration
   seam and never rerun all child proof.
5. **Verification never nests.** A closer cannot spawn another closer, suite, or proof ladder.
6. **Stop when the work works.** With the changed path successful and no critical boundary crossed,
   record completion and move immediately to the next valuable task.
7. **Failures become fresh input.** Delivery agents record observed failures for possible routing to
   a dedicated repair/error-fixing lane; they do not speculate or preemptively switch roles.
8. **Never clobber concurrent work.** Make targeted commits and leave another session's work intact.

## Placeholders

`{{PROJECT_KEY}}`, `{{BRAND}}`, `{{LIVE_URL}}`, `{{DEPLOY_CMD}}`, `{{REPO_PATH}}` — substitute for your
environment. Nothing here names a specific person, host, or project by design.
