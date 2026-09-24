# Deploy hardening — the step failures that roll back good builds or hide bad ones

`deploy-contract.md` makes "deployed" a fact; `deploy-runbook.md` is how an agent executes it.
This page is the set of deploy-STEP defects that survive both: each one was a real outage on the
system this scaffold was extracted from, each looked like correct fail-closed behaviour from the
inside, and each is a rule you can hold in whatever deploy mechanism you run. There is no script
here on purpose (see `AGENTS.md`); the rules are the portable part.

## 1. Secret/config sync: a transport failure is not the source answering

A deploy that pulls its runtime configuration (a secret store, a config service, a vault) before
starting the new release must fail closed when the source REFUSES — a revoked grant, a missing
entry. It must not fail closed when the source merely could not be REACHED, or a busy minute
becomes a rollback.

- **Retry transport failures only**: connect timeout, refused, reset, DNS — three attempts, a few
  seconds apart. An HTTP answer is the source speaking and is acted on at once, so the fail-closed
  logic keeps its meaning.
- **A gateway 5xx (502/503/504) is transport, not an answer.** It is the proxy speaking for a
  source it cannot reach. Treating it as "the store refused" rolled a host back while that host
  already held every value it needed. A 403 or a 500 from the source itself is still an answer.
- **Unreachable keeps what is there.** A host whose configuration is already populated keeps its
  values and deploys; a host with NO configuration stops. The broad "list everything" read gets the
  same rule as the per-entry read.

Measured shape of the bug: one timeout on the first read under CI load rolled a healthy app back
to its previous commit; a "service is starting" 502 did the same a week later.

## 2. The pre-migration snapshot: no success marker without a snapshot

- **Fail loudly on a failed or empty copy.** A snapshot step that runs with errors downgraded to
  warnings will print its success line after a copy that never happened. Stop on the first error,
  check the copy is non-empty, and print the success marker only after both.
- **Prune BEFORE you write, or check free space first.** "Copy, then keep the newest N" never
  prunes again once the disk is full: every copy fails, the prune (being after the copy) never
  runs, and every deploy on the host rolls back until a person frees space. A fallback location on
  the same volume as the thing it protects keeps fewer copies than the real vault.

## 3. The deploy record: a fail-soft post must not fail silently

The step that posts the release record (`POST /hub/api/deploy`) is usually fail-soft, correctly —
a board outage must not block a release. That makes its failures invisible: on the origin system a
runtime upgrade removed the API the script used to trust an internal CA, the script kept running,
and every record was lost at the TLS handshake while every pipeline stayed green. The portfolio,
the rollback target and attribution quietly stopped being populated.

- Choose TLS trust by what the RUNTIME supports (feature-detect, or branch on the interpreter's
  edition), and pass version-specific options in a way that does not break the other version.
- Prove the post against a TLS endpoint shaped like production (self-signed if yours is), not over
  plain HTTP where the defect cannot show.
- Print a distinct `RECORD_POSTED` / `RECORD_FAILED` line and forward the failure to the error
  stream (`patterns/error-visibility.md`).

## 4. The readiness gate: the app's own account of its wiring, adopted on evidence

Liveness says the process answers; readiness says it can do its job (database, queues, the
services it calls). A verify step should read the app's readiness endpoint, and **a failed gate
refuses the deploy record** — a deploy that cannot prove its readiness must not appear verified.

- **Enumerate the checks from the endpoint's source at the deployed revision**, not from one
  reading of its payload: a single payload cannot tell you what the endpoint is capable of
  emitting, and adopting on a point-in-time payload reddens healthy apps.
- **A dependency the app does not own is advisory.** A check that probes a shared server turns
  somebody else's outage into a refused build. Report it; do not gate on it.
- **Classify the shape before counting.** "Inspected nothing" (an empty or unrecognised payload)
  is not "found no failure"; a gate that counts failures over zero checks passes on a broken
  endpoint.

## 5. Failure outcomes must reach the board

A release record is written by the job that runs after verification passes, so it can only ever
say "ok". The outcomes that matter most — a failed deploy, a host ROLLED BACK to the previous
commit — need a channel of their own:

```bash
# from the deploy step, on failure or after an automatic restore
HUB_API_BASE=https://hub.example.com/hub HUB_CI_WEBHOOK_SECRET=... \
python -m hub_core.client ci-report --kind deploy --project budget-app --status rolled_back \
  --sha "$REJECTED_SHA" --restored-sha "$RUNNING_SHA" --ref main
```

A rollback lands as a critical row that no later green retires — a person closes it, because the
host is running code the pipeline rejected. Pipeline and job results arrive the same way from the
CI system's own webhook (`POST /hub/api/ci-event`, see `adapters/django/HUB-API.md`), and a later
green on the same job and ref retires the red.

## 6. Every project's CI is wired, and "cannot see" is never "ok"

A webhook nobody registered makes a project dark, and a dark project's empty CI section reads as
green. Register hooks per project with a create-only reconciler run on a schedule
(`adapters/gitlab/ensure_ci_hooks.py` for GitLab): it creates missing hooks and enables events on
hooks that already point at the hub, never deletes or edits anything else, and reports a project
whose hooks it cannot list as `unknown` — never `ok` — with a non-zero exit. Two traps it is built
around: group-level hooks can exist, pass a manual test, and never fire on a tier that does not fan
them out; and an API answers a list when it succeeds and an object when it refuses, so code that
iterates the answer without checking its shape crashes exactly when it is needed.
