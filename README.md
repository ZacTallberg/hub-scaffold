# hub-scaffold

Every new or upgraded Hub is governed by [`PROJECT/HUB-QUALITY.md`](PROJECT/HUB-QUALITY.md): visual
excellence, realtime truth, accessible interaction, measurable flow, and durable agent coordination
are one product contract. The real Hub experience is the proof: use it, observe it, and record the
result without creating a permanent verifier.

`hub-scaffold` is a portable project operating system built around an append-only task/decision
ledger. It combines:

1. an event-sourced Hub (`hub_core/` plus a Django adapter);
2. a durable, content-agnostic management plane (`PROJECT/`);
3. opt-in enforcement patterns for deployment, canaries, repository hygiene, and agent safety;
4. campaign playbooks for maintaining, reviewing, extending, and building from the board.

The design goal is not more ceremony. The default tracking loop is deliberately small—discover,
claim, implement, complete with a note and evidence—while a temporary focused probe is reserved for
a rare critical boundary.

## What is implemented

- Canonical events in `PROJECT/.hub/events.jsonl`, hash-chained and append-only.
- A rebuildable SQLite index with optimistic concurrency and aggregate-scoped idempotency.
- JSON-Schema-validated tasks, durable AgentRuns, ADRs, features, gaps, capabilities, deploys, and notes.
- Derived dependency/urgency state, graph, collections, audit, and a browser dashboard.
- A typed write API with scoped revocable agent credentials, expiring fenced task leases, atomic
  failure-to-repair routing, and a guarded completion transition. Shared-root auth is an explicit
  migration bridge, not ordinary worker identity.
- A dependency-free live-board client (`python -m hub_core.client`) that keeps agent mutations on
  that HTTP seam. It never opens the ledger: durable append and realtime wake remain one operation
  instead of letting a connected cockpit silently lag an out-of-process write.
- Event-sourced AgentRuns carry commands, messages, checkpoints, input, handoffs, cooperative
  cancellation, recovery envelopes, and composed child receipts; current MCP Tasks methods expose
  the real durable lifecycle without advertising phantom A2A or notification transports.
- Compatibility-first atomic pull: optional task capability/risk/resource/locality/outcome
  requirements filter workers before quality/latency/cost preference scoring inside the canonical
  ready frontier.
- A focused computed audit for schemas, references, ADR numbering, event integrity, build
  coherence, selected Django safety settings, and mutation-route guards.
- A minimal mounted Django example in which real Hub operations can be exercised.
- A production-shaped ASGI entrypoint for immediate, long-lived push delivery. Mutations publish
  canonical patches directly; reconnect cursors repair interruption without a polling freshness loop.
- An optional Windows one-click worker launcher that keeps the general write token out of the
  browser and closes its host window when the configured wrapper finishes.

The plane, patterns, and campaigns also specify controls that require adopter wiring. Merely copying
a deploy pattern, verification contract, or conformance-scanner specification does not activate it.
The exact shipped-versus-normative boundary is documented in
[Architecture and guarantees](docs/ARCHITECTURE.md).

## Security boundary—read before deploying

Hub reads are unauthenticated by default and expose the complete projected board. “Public read” does
not mean automatically redacted: keep sensitive material out of entities or add an authentication
boundary.

Normal workers use short-lived, revocable, scope-bearing `X-Agent-Token` credentials whose immutable
subjects are bound into task leases and canonical event provenance. The legacy `HUB_WRITE_TOKEN`
is an explicitly labeled, disable-able shared-root migration bridge; a holder can grant `done`,
record deploys, and rule ADRs. Neither mode is code execution: the Hub never runs an
optional critical-boundary `verification_command` — the worker runs it out-of-band and submits a
typed exit-0 receipt the Hub validates. Ordinary tasks carry no command. Strict evidence URLs ARE
still fetched by the server. Treat the token as production credentials,
give consequential scopes only to trusted operators/agents, and isolate the service accordingly. The optional browser
launcher never receives it. Read [SECURITY.md](SECURITY.md) before enabling writes or worker launch.

## Documentation map

