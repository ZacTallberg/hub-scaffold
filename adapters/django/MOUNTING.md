# Mounting the hub into an EXISTING Django project

Mounting is complete only when the rendered Hub satisfies `PROJECT/HUB-QUALITY.md`; preserve local
identity while proving its width, preference, transport, accessibility, and flow states.

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

# The literal-realtime transport is served by an ASGI process server.
ASGI_APPLICATION = "your_project.asgi.application"

# Required when more than one server process serves the Hub. The factory returns an object with
# publish(channel, signal) and blocking listen(channel) methods (Redis/NATS/Postgres, etc.).
# HUB_REALTIME_BROKER = "your_project.realtime.RedisBroker"

# --- hub configuration (all keys optional; defaults shown) ---
HUB_PROJECT_KEY = "{{PROJECT_KEY}}"   # entity-id prefix, lowercase slug, e.g. "acme"
HUB_BRAND = "{{BRAND}}"               # human title, e.g. "Acme" -> navbar reads "Acme · Hub"
# HUB_PROJECT_DIR = BASE_DIR / "PROJECT"  # override for monorepos; env form is also accepted
# HUB_WORK_ROOT = BASE_DIR                 # repo/evidence root; defaults to HUB_PROJECT_DIR.parent
# HUB_DIR = "/mounted/durable/path/hub"   # REQUIRED in production; never leave the live ledger
#                                          # inside an ephemeral application image
HUB_BUILD_STAMP = "build_sha.txt"     # BASE_DIR-relative build-identity stamp (see section 8)
# HUB_BUILD_SHA = os.environ.get("HUB_BUILD_SHA", "")  # optional immutable platform revision
HUB_DONE_STRICTNESS = "tracked"       # the evidence-resolution dial — see below
# HUB_FAILURE_CIRCUIT_THRESHOLD = 3    # identical cause signatures before circuit-open
# HUB_FAILURE_BACKOFF_BASE_S = 30      # exponential retry base
# HUB_FAILURE_BACKOFF_MAX_S = 3600     # hard retry ceiling
# HUB_SETTINGS_FILE = BASE_DIR / "config" / "settings.py"  # only if the audit should scan a
#                                       # different file than DJANGO_SETTINGS_MODULE resolves to

# Shared-root compatibility token: ALWAYS from the environment, NEVER a committed literal.
# Normal workers use scoped X-Agent-Token credentials issued by the credential API.
HUB_WRITE_TOKEN = os.environ.get("HUB_WRITE_TOKEN", "")
HUB_SHARED_TOKEN_COMPAT = os.environ.get("HUB_SHARED_TOKEN_COMPAT", "true").lower() == "true"

