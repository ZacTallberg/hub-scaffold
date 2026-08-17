# Operations guide

This guide covers the shipped filesystem/Django deployment. For first-time integration, follow
[MOUNTING.md](../adapters/django/MOUNTING.md); for request bodies and errors, use
[HUB-API.md](../adapters/django/HUB-API.md).

## Operational proof policy

The successful real operation is the default proof. Do not add or run tests for copy, wording,
style, motion, routine fixes, or other non-critical work, and do not install permanent tests,
fixtures, checker scripts, or CI verification workflows. A rare security, destructive-data,
migration, protocol, or concurrency boundary may use one temporary one-shot probe in disposable
scratch. Retain its receipt and delete the probe before commit. Completed receipts compose;
upgrades and releases inspect only a newly created critical integration seam. A real failure opens
a fresh repair task and may route to a dedicated error-fixing agent.

## Configuration reference

| Setting/environment | Default | Operational meaning |
|---|---|---|
| `HUB_PROJECT_KEY` | `{{PROJECT_KEY}}` | Stable lowercase id prefix; do not change after entities exist |
| `HUB_BRAND` | `{{BRAND}}` | Human-facing Hub title |
| `HUB_BUILD_STAMP` | `build_sha.txt` | `BASE_DIR`-relative running-build identity |
| `HUB_SETTINGS_FILE` | resolved Django settings module | File scanned by the focused AST safety audit |
| `HUB_WRITE_TOKEN` | empty | General writes disabled when empty; grants terminal board authority (done/deploy/ADR) when configured |
| `HUB_DONE_STRICTNESS` | `tracked` | `tracked` or `strict`; unknown values behave as `tracked` |
| `HUB_DIR` setting/environment | `BASE_DIR/PROJECT/.hub` locally | Canonical runtime ledger, index, leases, credentials, and grants; explicitly set to a writable durable mount in production |
| `HUB_ATTEST_SECRET` environment variable | generated `HUB_DIR/.attest-secret` | Optional stable launch-grant HMAC secret override; protect like a credential |
| `HUB_WORKER_LAUNCH_ENABLED` | `False` | Expose the optional launch mint and page control |
| `HUB_WORKER_PROTOCOL` | `hub-worker` | Validated custom URL scheme; invalid values fall back to `hub-worker` |
| `HUB_WORKER_LAUNCH_ISSUER_URL` | derived from request | Exact grant-consume URL; set explicitly in production |
| `HUB_WORKER_GRANT_TTL_S` | `120` | Short grant lifetime, additionally clamped by the grant core |

`HUB_PROJECT_KEY`, `BASE_DIR`, and schema paths are resolved when the adapter module imports. Set
them before Django starts; changing them in a live process is unsupported.

## First boot

```bash
python -m pip install -r requirements.txt
export HUB_WRITE_TOKEN='<generated secret>'
python manage.py migrate
python manage.py seedhub
```

`seedhub` is idempotent by entity id. A rejected seed exits nonzero; an existing id is skipped, not
updated. After genesis, use the served typed API rather than editing the event log or generated
views. `python -m hub_core.client` is the dependency-free command wrapper. Direct EventStore,
JSONL, or SQLite mutation is an offline recovery operation only: drain live writers first.

## Runtime storage

| Path under `HUB_DIR` | Purpose | Backup? |
|---|---|---|
| `events.jsonl` | Canonical event history | Yes—primary recovery artifact |
| `events.db`, `events.db-wal`, `events.db-shm` | Rebuildable SQLite index | Optional; quiesce writes for a consistent copy |
| `claims/*.json` | Expiring task leases | Usually no; losing them makes the durable `in_progress` task reclaimable |
| `.attest-secret` | Launch-grant signing secret | Yes if launch continuity matters; keep secret |
| `grants/*.used` | Consumed nonces | Retain at least through maximum grant lifetime |
| `grants/decisions.jsonl` | Launch grant/consume/refusal audit trail | According to audit policy |

Exact launch sidecar names are implementation details and may grow; in practice, back up the whole
`HUB_DIR` while preserving permissions. Never store `HUB_DIR` on ephemeral container storage unless
loss of the complete board is acceptable.

## Backup and restore

1. Stop or drain Hub writers, or take a filesystem snapshot with atomic snapshot semantics.
2. Copy the whole `HUB_DIR` to protected storage.
3. During an explicitly scoped disaster-recovery operation, restore into a disposable separate
   path, set `HUB_DIR`, and open the EventStore.
4. Because restore is a destructive-data boundary, use one decisive integrity observation such as
   `hubaudit`, retain its receipt, and delete the disposable restored copy before commit.
5. Start serving only after the actual restored board matches the pre-loss record.

`events.db` may be deleted from an offline restored copy; the EventStore rebuilds it from
`events.jsonl`. Do not “repair” JSONL manually. A non-final malformed line is treated as corruption;
an incomplete final line is quarantined by truncating to the last complete event when the store
opens.

## On-demand ledger diagnosis

```bash
python manage.py hubaudit
python manage.py hubaudit --json
```

Use this only when ledger integrity, migration, or recovery is actually in scope; it is not a
routine completion gate or a standing CI job. The management command returns:

- `0` for pass or warn-only;
- `2` for critical/high violations;
- `1` for an internal audit error.

The JSON payload's internal `exit_code` uses `3` for warn-only even though the management command
maps that state to process exit `0`. An explicitly scoped caller may parse JSON if amber needs
distinct handling.

The audit checks schema validity, dangling references, ADR numbering, event-chain integrity,
build/deploy coherence, focused Django settings safety, and explicit guards on Hub mutation routes.
It does not prove backups, TLS, authorization in front of reads, the live front door, or alert
delivery. When one of those is a critical boundary for the current task, perform the real operation
or use one disposable probe and retain only its receipt.

