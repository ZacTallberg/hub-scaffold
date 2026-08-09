# hub-scaffold

`hub-scaffold` is a portable project operating system built around an append-only task/decision
ledger. It combines:

1. an event-sourced Hub (`hub_core/` plus a Django adapter);
2. a durable, content-agnostic management plane (`PROJECT/`);
3. opt-in enforcement patterns for deployment, canaries, repository hygiene, and agent safety;
4. campaign playbooks for maintaining, reviewing, extending, and building from the board.

The design goal is not more ceremony. The default tracking loop is deliberately small—discover,
claim, implement, complete with a note and evidence—while proof-heavy checks can be enabled where a
failure mode justifies them.

## What is implemented

- Canonical events in `PROJECT/.hub/events.jsonl`, hash-chained and append-only.
- A rebuildable SQLite index with optimistic concurrency and aggregate-scoped idempotency.
- JSON-Schema-validated tasks, ADRs, features, gaps, capabilities, deploys, and notes.
- Derived dependency/urgency state, graph, collections, audit, and a browser dashboard.
- A typed write API with one shared header token, expiring task leases, and a guarded completion
  transition.
- A focused computed audit for schemas, references, ADR numbering, event integrity, build
  coherence, selected Django safety settings, and mutation-route guards.
- A minimal mounted Django example and end-to-end refusal tests.
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

The general `HUB_WRITE_TOKEN` is more powerful than an ordinary tracker token. A write-token holder
can grant `done`, record deploys, and rule ADRs. It is NOT code execution: the Hub never runs a
task's `verification_command` — the worker runs it out-of-band and submits a typed exit-0 receipt
the Hub validates. Strict evidence URLs ARE still fetched by the server. Treat the token as production credentials,
give it only to trusted operators/agents, and isolate the service accordingly. The optional browser
launcher never receives it. Read [SECURITY.md](SECURITY.md) before enabling writes or worker launch.

## Documentation map

| Need | Start here |
|---|---|
| Understand components, storage, flows, and guarantees | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Threat model, trust boundaries, deployment checklist | [SECURITY.md](SECURITY.md) |
| Mount the Hub into Django | [adapters/django/MOUNTING.md](adapters/django/MOUNTING.md) |
| Operate the HTTP API | [adapters/django/HUB-API.md](adapters/django/HUB-API.md) |
| Run, back up, restore, rotate, or troubleshoot | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Test locally or in CI | [docs/TESTING.md](docs/TESTING.md) |
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
example/                        runnable strict-mode Django integration fixture
patterns/                       opt-in deploy/canary/repository/agent patterns + worker longevity
campaigns/                      operating playbooks
governance/                     agent-rule templates installed by init.sh
docs/                           architecture, operations, and testing manuals
tools/                          bootstrap, scrub, documentation, and end-to-end checks
PROJECT-PLANE-BOOTSTRAP.md      standalone plane specification with embedded templates
```

## Quickstart: get fast confidence

Prerequisites are Git, Bash, and Python. `hub_core` supports Python 3.10+ with no third-party
runtime dependencies. The example needs Django; install the repository requirements first.

```bash
bash tools/check.sh --all-fast
```

Use `PYTHON=python3` when needed. Windows users should use Git Bash and a Windows Python
interpreter; exact recipes are in [docs/TESTING.md](docs/TESTING.md).

This fast check needs no Django install. It covers the scrub, the compile/import floor,
documentation/schema links, generated bootstrap parity, and syntax. There is deliberately no unit
battery — a guard is proven by watching it fire, a feature against the real example app
([docs/TESTING.md](docs/TESTING.md)). The isolated full verifier additionally needs
`python -m pip install -r requirements.txt` and runs with `bash tools/selftest.sh`; reserve it for a
release, risky cross-cutting boundary, regression, or occasional audit.

For independent review, dispatch a fresh read-only agent with
[the verification-closer prompt](campaigns/verification-closer.md) or the reusable
[`$verification-closer` skill](skills/verification-closer/SKILL.md). It returns one evidence-backed
verdict and exits; minor changes do not automatically receive another agent or the full ladder.

## Stamp a new project

`init.sh` requires a new or empty target directory and creates a Git repository with a genesis
commit:

```bash
bash init.sh ../my-project my-project "My Project" https://my-project.example.com
```

It copies `PROJECT/`, `hub_core/`, adapters, patterns, the operating/security agreements, and the
architecture contract; installs the governance templates as `CLAUDE.md` and `AGENTS.md`; substitutes `{{PROJECT_KEY}}`, `{{BRAND}}`, and
`{{LIVE_URL}}`; and fails if a placeholder survives. It does not merge into an existing project or
mount the Django app automatically.

Then, inside the new project:

1. Mount `adapters/django/hub` under `/hub/` using
   [MOUNTING.md](adapters/django/MOUNTING.md). Never mount it at the site root.
2. Decide whether Hub reads may be unauthenticated; add a protection boundary if not.
3. Generate and inject a write token through the deployment secret mechanism.
4. Validate and seed genesis state.
5. Put `hubaudit` and the project's own high-value tests in CI/pre-deploy.
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
  evidence value. A `verification_command` is optional, but the server executes it when present.
- `strict`: every evidence item must resolve as a URL, repository commit, or existing path resolved
  from `BASE_DIR` (absolute paths are accepted too), and the completion must carry a typed exit-0 receipt for the task's own
  verification command.

Both modes refuse direct `status: done` writes and block completion on critical Hub audit failures.
Strict mode provides stronger mechanical evidence, but it also makes the write-token command
execution boundary unavoidable. See [the API completion gate](adapters/django/HUB-API.md#the-completion-gate-hubapicomplete--what-it-checks-in-order).

## Optional worker launch

Worker launch is off by default. When intentionally enabled, the Hub page pre-mints a short-lived
grant using same-origin CSRF protection, then opens the registered `hub-worker://` handler on the
real click. The workstation validates the exact issuer and consumes the single-use grant with its
local token before starting an operator-supplied wrapper. There is no browser token prompt, unlock
overlay, popup worker window, or vendor-specific agent command in the scaffold.

Follow [the Windows adapter guide](adapters/windows/README.md); do not enable the server setting
without installing and testing the workstation half.

## What adopters must supply

- The actual build and ship commands.
- A production process server, TLS/reverse-proxy configuration, and any read authentication.
- Durable storage and tested backup/restore for `HUB_DIR`.
- A secret manager and write-token rotation process.
- The build stamp and live front-door canary.
- Alert delivery and scheduling for standing monitors.
- Project-specific verification commands, tests, schemas, and business invariants.
- The vendor-specific local worker wrapper, if worker launch is used.

The repository intentionally does not ship a machine-wide/session-memory system. The Hub and
`PROJECT/` are durable project records; general agent memory is an environment-level concern.

## Maintenance

- Run `bash tools/check.sh` for ordinary pending changes. It selects cheap checks by impact.
- Invoke a disposable closer and/or `bash tools/selftest.sh` only at a meaningful risk or release
  boundary; do not put the full ladder on every minor edit.
- If any `PROJECT/` template changes, run `python tools/build_bootstrap.py` and commit the regenerated
  `PROJECT-PLANE-BOOTSTRAP.md`.
- Run `python tools/docs_check.py` to catch broken local links and schema-mirror drift.
- Run `bash tools/scrub_check.sh` before publishing to keep the scaffold environment-agnostic.
- Schedule the conformance scan described in `patterns/` only after implementing its registry and
  alert bindings.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Zac Oberg.