| Need | Start here |
|---|---|
| Understand components, storage, flows, and guarantees | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Threat model, trust boundaries, deployment checklist | [SECURITY.md](SECURITY.md) |
| Mount the Hub into Django | [adapters/django/MOUNTING.md](adapters/django/MOUNTING.md) |
| Upgrade an existing adopter without replacing its identity or ledger | [docs/ADOPTER-UPGRADE.md](docs/ADOPTER-UPGRADE.md) |
| Operate the HTTP API | [adapters/django/HUB-API.md](adapters/django/HUB-API.md) |
| Run, back up, restore, rotate, or troubleshoot | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Apply the transient proof policy | [docs/TESTING.md](docs/TESTING.md) |
| Install the optional Windows worker adapter | [adapters/windows/README.md](adapters/windows/README.md) |
| Understand the human/agent laws | [OPERATING-AGREEMENT.md](OPERATING-AGREEMENT.md) |
| Run a maintenance/build/review campaign | [campaigns/README.md](campaigns/README.md) |
| Bootstrap or rebind the management plane | [PROJECT-PLANE-BOOTSTRAP.md](PROJECT-PLANE-BOOTSTRAP.md) |
| See scaffold release history | [CHANGELOG.md](CHANGELOG.md) |
| Contribute safely | [CONTRIBUTING.md](CONTRIBUTING.md) |

Agents should read [AGENTS.md](AGENTS.md) first.

## Repository layout

```text
hub_core/                       framework-free event store, fold, audit, rendering, grants
adapters/django/hub/            mounted Django app and management commands
adapters/windows/               optional per-user local worker bridge
PROJECT/                        canonical management-plane templates and entity schemas
example/                        runnable Django integration example
patterns/                       opt-in deploy/canary/repository/agent patterns + worker longevity
campaigns/                      operating playbooks
governance/                     agent-rule templates installed by init.sh
docs/                           architecture, operations, and transient-proof guidance
tools/                          bootstrap, scrub, and documentation maintenance utilities
PROJECT-PLANE-BOOTSTRAP.md      standalone plane specification with embedded templates
```

## Quickstart: use the real artifact

Prerequisites are Git, Bash, and Python. `hub_core` supports Python 3.10+ with no third-party
runtime dependencies. The example needs Django; install the repository requirements, boot the
example, and perform the Hub operation you care about. That real operation is the default proof.

Do not create tests for copy, wording, spacing, color, animation polish, or routine non-critical
fixes. If the real operation succeeds, record the natural receipt and stop. Only a rare
security/authorization, destructive/data-integrity, migration, protocol-compatibility, or
concurrency boundary may justify a focused test. Such a probe lives in temporary space, runs once,
leaves its receipt, and is deleted before commit. See [the proof policy](docs/TESTING.md).

If the real operation fails, capture it as fresh Hub task input. It can route to a dedicated
repair/error-fixing lane later; the delivery agent stays with its claimed work and does not
speculatively become the repair agent.

Completed task receipts compose upward. A release inherits them and may prove only the newly
created integration seam when that seam is itself critical; it never reruns every child task or
fans out through nested verifiers. There is intentionally no automatic test workflow.

### Run the literal-realtime example

The example includes both Django entrypoints. `runserver` is a convenient WSGI compatibility
preview; the live Hub is meant to run through ASGI so its event connection stays open without a
polling cycle. Install the process server you prefer separately, then start the reference Uvicorn
path from `example/` with `DEBUG=1` and `HUB_WRITE_TOKEN` set in the environment:

```bash
python -m pip install uvicorn
cd example
python -m uvicorn example_site.asgi:application --host 127.0.0.1 --port 8000
```