## Build coherence

For a checkout, the adapter reads Git HEAD. Without `.git`, it reads `HUB_BUILD_STAMP` from the
artifact. `PROJECT/state.json` supplies `last_deploy_sha`; a caller may add `?served=<sha>` to the
snapshot request to compare an independently observed live SHA.

Before the first deploy, a missing deploy record is warn-only. A production process with neither
Git metadata nor a build stamp is blocking because its running identity is unknowable. The deploy
pattern shows how to stamp and probe an artifact; wire the real ship and alert mechanisms yourself.

A newly swapped exact artifact and its immutable task-bearing deploy record have one intentional
ordering edge: the task must be done before `tasks_closed` may name it, while `coherence:repo`
cannot clear until that record exists. After an exact independent canary, the release driver may
classify **only** that single prior-SHA/new-head finding as `closure_pending`, complete the named
task, append the exact closure, and then require one post-record audit with zero critical/high
findings. Any additional finding blocks, and no immutable record is rewritten. The full sequence is
in `patterns/deploy-runbook.md`.

## Token rotation

1. Generate a new high-entropy token in the secret manager.
2. Update the Hub service and restart/reload it.
3. Update trusted API clients and each workstation token file.
4. Confirm the old token receives `403` and the new token succeeds on a harmless, intended call.
5. Remove old secret versions according to local policy.

There is no overlap set or per-client revocation: the configured token is the one accepted token.

## Queue and lease recovery

A successful claim durably transitions a `todo` task to `in_progress` and returns a fencing token.
`/hub/next.json` excludes every task with a live lease. If a worker exits without completing, the
task stays visibly `in_progress`; after its lease expires (or lease sidecars are lost), discovery
returns it with `stale_reclaim:true` so another worker can reclaim it. A same-agent claim retry
renews the existing token; it does not rotate the token behind that worker. Completion rechecks the
token at the final append and releases its lease on success.

Claim and heartbeat TTLs are limited to 1–86400 seconds. Choose a TTL longer than routine work
between heartbeats, heartbeat well before expiry, and treat `409 no/stale lease` as loss of
ownership. Do not keep working or attempt completion after losing the lease; rediscover and claim.

`POST /hub/api/take` may include a `worker` placement profile. The Hub first derives the ordinary
ready frontier, then filters explicit task routing requirements against that profile. A
`409 no_compatible_task` response includes exclusion counts and reasons; it means ready work exists
but this worker did not prove the required fit. Correct the worker declaration or let another seat
pull it; do not weaken the task requirement merely to make the queue non-empty.

## Worker-launch operations

Follow [the Windows adapter guide](../adapters/windows/README.md) for installation and removal. A
healthy click moves through four observable stages: grant mint, custom-protocol handoff,
authoritative consume, then wrapper start. Failure before consume must start no process.

If a browser reports that it cannot reach the Hub, verify the issuer URL, HTTPS reachability from
the workstation, the local token file, and whether the token was rotated. If clicking does nothing,
verify the per-user protocol registration and browser permission for external protocols. If windows
remain open, the operator wrapper is still running or is spawning a detached child; the adapter does
not use `-NoExit` and closes its own host when the wrapper returns.

## Upgrade procedure

1. Back up `HUB_DIR` and record the current commit/build.
2. Review changes to schemas and adapter settings, reusing completed child-task receipts.
3. Only when the upgrade crosses a critical migration, destructive-data, protocol, security, or
   concurrency boundary, exercise that one seam with a disposable ledger copy or temporary probe;
   retain its receipt and delete all scratch before commit.
4. Deploy from a committed SHA and perform the changed real operation on the mounted project.
5. Record the observed live build stamp and outcome. Do not rerun child proof or a broad suite.
6. Keep the prior artifact and backup available for rollback; do not rewrite the event log to
   downgrade projected state.

## Troubleshooting quick reference

| Symptom | Likely cause / check |
|---|---|
| All writes return `403` | Token unset, header missing, or client/server tokens differ |
| Update returns `428` | Existing entity update omitted `expected_version` |
| Claim returns `409 held` | Another worker owns a live lease; choose another discovered task |
| Atomic take returns `409 no_compatible_task` | Ready work exists, but capability/risk/resource/locality/outcome requirements exclude this worker; inspect `routing.excluded_by_reason` |
| `in_progress` task appears in `next.json` | Its lease is absent/expired; it is an intentional stale reclaim candidate |
| Heartbeat returns `409 no/stale lease` | Ownership expired or was reclaimed; stop and rediscover |
| Completion returns `409 must_claim` | Claim first and retain its fencing token |
| Completion returns `422 evidence_unresolvable` | Strict mode cannot fetch/find one evidence item |
| Completion returns `422 need_verification_run` | This task explicitly declared a one-shot critical probe; run it out-of-band, submit its typed receipt, and delete the temporary probe before commit |
| Completion returns `422 bad_verification_run` | The exceptional receipt does not match the declared action/agent or reports failure; open a fresh repair task from the observed failure |
| Audit reports `coherence:unknown` | No Git/build stamp or no first-deploy record |
| Schema load fails after copy | `PROJECT/schema/` is absent or does not match the adapter's `BASE_DIR` |
| Different workers see different boards | Server processes are using different `HUB_DIR` paths |
| Connected cockpit trails the ledger | A writer bypassed the served API, or a multi-process deployment lacks/has lost its shared broker; restore the single live write entrance or broker, then reconnect once for cursor recovery |
| Launch grant immediately fails | Issuer mismatch, expiry, changed action/task/count, replay, or token mismatch |
