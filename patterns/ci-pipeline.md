# CI pipeline rules for a forward-only deploy

`deploy-contract.md` says what must be true before anyone may say "deployed"; `deploy-runbook.md`
is how an agent satisfies it by hand. When an adopter drives the same laws from a CI system, five
pipeline-level rules decide whether deploys land at all. Each was learned from a pipeline that was
green, or quiet, while nothing shipped. Examples use GitLab CI syntax because it is compact; every
rule has an equivalent in other CI systems.

## 1. Never auto-cancel a pipeline that is still waiting to deploy

Many CI systems cancel a pending pipeline when a newer commit arrives. With a busy runner pool
that means a run of pushes each kill the previous pipeline before any job starts, and nothing
deploys for as long as main keeps moving — while every individual pipeline reads "canceled",
which nobody treats as a failure.

If the deploy step is **forward-only** — it ships a commit only if that commit is ahead of what the
host serves, and exits as superseded otherwise (Law 1–3 plus the per-target lease in
`deploy-contract.md`) — an older pipeline surviving is a no-op, never a rollback. Then turn
auto-cancel off for the pipelines that deploy:

```yaml
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" || $CI_PIPELINE_SOURCE == "api"'
      auto_cancel:
        on_new_commit: none
    - when: always          # every other source keeps the platform default
```

An operator's API-triggered pipeline is never redundant with a push either, so it survives too.

Only do this once the deploy step is genuinely forward-only; without that property, a surviving
older pipeline IS a rollback.

## 2. Change-triggered jobs fire on a push, not on every pipeline source

Path filters ("run when these files changed") are commonly evaluated as **true** for a pipeline
with no push event — a schedule, an API trigger, a manual run. An expensive job keyed to a
source path then runs beside every nightly backup and every API-triggered pipeline, and can queue
ahead of deploys on a shared lock. Require the push source alongside the path filter; keep
explicit opt-in variables working from any source:

```yaml
expensive_eval:
  rules:
    - if: '$RUN_EVAL == "1"'                    # explicit request: any source
    - if: '$CI_PIPELINE_SOURCE == "push"'
      changes: [retrieval/**/*]
```

## 3. A job's timeout counts the whole job — size it from the slowest runner

The job timeout covers fetching sources and starting the shell, not just your script. Measured on
a loaded runner, the fetch and shell start consumed most of a ten-minute budget before the first
script line printed, and a verification that passed every check was killed before it could post
the deploy record. Size a ceiling from the slowest observed fetch + shell start + the script's own
bounded retries; a hung job holding a lock a little longer is the cheaper of the two ways to be
wrong. Read a red job's **failure reason** before its log tail: `job_execution_timeout` is not a
failed check.

## 4. Publications derived from the repository ride the deploy, from the protected ref

Anything the running system serves that is *authored* in a repository — doctrine, a charter core,
a runbook shown on the board, a feature-flag file — drifts from the repository the moment
publishing it is a manual step: the repo looks authoritative and the running system keeps serving
the previous version indefinitely. Publish it on every protected-branch deploy, and:

- **Read it from the protected ref, never from a working tree on the host.** A shared checkout's
  state belongs to nobody; publishing whatever happens to sit in it can restore content
  superseded hours ago. Fetch the ref into a scratch location with the job's own credential (no
  prompts, no cached credential helper), and materialize the file with the VCS's own checkout
  rather than by capturing `show` output through a shell that may re-decode it.
- **Make it idempotent.** An unchanged body writes nothing, so the published version counts
  changes to the content, not deploys.
- **Make it loud.** Print what was published and from which commit (`PUBLISH_SOURCE sha=…`,
  `PUBLISH_OK` / `PUBLISH_UNCHANGED version=…`), and exit non-zero with a named marker on any
  failure. Mark the job allowed-to-fail *relative to the deploy* — stale content must never roll
  back a good release — but route its failure into the Hub's error stream
  (`python -m hub_core.client ci-failure …`) so "allowed to fail" never becomes "failed forever
  in silence".

## 5. Verification gates the proof, not the full history

Verification selection follows `docs/TESTING.md`: the real operation is the proof, a transient
probe exists only for a named critical boundary, and a pipeline never replays a standing suite
because a deploy is happening. A deploy pipeline that queued behind another may reuse the
receipts already recorded for an ancestor it contains instead of re-running them.

## Reporting a failed job

Post the failing job's log tail to the Hub (`python -m hub_core.client ci-failure --project …
--job … --trace-file -`, or the `report_ci_failure` MCP tool). The Hub classifies what the LOG says
— rolled back, the job's own checks failed, stopped before deploying, or unclear — and files one
operational row at that severity; the pipeline's trigger is only ever a hint.