Add `--reload` only for local code editing. Hypercorn or Daphne can serve the same ASGI callable.
For production, pin the chosen server in the adopting app, terminate TLS at the deployment edge,
and disable reverse-proxy buffering/caching for `text/event-stream`; see the
[ASGI mounting contract](adapters/django/MOUNTING.md#5-serve-the-live-hub-through-asgi).

### Operate a running board without bypassing realtime

Once a Hub service is running, every agent and operator mutation goes through its served HTTP API.
Do not import `EventStore`, call `hub_app.store()`, edit `events.jsonl`, or open `events.db` from a
side process to change an active board: those are offline recovery surfaces and cannot wake the
process that owns a connected stream. The small standard-library client makes the correct path the
fast path:

```bash
export HUB_API_BASE=http://127.0.0.1:8000/hub
export HUB_AGENT_TOKEN='<scoped worker credential>' # HUB_WRITE_TOKEN is the migration fallback
python -m hub_core.client create --title "Ship the export" --acceptance "The live export works" --priority P1
python -m hub_core.client claim example:task:0001 --agent worker-1
HUB_LEASE_TOKEN='<returned fencing token>' python -m hub_core.client complete example:task:0001 \
  --agent worker-1 --accept-note "The live export returned its artifact" \
  --evidence http://127.0.0.1:8000/export/latest
```

The command response is the operation receipt; the open cockpit receives the same mutation by push.
Direct ledger access is permitted only while writers are drained for an explicit backup, restore,
or disaster-recovery operation.

## Stamp a new project

`init.sh` requires a new or empty target directory and creates a Git repository with a genesis
commit:

```bash
bash init.sh ../my-project my-project "My Project" https://my-project.example.com
```

It copies `.gitignore`, `PROJECT/`, `hub_core/`, adapters, patterns, the operating/security agreements, and the
architecture contract; installs the governance templates as `CLAUDE.md` and `AGENTS.md`; substitutes `{{PROJECT_KEY}}`, `{{BRAND}}`, and
`{{LIVE_URL}}`; and fails if a placeholder survives. It does not merge into an existing project or
mount the Django app automatically.

Existing projects use the manifest-driven [whole-unit upgrader](docs/ADOPTER-UPGRADE.md). It advances
the engine, Django adapter, schemas, and canonical quality/doctrine contracts together, records the
exact scaffold commit and file hashes, and preserves project identity, seed, state, live ledger,
project laws, and adopter extras.

Then, inside the new project:

1. Mount `adapters/django/hub` under `/hub/` using
   [MOUNTING.md](adapters/django/MOUNTING.md). Never mount it at the site root.
2. Decide whether Hub reads may be unauthenticated; add a protection boundary if not.
3. Generate and inject a write token through the deployment secret mechanism.
4. Validate and seed genesis state.
5. Exercise each changed production operation directly; use a temporary focused probe only for a
   critical boundary and delete it before commit.
6. Adapt the deploy/canary patterns to the actual platform and alert channel.

```bash
export HUB_WRITE_TOKEN='<generated secret>'
python manage.py migrate
python manage.py seedhub --dry-run
python manage.py seedhub
python manage.py hubaudit
```

## The completion dial

`HUB_DONE_STRICTNESS` separates cheap tracking from stronger proof:

- `tracked` (default): completion requires a live claim, acceptance note, and at least one non-empty
  evidence value. A `verification_command` is optional; when present, the worker must submit its
  matching typed exit-0 receipt. The Hub never executes it.
- `strict`: every evidence item must resolve as a URL, repository commit, or existing path resolved
  from `BASE_DIR` (absolute paths are accepted too). A verification command remains optional; when
  one is explicitly present, completion carries its matching typed exit-0 receipt.

Both modes refuse direct `status: done` writes and block completion on critical Hub audit failures.
Strict mode provides stronger mechanical evidence without granting the write token shell authority.
Strict URL evidence is still fetched from the Hub service account's network. See
[the API completion gate](adapters/django/HUB-API.md#the-completion-gate-hubapicomplete--what-it-checks-in-order).

## Optional worker launch

Worker launch is off by default. When intentionally enabled, the Hub page pre-mints a short-lived
grant using same-origin CSRF protection, then opens the registered `hub-worker://` handler on the
real click. The workstation validates the exact issuer and consumes the single-use grant with its
local token before starting an operator-supplied wrapper. There is no browser token prompt, unlock
overlay, popup worker window, or vendor-specific agent command in the scaffold.

Follow [the Windows adapter guide](adapters/windows/README.md); do not enable the server setting
without installing and exercising the workstation half through its real operation.

## What adopters must supply

- The actual build and ship commands.
- An ASGI production process server, TLS/reverse-proxy configuration that preserves unbuffered
  event streams, and any read authentication.
- Durable storage and a demonstrated backup/restore operation for `HUB_DIR`.
- A secret manager and write-token rotation process.
- An immutable build identity (`HUB_BUILD_SHA`, a platform-provided `SOURCE_VERSION`, or a pre-build
  stamp), the live front-door canary, and its immutable `{sha, served_sha, tasks_closed}` deploy
  record.
- Alert delivery and scheduling for standing monitors.
- Project-specific schemas and business invariants, plus temporary critical-boundary probes when
  a task genuinely needs one.
- The vendor-specific local worker wrapper, if worker launch is used.

The repository intentionally does not ship a machine-wide/session-memory system. The Hub and
`PROJECT/` are durable project records; general agent memory is an environment-level concern.

## Maintenance

- Perform the actual changed operation and record its natural receipt. That is sufficient for
  ordinary maintenance, including copy and visual work.
- If any `PROJECT/` template changes, run `python tools/build_bootstrap.py` and commit the regenerated
  `PROJECT-PLANE-BOOTSTRAP.md`; generation is the operation, not an invitation to build a test ladder.
- Invoke a maintenance utility only when its own artifact or protected publishing boundary is the
  subject of the task. Do not run broad checks out of habit.
- If a critical boundary needs a probe, create it outside the repository, run it once, record the
  receipt, and delete it before committing. Never turn it into an automatic workflow.
- Schedule the conformance scan described in `patterns/` only after implementing its registry and
  alert bindings.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Zac Oberg.