# Optional workstation worker bridge (disabled unless explicitly enabled):
HUB_WORKER_LAUNCH_ENABLED = False
HUB_WORKER_PROTOCOL = "hub-{{PROJECT_KEY}}"
# HUB_WORKER_LAUNCH_ISSUER_URL = "{{LIVE_URL}}/hub/api/launch-grant/consume"
# HUB_WORKER_GRANT_TTL_S = 120
```

`HUB_DIR` is optional only for local development. In production, point it explicitly at storage
that survives process and artifact replacement and is writable by the service account. The Hub
publishes topology-free `realtime.storage` truth and opens a high audit finding when production
falls back to implicit storage or cannot write the configured ledger. A release task must still be
present after the real process/artifact swap before its deploy closure is recorded; that observed
survival is the durability receipt, not a permanent test.

### The evidence-resolution dial (`HUB_DONE_STRICTNESS`)

The hub is designed to track work without manufacturing test work. Completing a task always records
who (claim lease), what (accept note), and evidence (≥1 URI). The real operation—rendering the page,
using the changed control, running the migration, or shipping the artifact—is the default proof.
That floor is deliberately cheap: one claim, one completion, no validation ceremony.

- `"tracked"` (default) — flow-first. Evidence can be anything non-empty (auth-walled ticket
  links, doc URLs, file paths); a `verification_command` on the task is optional. When present,
  the worker must submit its matching typed exit-0 receipt; the Hub never runs the command.
- `"strict"` — dereferenceable evidence. Every evidence URI must resolve (URL <400 / commit in
  this repo / existing path resolved from `BASE_DIR`). A `verification_command` remains optional;
  strict mode never creates a requirement to test ordinary work. If a command is explicitly present,
  its matching typed exit-0 receipt is required in either mode. Strict mode does not sandbox an
  untrusted token holder.

Changing the setting later is a one-line change and applies only to future completions. Start
tracked; use strict only when evidence itself must be mechanically reachable.

Tests are exceptional and transient. Do not create one for copy, wording, spacing, color, minor
layout, animation polish, or routine fixes. Only a critical security, destructive-data, migration,
protocol-compatibility, or concurrency boundary can justify a temporary task-specific probe. Create
that probe outside the permanent suite, run it once, store the receipt, and remove the probe artifact
before commit. Once the actual changed behavior succeeds and no critical boundary remains, stop.

Receipts compose. A task inherits the completed receipts of its dependencies; a release or parent
task examines only a genuinely new critical integration seam. It does not rerun child proof or
create nested verifier fan-out.

Security consequence: strict URL evidence is fetched from the server's network. Scoped agent
credentials grant only their named operations; the compatibility `HUB_WRITE_TOKEN` grants root
board authority (done, deploys, ADRs), but not shell execution:
when a task carries the optional `verification_command`, the worker runs it out-of-band and submits
a receipt. Only trusted
operators/agents may hold it because terminal governance authority is still privileged.

Notes:

- `BASE_DIR` must exist in settings (Django's default template provides it). The adapter resolves
  the build stamp relative to it. The project plane defaults to
  `BASE_DIR/PROJECT`; set `HUB_PROJECT_DIR` (Django setting or environment) when a monorepo keeps
  the canonical plane elsewhere. A relative override is resolved from `BASE_DIR`, and identity,
  schemas, ledger, claims, and grants all follow that one plane. Git and relative evidence paths
  resolve from `HUB_WORK_ROOT`, which defaults to that Project Plane's parent.
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
  schema/            # required registry (task/run/adr/feat/gap/cap/deploy/note/common)
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
overrides the event-log location (useful for isolated or temporary runs).

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

MCP exposes durable AgentRun control plus the current Tasks extension `tasks/get`, `tasks/update`,
and `tasks/cancel` methods. It does not advertise task-notification subscriptions because this
stateless view does not implement that stream. The Hub's `/hub/live/events` SSE connection is the
shipped immediate-push rail for the cockpit and worker coordination; MCP point reads never become
a background refresh cycle. Every run append wakes SSE as part of the normal committed-event path.

Read surface (unauthenticated; safe to expose only when all board data is publishable):

- `GET /hub/` — the human view (single-file tabbed app; `?format=json` returns the snapshot)
- `GET /hub/?served=<sha>` / `hub.json` — snapshot incl. build coherence
- `GET /hub/audit.json`, `graph.json`, `next.json`, `task.json`, `task/<local>.json`,
  `schema/<type>.schema.json`

If the adopter makes Hub reads private, keep the agent write seam distinct from human read access.
Private middleware may pre-authenticate `X-Agent-Token` through
`hub_core.agent_auth.CredentialRegistry` solely to route a valid credential to `/hub/api/*` without
logging the caller in. Invalid or missing credentials must retain the adopter's identical opaque
404/login response; header-presence pass-through leaks route existence. The endpoint's `@writer`
decorator reauthenticates and remains the only scope/authorization decision. Never cache the routing
check as authority and never extend it to `/hub/`, JSON reads, schemas, audit, or
`/hub/live/events`. See `SECURITY.md` for the minimal middleware shape.

## 5. Serve the live Hub through ASGI

The Hub's live event connection is a long-lived streaming response. Serve the Django site through
its ASGI application in every environment where realtime behavior matters. Django's built-in
`runserver` is a WSGI development server: it is still useful as a compatibility preview, but it is
not the production shape and it should not be used to judge or operate long-lived event delivery.
Under ASGI, Django can hold many slow streaming connections without dedicating one WSGI thread to
each connection. Keep the middleware chain async-capable end to end as well: a synchronous
middleware between the ASGI server and an async stream forces Django to adapt that request back
through a worker thread and gives up the concurrency benefit.

Keep both entrypoints if the host needs them, but add the standard ASGI callable and prefer it for
the Hub:

```python
# your_project/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "your_project.settings")
application = get_asgi_application()
```

If the Hub code is vendored outside the project root, apply the same `sys.path` bootstrap in
`asgi.py` that you use in `manage.py`; `example/example_site/asgi.py` is the runnable reference.
Install and pin one ASGI server in the adopting deployment, then launch it from the directory that
contains `manage.py`. Uvicorn is the shortest reference command:

```bash
python -m pip install uvicorn
python -m uvicorn your_project.asgi:application --host 127.0.0.1 --port 8000
```

For local code reloads only, append `--reload`. Hypercorn
(`hypercorn your_project.asgi:application --bind 127.0.0.1:8000`) and Daphne
(`daphne -b 127.0.0.1 -p 8000 your_project.asgi:application`) are valid alternatives. In
production, put the chosen ASGI process server behind the deployment's TLS/reverse proxy and make
sure that proxy does not buffer `text/event-stream`, does not cache or transform it, and gives the
connection a suitable idle timeout. Those proxy settings are part of the realtime path: buffering
turns immediate server events back into delayed batches even when Django is correct.

The Hub kit is inlined and does not require `collectstatic`, but the host product often does. Build
the product's static artifact under **production settings**. In particular, never run
`DEBUG=1 python manage.py collectstatic` when `STORAGES` selects its hashed/manifest backend only in
production: that creates a development artifact and the released app then resolves a manifest that
was never built. A container build should use `DEBUG=0` (plus a non-secret build-only
`SECRET_KEY` where settings require one) for `collectstatic`.

The built-in wake bus is literal push with process scope, which is complete for one ASGI worker.
If the deployment runs multiple server processes or hosts, configure `HUB_REALTIME_BROKER` with a
shared broker factory. It must return an object exposing `publish(channel, signal)` and
`listen(channel)` (a blocking iterator of signal dictionaries). The adapter runs one listener per
process and relays into its native async waiters. Without that setting, a write handled by process A
cannot wake an SSE connection owned by process B; the stream reports `realtime.scope:"process"`
rather than pretending otherwise. Broker failure reports `shared-degraded`; durable mutations are
never rolled back, and reconnect cursor recovery remains authoritative.

This guarantee assumes the active board has one write entrance: these served API routes. A Python
sidecar that imports the store, a management shell that appends directly, or a script that edits the
ledger is not a live writer and cannot publish into the server's wake plane. Use
`python -m hub_core.client` or ordinary HTTP for all agent/operator mutations. Stop or drain the Hub
before any explicit direct-ledger recovery operation.

This is the transport contract:

- normal Hub writes and reads remain ordinary HTTP requests;
- the browser holds one persistent live event connection while the page is open;
- each mutation wakes that connection and carries a cumulative canonical `patch` directly—there
  is no identity-event/follow-up-fetch window;
- UI state reports that connection as connected or disconnected, never as periodically synced;
- reconnect is recovery from a transport interruption, not an expected polling cycle.

See Django's official [ASGI deployment guide](https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/),
[Uvicorn guide](https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/uvicorn/), and
[async support notes](https://docs.djangoproject.com/en/6.0/topics/async/) for the current server
and long-lived-request model.

## 6. Genesis: seed the board

```bash
python manage.py migrate            # hub has no models; migrate your own apps as usual
python manage.py seedhub --dry-run  # validate PROJECT/seed.json against the schemas
python manage.py seedhub            # idempotent genesis import (re-running skips existing ids)
```

`seedhub` is the ONE sanctioned hand-authored entry point. After genesis, the board changes
only through the served typed write API (discover -> claim -> implement -> record). This is also a
realtime invariant: never call the store directly from a side process while the Hub is active.

## 7. The audit as a board-integrity gate

```bash
python manage.py hubaudit           # exit 0 = PASS/WARN-only, 2 = violations, 1 = internal error
python manage.py hubaudit --json    # machine-readable, for operator/deploy tooling
```

Use it at the boundary where canonical board integrity matters—for example immediately before a
production deployment or after a structural schema/event-store change—and abort on a critical
violation. It is not a per-task test and should not be invoked for copy, styling, or other ordinary
page changes. `patterns/pre-receive-gate.sh` remains the separate repository-law boundary for
credential-shaped pushes.

The audit is computed-not-attested: schema validity of every entity, referential integrity
(no dangling idrefs), ADR numbering, event-log hash-chain tamper check, build coherence
(Git/artifact identity vs deploy record vs served sha), portable-identity alignment
(`HUB_PROJECT_KEY` must match `PROJECT/project.json`), settings AST safety, and route-guard introspection
(every `/hub/api/` route must carry either the general `@writer` token gate or the explicitly narrow
origin-gated launch mint marker). It never trusts a stored boolean.

## 8. Build coherence (the false-green killer)

The audit wants to know WHICH build is running:

- In a checkout, it uses `git rev-parse HEAD`.
- In a deployed artifact (no `.git`), an explicit immutable `HUB_BUILD_SHA` wins, followed by the
  pre-build `HUB_BUILD_STAMP`, then the platform-provided `SOURCE_VERSION` fallback. The
  stamp precedes `SOURCE_VERSION` because many platforms expose a full SHA while established Hub
  artifacts deliberately stamp its short, deploy-record-compatible form. Have your build inject
  or write the commit sha **before** constructing the artifact (for example,
  `git rev-parse --short HEAD > build_sha.txt`). The same artifact-native identity is attached to
  production Hub mutations and drives live delivery without requiring Git in the container.
- Your deploy script may also record the shipped sha in `PROJECT/state.json`
  (`last_deploy_sha`). If that runtime shortcut is absent, build/audit coherence falls back to the
  latest coherent immutable deploy entity rather than reporting a false "no deploy record".
- After the front-door canary sees that exact SHA, `POST /hub/api/deploy` records immutable
  `sha`, matching `served_sha`, and the explicit done `tasks_closed[]` carried by the release.
  That exact closure plus the running artifact stamp proves those tasks live directly. Git ancestry
  remains optional source-checkout/legacy enrichment.
- A reverse-proxy/canary may also pass the sha it observed as `?served=<sha>`; it is an external
  comparison, not the primary running identity, and a conflict with the artifact stamp is visible.

Unknowable coherence is REPORTED, never silently skipped: missing build identity is a blocking
violation in prod and an amber warning under `DEBUG`; a missing deploy record (pre-first-deploy)
is amber so it cannot block the very deploy that creates it.

## 9. Scoped write-authority contract

- Normal workers send `X-Agent-Token: <token>`. Issue one with
  `POST /hub/api/agent-credential` and
  `{"action":"issue","subject":"worker-id","scopes":["task:*","run:*","mcp:call"],"ttl_s":3600}`.
  The bearer token is returned once; the durable registry stores only its SHA-256 digest.
- `action:"revoke"` plus `credential_id` invalidates a credential immediately;
  `action:"list"` returns identity/scope/lifetime metadata but never a token or digest.
- Operation scopes include `task:claim`, `task:write`, `task:heartbeat`, `task:complete`,
  `task:release`, `task:fail`, entity-specific `*:write` scopes, `run:write`, `launch:consume`,
  `mcp:call`, and `credential:manage`. A namespace wildcard such as `task:*` or `run:*` is accepted.

- Compatibility transport also remains header-only: `X-Write-Token` is never accepted from a
  query string, where credentials leak into access logs and referrers. Digests compare constant-time.
- Compatibility: set `HUB_WRITE_TOKEN` only as the existing-client migration bridge and keep its
  local copy untracked. While `HUB_SHARED_TOKEN_COMPAT=True`, its requests are visibly recorded as
  the `shared-root-compat` actor; disable the setting after migration.
- Fail-closed: a missing, invalid, expired, revoked, or insufficiently scoped credential returns 403.
  Reads stay public; the optional CSRF-gated mint capability is described below.
- Endpoints (all POST, JSON body): `/hub/api/task`, `/hub/api/complete`, `/hub/api/adr`,
  `/hub/api/capability`, `/hub/api/decision`, `/hub/api/claim`, `/hub/api/heartbeat`, `/hub/api/fail`,
  `/hub/api/run`, `/hub/api/run/update`, and `/hub/api/launch-grant/consume`.

Each agent token authorizes only its named operations. The compatibility write token authorizes
every operation and therefore terminal board/governance state. Neither authorizes operating-system
shell execution, and neither is suitable for browser storage or an untrusted client.

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
5. A `verification_command` is optional in both modes. If the task explicitly carries one, the
   server requires a typed exit-0 `verification_run` receipt the WORKER produced (the Hub never
   runs the command). A missing receipt is **422 need_verification_run**; a receipt for another
   command or a nonzero exit is **422 bad_verification_run**.
6. Completion does not launch a repository-wide audit; run structural maintenance deliberately
   only when its own boundary warrants it.
7. Immediately before append, the fencing token is rechecked and the transition is bound to the
   exact task version whose command was verified. Lease expiry/reclaim or a concurrent task edit
   returns **409**. Success releases the lease; abandoned `in_progress` work is reoffered after
   lease expiry.

Default completion loop (valid in both modes; strict additionally dereferences the evidence):

```bash
T="$HUB_WRITE_TOKEN"; H="X-Write-Token: $T"; U=http://localhost:8000/hub/api
curl -s $U/task -H "$H" -d '{"title":"Ship X","acceptance":"the changed operation succeeds","agent":"a1"}'
curl -s $U/claim -H "$H" -d '{"id":"acme:task:0001","agent":"a1"}'            # -> lease token
curl -s $U/complete -H "$H" -d '{"id":"acme:task:0001","token":"<lease>","agent":"a1",
  "accept_note":"the changed operation succeeded","evidence_uri":["<commit-sha>"]}'
```

For a rare critical boundary, the optional `verification_command` must describe the temporary probe
for that task's own artifact. Run it out-of-band, attach the matching receipt, and delete the probe
before commit. The write API refuses a broad suite runner because generic suite health does not prove
that task's work happened and permanent test accumulation damages throughput.

## 10. Optional local-worker launch

This feature is off by default. When enabled, the page shows **Launch Worker** and pre-arms it before
the click so the final `hub-worker://` navigation remains synchronous and retains browser user
activation. No popup or token-unlock console is used. The grant signing secret stays under
`PROJECT/.hub`; first-use creation is serialized across processes, and every nonce is single-use.

On Windows, configure the server settings above and follow `adapters/windows/README.md`. Registration
is per-user (HKCU, no admin) and requires both a local token-file path and an operator-supplied agent
wrapper. The handler validates the configured issuer exactly, refuses non-HTTPS remote issuers, and
starts no process until the issuing Hub authorizes and burns the grant. Worker windows are tied to
the wrapper lifecycle and close when it exits.

## 11. Optional ingests

- `python manage.py hubimport` — `CAPABILITY-LEDGER.md` table rows -> `cap` entities.
- `python manage.py hubmaterialize` — `PROJECT/REVIEW-AND-REIMPL-PLAN.md` gap ledger -> `gap`
  entities. Both idempotent, both validate before append.
