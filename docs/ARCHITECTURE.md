# Architecture and guarantees

This document describes the system that is shipped in this repository. The files under
`PROJECT/`, `campaigns/`, and `patterns/` also contain normative operating practices; those
practices become mechanical guarantees only after an adopter wires the named gate, monitor, or
workflow.

## System map

```text
operator / trusted agent
        |
        | X-Write-Token (general writes)
        v
Django adapter at /hub -------------- unauthenticated GETs ----------> browser / readers
        |                                                               (whole snapshot)
        | validate, OCC, lease checks, completion gate
        v
hub_core EventStore
        |-- PROJECT/.hub/events.jsonl   canonical append-only event log
        |-- PROJECT/.hub/events.db      rebuildable SQLite index
        |-- PROJECT/.hub/claims/        expiring task leases
        `-- PROJECT/.hub/grants/        optional launch grant state
                 |
                 v
        deterministic fold + derived graph/flags
                 |
                 +--> /hub and JSON read API
                 +--> computed audit
                 `--> optional materialized JSON/Markdown projections
```

The optional Windows worker path is separate from general browser operation:

```text
Hub page --same-origin CSRF mint--> short-lived signed grant
    |                                  |
    `-- hub-worker:// navigation ------> workstation handler
                                           |
                                           | exact issuer + local token file
                                           v
                                  token-gated grant consume
                                           |
                                           `--> operator-supplied worker wrapper
```

The browser never receives the Hub write token. The workstation consumes a launch grant at the
Hub that issued it before it starts a process.

## Components

| Component | Responsibility | Dependencies |
|---|---|---|
| `hub_core/` | Event storage, folding, schemas, derivations, audit core, rendering, launch grants | Python standard library |
| `adapters/django/hub/` | `/hub` page, read/write HTTP APIs, Django-specific audit adapters, management commands | Django 5.2 or 6.0 |
| `adapters/windows/` | Optional per-user `hub-worker://` handler and worker lifecycle | Windows PowerShell |
| `PROJECT/` | Portable management-plane templates and schemas | Plain files |
| `patterns/` | Opt-in deploy, canary, repository, and agent guard patterns | Adopter wiring |
| `campaigns/` | Human/agent operating playbooks | An operator or orchestration harness |
| `example/` | Runnable strict-mode integration fixture in the source scaffold (not stamped by `init.sh`) | Django |

## Canonical state and projections

`PROJECT/.hub/events.jsonl` is the canonical Hub ledger. Each event includes a monotonic sequence,
aggregate version, previous hash, and a hash over a fixed canonical field set. The SQLite file is a
transactional index for optimistic concurrency and idempotency; it is reconciled from JSONL when an
`EventStore` opens. Claiming emits the durable `todo` to `in_progress` transition; the expiring lease
and consumed launch nonces remain runtime sidecars rather than entity events.

Folding is last-write-wins per aggregate field. The fold materializes seven entity types:
`task`, `adr`, `feat`, `gap`, `cap`, `deploy`, and `note`. Decision-log events remain in the event
stream but are not projected as an entity collection. Derived state includes dependency edges,
dangling references, task actionability, back-references, counts, phases, and feature coverage.

The following are projections, not independent stores:

- `/hub/` and `/hub/hub.json`;
- collection and entity JSON endpoints;
- computed audit results;
- files produced by `hub_core.projections` (`TASKS.md`, `FEATURES.md`, `ADR.md`, `DEPLOYS.md`,
  `CHANGELOG.md`, `hub.json`, and `state.json`).

## Write path

General HTTP writes pass through one gate:

1. require `POST` and a constant-time-matched `X-Write-Token`;
2. parse JSON;
3. merge an update with the current aggregate;
4. require `expected_version` for updates;
5. validate the merged entity against its schema;
6. append with aggregate-scoped idempotency and optimistic concurrency.

Task completion has additional steps: a live lease, an acceptance note, evidence, optional or
required proof checks according to `HUB_DONE_STRICTNESS`, execution of any stored
`verification_command`, and a recomputed audit that refuses completion on critical violations.
The final append rechecks the fencing token under the cross-process claim lock and uses the version
whose command was verified, preventing an expired worker or concurrently changed task from landing
an obsolete completion.
See [HUB-API.md](../adapters/django/HUB-API.md) for the exact contract.

## Enforcement matrix

| Claim | Shipped enforcement | Important boundary |
|---|---|---|
| Event history is tamper-evident | Hash-chain verification; SQLite update/delete triggers | Hashing detects mutation; it does not prevent deletion by a host administrator. Backups are still required. |
| Concurrent entity updates do not silently overwrite | `expected_version` plus a serialized SQLite writer | New aggregates allow no precondition; filesystem/runtime storage must be shared by all server processes. |
| One worker owns a task | Cross-process claim lock, expiring lease/fencing token, durable `in_progress` transition, and live-lease filtering in discovery | Claim files are runtime state and must be on the same shared filesystem as the Hub. Missing/expired leases make abandoned `in_progress` work reclaimable. |
| Direct `status=done` is refused | Only `/api/complete` can grant done | `tracked` mode records evidence but does not dereference it or require a command. |
| Strict completion proves more | Evidence dereference plus a typed exit-0 receipt the WORKER produced | The hub never runs the command itself; the token grants terminal board authority, not a shell. See [SECURITY.md](../SECURITY.md). |
| Unsafe Django defaults are visible | AST audit for DEBUG, literal SECRET_KEY fallback, and `ALLOWED_HOSTS="*"` | This is a focused audit, not a complete Django deployment check. |
| Mutating Hub routes have an explicit guard | URL resolver audit recognizes `@writer` or the narrow origin-gated mint | Reads are deliberately unauthenticated and expose whatever entities contain. |
| Running build matches deploy record | Git/build stamp and `PROJECT/state.json` comparison; optional `served` observation | The Hub does not deploy or continuously probe production by itself. Adopt the deploy/canary patterns. |
| Browser launch cannot mint general write authority | CSRF mint; signed bounded grant; token-gated single-use consume | Enabling process launch is a privileged local capability and requires workstation installation. |

## What is not automatic

The base package does not provide user accounts, per-project authorization, encryption at rest,
network isolation, a production WSGI server, a deployment provider, alert delivery, backup
scheduling, a continuously running conformance scanner, or the project-specific verification
harness described by `PROJECT/verify/`. The patterns and templates specify those roles but do not
make them active merely by existing in a repository.

Independent verification is also not a standing requirement for every task. The operating model
uses truthful implementer evidence for routine low-risk work and dispatches a fresh disposable
closer at releases, privileged/data/public-contract boundaries, regressions, and occasional samples.

The Hub also does not redact entity fields on read. If `/hub` is reachable, its complete board is
readable without authentication. Put only publishable data in the ledger or add authentication at
the reverse proxy/application boundary.

## Portability boundaries

The core is filesystem-backed and assumes one shared durable `HUB_DIR`. Multiple Django processes
may share it because SQLite serializes event writers and claim/launch sidecars use cross-process
locks. Multiple independent machines must not use separate copies of the same ledger; use a shared
filesystem with appropriate semantics or replace the store adapter.

The Django adapter expects `BASE_DIR/PROJECT/schema/` and resolves evidence paths and verification
commands relative to `BASE_DIR`. `HUB_DIR` can relocate runtime state, which is useful for durable
mounts and tests. The optional launcher currently ships a Windows registration adapter; the grant
protocol itself is Python and vendor-neutral.
