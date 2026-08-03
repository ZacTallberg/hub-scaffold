# Operations guide

This guide covers the shipped filesystem/Django deployment. For first-time integration, follow
[MOUNTING.md](../adapters/django/MOUNTING.md); for request bodies and errors, use
[HUB-API.md](../adapters/django/HUB-API.md).

## Configuration reference

| Setting/environment | Default | Operational meaning |
|---|---|---|
| `HUB_PROJECT_KEY` | `{{PROJECT_KEY}}` | Stable lowercase id prefix; do not change after entities exist |
| `HUB_BRAND` | `{{BRAND}}` | Human-facing Hub title |
| `HUB_BUILD_STAMP` | `build_sha.txt` | `BASE_DIR`-relative running-build identity |
| `HUB_SETTINGS_FILE` | resolved Django settings module | File scanned by the focused AST safety audit |
| `HUB_WRITE_TOKEN` | empty | General writes disabled when empty; command-execution-grade when configured |
| `HUB_DONE_STRICTNESS` | `tracked` | `tracked` or `strict`; unknown values behave as `tracked` |
| `HUB_DIR` environment variable | `BASE_DIR/PROJECT/.hub` | Canonical runtime ledger, index, leases, and grant sidecars |
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
python manage.py seedhub --dry-run
python manage.py seedhub
python manage.py hubaudit
```

`seedhub` is idempotent by entity id. A rejected seed exits nonzero; an existing id is skipped, not
updated. After genesis, use the typed API rather than editing the event log or generated views.

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
3. Test restoration into a separate path by setting `HUB_DIR` and opening the EventStore.
4. Run `hubaudit`; require a valid chain and expected entity counts.
5. Start serving only after the restored board has been compared with the pre-loss record.

`events.db` may be deleted from an offline restored copy; the EventStore rebuilds it from
`events.jsonl`. Do not “repair” JSONL manually. A non-final malformed line is treated as corruption;
an incomplete final line is quarantined by truncating to the last complete event when the store
opens.

## Health and audit

```bash
python manage.py hubaudit
python manage.py hubaudit --json
```

The management command returns:

- `0` for pass or warn-only;
- `2` for critical/high violations;
- `1` for an internal audit error.

The JSON payload's internal `exit_code` uses `3` for warn-only even though the management command
maps that state to process exit `0`. CI should parse JSON if amber needs distinct handling.

The audit checks schema validity, dangling references, ADR numbering, event-chain integrity,
build/deploy coherence, focused Django settings safety, and explicit guards on Hub mutation routes.
It does not test backups, TLS, authorization in front of reads, the live front door, or alert
delivery. Those require deployment-specific probes.

## Build coherence

For a checkout, the adapter reads Git HEAD. Without `.git`, it reads `HUB_BUILD_STAMP` from the
artifact. `PROJECT/state.json` supplies `last_deploy_sha`; a caller may add `?served=<sha>` to the
snapshot request to compare an independently observed live SHA.

Before the first deploy, a missing deploy record is warn-only. A production process with neither
Git metadata nor a build stamp is blocking because its running identity is unknowable. The deploy
pattern shows how to stamp and probe an artifact; wire the real ship and alert mechanisms yourself.

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
2. Review changes to schemas and adapter settings.
3. Run `python tools/build_bootstrap.py --check` and the complete self-test in the new checkout.
4. Test the mounted project against the new code with a copy of its ledger.
5. Deploy from a committed SHA, run `hubaudit`, and verify the live build stamp.
6. Keep the prior artifact and backup available for rollback; do not rewrite the event log to
   downgrade projected state.

## Troubleshooting quick reference

| Symptom | Likely cause / check |
|---|---|
| All writes return `403` | Token unset, header missing, or client/server tokens differ |
| Update returns `428` | Existing entity update omitted `expected_version` |
| Claim returns `409 held` | Another worker owns a live lease; choose another discovered task |
| `in_progress` task appears in `next.json` | Its lease is absent/expired; it is an intentional stale reclaim candidate |
| Heartbeat returns `409 no/stale lease` | Ownership expired or was reclaimed; stop and rediscover |
| Completion returns `409 must_claim` | Claim first and retain its fencing token |
| Completion returns `422 evidence_unresolvable` | Strict mode cannot fetch/find one evidence item |
| Completion returns `422 verify_failed` | Stored verification command exited nonzero; inspect returned stderr tail |
| Completion returns `422 audit_unsound` | Critical chain/schema integrity issue; run JSON audit before retrying |
| Audit reports `coherence:unknown` | No Git/build stamp or no first-deploy record |
| Schema load fails after copy | `PROJECT/schema/` is absent or does not match the adapter's `BASE_DIR` |
| Different workers see different boards | Server processes are using different `HUB_DIR` paths |
| Launch grant immediately fails | Issuer mismatch, expiry, changed action/task/count, replay, or token mismatch |
