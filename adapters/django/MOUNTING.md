# Mounting the hub into an EXISTING Django project

The hub is two pieces:

- `hub_core/` — pure-Python engine (event store, schema validator, audit, projections, frontend
  kit). Framework-free, stdlib-only. Must be importable as `hub_core`.
- `hub/` (this directory's `hub/`) — the Django adapter app: read API, token-gated write API,
  the human view, and the `seedhub` / `hubaudit` / `hubimport` / `hubmaterialize` management
  commands. Must be importable as `hub`.

Everything below assumes your repo root is the Django `BASE_DIR` (the directory holding
`manage.py`). The Hub's GET surfaces are unauthenticated and return the complete board unless you
add an authentication boundary. Read [SECURITY.md](../../SECURITY.md) before exposing `/hub` or
issuing a token.

## 1. Put the code on the import path

Copy (or submodule/symlink) into the repo root:

```
your-project/
  manage.py
  hub_core/          <- copy of hub-scaffold/hub_core
  hub/               <- copy of hub-scaffold/adapters/django/hub
  PROJECT/           <- the project plane (step 3)
```

Vendoring at the repo root is the zero-config path. If you keep them elsewhere, add that
location to `sys.path` in `manage.py` AND your WSGI/ASGI entrypoints (the scaffold's `example/`
does exactly this so it can run in place without copying).

## 2. Settings

```python
INSTALLED_APPS = [
    # ... your apps ...
    "hub",
]

MIDDLEWARE = [
    # ... your middleware ...
    "hub.middleware.NoStoreHTMLMiddleware",   # optional: no-store on dynamic HTML after deploys
]

# --- hub configuration (all keys optional; defaults shown) ---
HUB_PROJECT_KEY = "{{PROJECT_KEY}}"   # entity-id prefix, lowercase slug, e.g. "acme"
HUB_BRAND = "{{BRAND}}"               # human title, e.g. "Acme" -> navbar reads "Acme · Hub"
HUB_BUILD_STAMP = "build_sha.txt"     # BASE_DIR-relative build-identity stamp (see section 7)
HUB_DONE_STRICTNESS = "tracked"       # the flow-vs-proof dial — see "The strictness dial" below
# HUB_SETTINGS_FILE = BASE_DIR / "config" / "settings.py"  # only if the audit should scan a
#                                       # different file than DJANGO_SETTINGS_MODULE resolves to

# Write-API token: ALWAYS from the environment, NEVER a committed literal.
HUB_WRITE_TOKEN = os.environ.get("HUB_WRITE_TOKEN", "")

# Optional workstation worker bridge (disabled unless explicitly enabled):
HUB_WORKER_LAUNCH_ENABLED = False
HUB_WORKER_PROTOCOL = "hub-{{PROJECT_KEY}}"
# HUB_WORKER_LAUNCH_ISSUER_URL = "{{LIVE_URL}}/hub/api/launch-grant/consume"
# HUB_WORKER_GRANT_TTL_S = 120
```

### The strictness dial (`HUB_DONE_STRICTNESS`)

The hub is designed to TRACK by default and PROVE on demand. Completing a task always records
who (claim lease), what (accept note), and evidence (≥1 URI) — that is the tracking floor, and
it is deliberately cheap: one claim, one complete, no ceremony.

- `"tracked"` (default) — flow-first. Evidence can be anything non-empty (auth-walled ticket
  links, doc URLs, file paths); a `verification_command` on the task is optional. When present,
  the worker must submit its matching typed exit-0 receipt; the Hub never runs the command.
- `"strict"` — proof-first. Every evidence URI must dereference (URL <400 / commit in this
  repo / existing path resolved from `BASE_DIR`) and every task needs a `verification_command` before it can be
  completed. Use this where completion claims cannot be taken on trust—for example an autonomous
  agent whose operating principal is trusted with Hub-server authority but whose “done” assertion
  still needs mechanical proof. Strict mode does not sandbox an untrusted token holder.

Ratcheting up later is a one-line settings change and applies only to future completions —
start tracked, go strict for the projects (or the agents) that earn it.

Security consequence: strict URL evidence is fetched from the server's network.
`HUB_WRITE_TOKEN` grants terminal board authority (done, deploys, ADRs), but not shell execution:
the worker runs a task's `verification_command` out-of-band and submits a receipt. Only trusted
operators/agents may hold it because terminal governance authority is still privileged.

Notes:

- `BASE_DIR` must exist in settings (Django's default template provides it). The adapter
  resolves `PROJECT/`, the build stamp, and evidence paths relative to it.
- `HUB_PROJECT_KEY` must match `^[a-z0-9][a-z0-9-]*$` — it prefixes every entity id
  (`acme:task:0001`). Pick it once; ids are allocated once and never renumbered.
- The AST security audit (part of `hubaudit`) scans your settings file and FAILS on:
  `DEBUG` defaulting to `True`, a literal `SECRET_KEY` fallback, or `ALLOWED_HOSTS`
  containing `"*"`. Keep the fail-closed posture: `SECRET_KEY` required in prod, ephemeral
  only under `DEBUG` (see `example/example_site/settings.py` for the reference shape).

## 3. The PROJECT/ directory

The hub's canonical state lives under `PROJECT/` at the repo root:

```
PROJECT/
  project.json       # required portable identity (key/brand/app/host/worker scheme)
  schema/            # required: the JSON-Schema registry (task/adr/feat/gap/cap/deploy/note/common)
  seed.json          # required for genesis: {"adrs":[...], "tasks":[...], "notes":[...]}
  state.json         # written by your deploy script: {"last_deploy_sha": "...", ...}
  .hub/              # RUNTIME, machine-written: events.jsonl + events.db + claims/ (gitignore .hub/)
```

Copy `PROJECT/schema/` from this scaffold's plane tree (or from `example/PROJECT/schema/`).
`init.sh` emits `PROJECT/project.json`; keep its `key` and `brand` aligned with the Django settings
above. `app_host` is the public app origin, `app_name` names the app for discovery/signing, and
`worker_scheme` is the per-project local launch scheme. The file is also the identity source for
MCP server discovery, the root agent discovery card, and receipt predicate types.
The runtime store (`PROJECT/.hub/`) is created on first store access; add `PROJECT/.hub/` to `.gitignore`
if you do not want runtime events in version control. The `HUB_DIR` environment variable
overrides the event-log location (useful for tests).

## 4. URLs

```python
# project urls.py
from django.urls import include, path
from hub.agent_card import agent_card_view

urlpatterns = [
    # ... your routes ...
    path(".well-known/agent-card.json", agent_card_view),  # ROOT discovery document
    path("hub/", include("hub.urls")),   # NEVER mount at the front door ("")
]
```

The root card is discovery-only because this adapter does not implement an A2A task transport. It
advertises no A2A interface or streaming capability; its callable-protocol extension points to the
real token-gated MCP JSON-RPC endpoint at `/hub/api/mcp`.

Read surface (unauthenticated; safe to expose only when all board data is publishable):

- `GET /hub/` — the human view (single-file tabbed app; `?format=json` returns the snapshot)
- `GET /hub/?served=<sha>` / `hub.json` — snapshot incl. build coherence
- `GET /hub/audit.json`, `graph.json`, `next.json`, `task.json`, `task/<local>.json`,
  `schema/<type>.schema.json`

## 5. Genesis: seed the board

```bash
python manage.py migrate            # hub has no models; migrate your own apps as usual
python manage.py seedhub --dry-run  # validate PROJECT/seed.json against the schemas
python manage.py seedhub            # idempotent genesis import (re-running skips existing ids)
```

`seedhub` is the ONE sanctioned hand-authored entry point. After genesis, the board changes
only through the typed write API (discover -> claim -> implement -> record -> verify).

## 6. The audit as a CI/deploy gate

```bash
python manage.py hubaudit           # exit 0 = PASS/WARN-only, 2 = violations, 1 = internal error
python manage.py hubaudit --json    # machine-readable, for CI annotations
```

Wire it so a red audit BLOCKS the ship, out of process from the agent doing the work:

- CI: run `python manage.py hubaudit` as a required check.
- Deploy: call it in your deploy script BEFORE building the artifact, and abort on nonzero.
- Server-side: `patterns/pre-receive-gate.sh` is the same out-of-process layer for repository
  law (it rejects credential-shaped pushes); extend it to also run the audit on push if your
  git host supports server hooks.

The audit is computed-not-attested: schema validity of every entity, referential integrity
(no dangling idrefs), ADR numbering, event-log hash-chain tamper check, build coherence
(git HEAD vs deploy record vs served sha), settings AST safety, and route-guard introspection
(every `/hub/api/` route must carry either the general `@writer` token gate or the explicitly narrow
origin-gated launch mint marker). It never trusts a stored boolean.

## 7. Build coherence (the false-green killer)

The audit wants to know WHICH build is running:

- In a checkout, it uses `git rev-parse HEAD`.
- In a deployed artifact (no `.git`), it reads the `HUB_BUILD_STAMP` file — have your build
  write the commit sha into it (e.g. `git rev-parse --short HEAD > build_sha.txt` at image build).
- Your deploy script records the shipped sha in `PROJECT/state.json` (`last_deploy_sha`).
- A reverse-proxy/canary can pass the sha it observed live as `?served=<sha>`.

Unknowable coherence is REPORTED, never silently skipped: missing build identity is a blocking
violation in prod and an amber warning under `DEBUG`; a missing deploy record (pre-first-deploy)
is amber so it cannot block the very deploy that creates it.

## 8. Write-API token contract

- Transport: general writes use `POST` with header `X-Write-Token: <token>` (never `?token=` —
  query strings leak into access logs and referers). Compared constant-time.
- Configuration: set the `HUB_WRITE_TOKEN` environment variable on the server; keep the local
  copy in an untracked file (e.g. `.hub_write_token.local`, gitignored). The adapter reads
  `settings.HUB_WRITE_TOKEN` first, then the environment.
- Fail-closed: when no token is configured, every general write and launch consume returns 403.
  Reads stay public; the optional CSRF-gated mint capability is described below.
- Endpoints (all POST, JSON body): `/hub/api/task`, `/hub/api/complete`, `/hub/api/adr`,
  `/hub/api/capability`, `/hub/api/decision`, `/hub/api/claim`, `/hub/api/heartbeat`, and
  `/hub/api/launch-grant/consume`.

The write token authorizes all of those endpoints and therefore terminal board/governance state.
It does not authorize operating-system shell execution. It is never suitable for browser storage
or an untrusted client.

The sole exception is the optional `POST /hub/api/launch-grant`: it is a same-origin,
`@csrf_protect` browser capability that can mint only a short-lived grant bound to
`action + task + count + issuer + nonce`. It cannot mutate board entities. The workstation then
calls the separate `/hub/api/launch-grant/consume` endpoint with the write token over HTTPS before
starting a process. The computed route audit recognizes exactly these two explicit gate classes:
general `@writer` routes and the narrow origin-gated mint route.

### The server-granted `done`

`status: "done"` cannot be written directly:

1. `POST /hub/api/task` with `"status": "done"` -> **409 use_complete**. Only
   `/hub/api/complete` can grant done.
2. `complete` requires a held claim lease (`/hub/api/claim` first) with a valid fencing token.
3. In both modes, `complete` requires `accept_note` + at least one non-empty `evidence_uri`.
4. In `strict` mode, every evidence string must dereference (URL answering <400, a commit sha
   present in the repo, or an existing path resolved from `BASE_DIR`) or completion returns
   **422 evidence_unresolvable**. This is a resolvability check, not a path/network sandbox.
5. In `strict` mode, the task must carry a `verification_command`; absence is
   **422 need_verification_command**. In either mode, if the task carries a command, the server
   requires a typed exit-0 `verification_run` receipt the WORKER produced (the Hub never runs the
   command). A missing receipt is **422 need_verification_run**; a receipt for another command or
   a nonzero exit is **422 bad_verification_run**.
6. The audit is recomputed server-side at completion time; CRITICAL violations (tampered chain,
   schema corruption) block with **422 audit_unsound**.
7. Immediately before append, the fencing token is rechecked and the transition is bound to the
   exact task version whose command was verified. Lease expiry/reclaim or a concurrent task edit
   returns **409**. Success releases the lease; abandoned `in_progress` work is reoffered after
   lease expiry.

Strict-mode example loop:

```bash
T="$HUB_WRITE_TOKEN"; H="X-Write-Token: $T"; U=http://localhost:8000/hub/api
curl -s $U/task -H "$H" -d '{"title":"Ship X","verification_command":"python tools/ship_x.py --dry-run","agent":"a1"}'
curl -s $U/claim -H "$H" -d '{"id":"acme:task:0001","agent":"a1"}'            # -> lease token
curl -s $U/complete -H "$H" -d '{"id":"acme:task:0001","token":"<lease>","agent":"a1",
  "accept_note":"dry-run exercised the changed artifact; output receipt attached","evidence_uri":["<commit-sha>"]}'
```

The `verification_command` must exercise the task's own artifact — the write API refuses a bare
suite runner (`python -m pytest`, `python -m unittest`) because a suite is green whenever the repo
is healthy, whether or not this task's work happened.

## 9. Optional local-worker launch

This feature is off by default. When enabled, the page shows **Launch Worker** and pre-arms it before
the click so the final `hub-worker://` navigation remains synchronous and retains browser user
activation. No popup or token-unlock console is used. The grant signing secret stays under
`PROJECT/.hub`; first-use creation is serialized across processes, and every nonce is single-use.

On Windows, configure the server settings above and follow `adapters/windows/README.md`. Registration
is per-user (HKCU, no admin) and requires both a local token-file path and an operator-supplied agent
wrapper. The handler validates the configured issuer exactly, refuses non-HTTPS remote issuers, and
starts no process until the issuing Hub authorizes and burns the grant. Worker windows are tied to
the wrapper lifecycle and close when it exits.

## 10. Optional ingests

- `python manage.py hubimport` — `CAPABILITY-LEDGER.md` table rows -> `cap` entities.
- `python manage.py hubmaterialize` — `PROJECT/REVIEW-AND-REIMPL-PLAN.md` gap ledger -> `gap`
  entities. Both idempotent, both validate before append.
