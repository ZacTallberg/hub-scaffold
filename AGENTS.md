# AGENTS.md — orientation for an agent that just pulled this repo

For any new or materially upgraded Hub, `PROJECT/HUB-QUALITY.md` is the canonical product,
realtime, accessibility, visual, and throughput contract; execute it through
`campaigns/elevate-hub.md`.

**This repo IS the canonical hub template — the single source every new project spins up from.**
(Until 2026-08-09 it was a generated export of a private upstream; that upstream is retired and
this repo now stands alone.) Improvements are committed HERE, under two laws. First, the
agnosticism gate is non-negotiable: nothing project-, host-, or person-specific may land —
`tools/scrub_check.sh` enforces it when that publishing boundary is intentionally exercised.
Second, improvements discovered inside a working instance
arrive as **curated upserts**: the instance proves the change in production first, then the
generic form of it is carried here through the scrub gate — never a bulk merge, never a clone.
The proving grounds are the instances; the template is where the lesson is kept. The rest of the
layer is `init.sh`, `example/`, and the bootstrap embedding.

Read this first. It is the machine-first map of the whole system: what's here, how to prove it works,
how to use it, how to operate it, and — importantly — **what is deliberately NOT here** so you don't
mistake a design choice for a missing piece. (`README.md` is the human-oriented version; this is yours.)

## What this repo is
A portable, environment-agnostic **project operating system**: an event-sourced hub (the operable
record), a content-agnostic PROJECT plane (the durable record), out-of-process enforcement patterns
(the anti-false-green kit), and the agent-prompt campaigns that run all of it. Extracted and scrubbed
from a working multi-project system. Nothing here names any specific person, host, or project;
`tools/scrub_check.sh` enforces that — keep it true.

## The mental model (four layers)
- **hub** (`hub_core/` + `adapters/`) — nouns you operate: events → projected tasks/ADRs/features/etc.,
  token-gated writes, server-granted "done", and an optional single-use-grant worker bridge. Mounts
  at `/hub` in a Django site; reads require an authenticated principal unless the operator declares
  the board public (`HUB_READ_AUTH`), and the workstation bridge is disabled by
  default. The board is a live cockpit: observed presence (who is on it, from which machine and
  console, doing what), the ask/answer loop (a blocked worker's question is DELIVERED to the
  operator's inbox long-poll, the answer comes back addressed, the ack closes it), and a bounded
  operational error stream with an honest read-time bar. Blocked on a fact only the operator has?
  `POST /hub/api/ask` — never stall in silence, and search the board first (`search.json`).
- **plane** (`PROJECT/`) — the durable on-disk tree: charter, doctrine, ADRs, registers, research,
  verification contracts, the leader/worker/verifier protocol. What a cold session reads to pick up.
- **patterns** (`patterns/`) — opt-in enforcement: deploy contract, standing canary, pre-receive gate,
  agent guard, conformance-scan spec, **error visibility** (`error-visibility.md`: wire every
  service's failures into the Hub's operational stream BEFORE the first feature — a service
  that keeps failures in its own log can only be debugged from a shell on its host), and
  **worker longevity** (`worker-longevity.md`: a worker's
  green condition is COMPLETIONS, not aliveness — the nine ways a seat stops finishing work and
  the barren ladder it climbs when a cycle produces nothing), the **agent client daemon**
  (`agent-client-daemon.md`: delivery into consoles, three-channel per-prompt context, a second
  runtime as an adapter, a self-updater that cannot break itself, headless on Windows), the
  **route reconciler** (`route-reconciler.md`: per-entry refusal behind a mass-refusal guard, and
  a report on every pass), **coordination** (`coordination.md`: how the hub pairs consoles on
  the same file, task or subsystem and what each side does with the signal), **deploy
  hardening** (`deploy-hardening.md`: the deploy-step failures that roll back good builds or hide
  bad ones, and how CI/rollback results reach the board), **machine push identity**
  (`machine-push-identity.md`), and the **presence gate** (`presence-gate.py`: a harness hook that
  derives the files a console edited, so the board can name two consoles editing one file), and **read access grants**
  (`read-access-grants.md`: a private board admits its static allowlist plus the ledger's own
  active grants — bounded, fail-soft, never wider on a failed read), and **app services**
  (`app-services.md`: hosted UI components apps link instead of copy, per-app component
  properties, each app's slice of the board, per-person preferences, and the hub as the one
  credential broker to an agent service — its routes ship mounted but do nothing until an app
  links a component or `HUB_AGENT_URL` is set), **unattended responders**
  (`unattended-responders.md`: `hub_core.responder` spends one bounded, non-interactive session
  per item that actually happened — a question, a fresh error, a task marked `--unattended` —
  never a rotting task or a timer), and **multi-agent coordination**
  (`multi-agent-coordination.md`: crossover between consoles, the five record kinds, and
  audience-fenced doctrine).
  Operating write-ups for what an adopter wires itself: `ci-pipeline.md` (forward-only deploys
  that CI never cancels, push-only change triggers, job ceilings, publications from the protected
  ref), `app-slots.md` (allocate / stage / cut over / retire / re-adopt an app's port, service and
  route), `directory-sign-in.md` (401 vs 503, one bind per rejected credential), and
  `shared-app-banner.md` (one linked header master for many apps). None runs unless you install
  it.
- **campaigns** (`campaigns/`) — the verbs: the robust prompts to MAINTAIN / IMPROVE / AUGMENT / BUILD.

## First-pull runbook
1. **Use the real thing first:** perform the operation the task changes. A successful real operation
   is the default proof; if it breaks, that failure is the notice to capture. Do not create tests for
   copy, style, animation polish, or another non-critical fix. Read `docs/TESTING.md` before adding
   any proof machinery.
   Record an observed failure as fresh Hub task input for a repair/error-fixing lane. Stay in the
   claimed delivery role; do not speculate or preemptively turn into the repair agent.
   Mutate a running board only through its served `/hub/api/*` seam (or
   `python -m hub_core.client`). Never use `EventStore`, `hub_app.store()`, `events.jsonl`, or
   `events.db` as a live write shortcut: a side-process append bypasses the push publisher and can
   leave a cockpit labeled Connected behind the ledger. Direct access is offline recovery only.
2. **Read, in order:** this file → `README.md` → `campaigns/00-orchestration-method.md` (how to run
   work well) → `OPERATING-AGREEMENT.md` (the working laws) → `SECURITY.md` (the actual trust boundary)
   → `adapters/django/MOUNTING.md` (how the hub
   mounts) → `adapters/django/HUB-API.md` (the API you drive the hub with — read this before you POST
   anything). Skim `PROJECT/DOCTRINE.md` for the in-repo law.
3. **See it:** in `example/`, `DEBUG=1 python manage.py migrate && seedhub && runserver`, open `/hub`.

## How to USE it (stamp a new project)
`bash init.sh <target-dir> <project-key> "<Brand>" [live-url]` → a governed, git-initialized project with
the plane + hub + governance files, placeholders substituted. Then: mount the adapter per
`MOUNTING.md`, `seedhub` the genesis board, invoke `hubaudit` only when its protected deploy boundary
is crossed, and adopt the
deploy contract (`patterns/deploy-contract.md`). `init.sh` refuses a non-empty target and never
clone-and-pivots — it's the only sanctioned way to start.

## How to OPERATE it (do real work)
Everything runs through `campaigns/`. Match the campaign to the verb:
- keep a project honest/current → `maintain-audit-reconcile.md`
- review/harden a codebase → `improve-moe-review.md` (the multi-expert audit + adversarial closer)
- add an entity type/tab, or backfill structure across repos → `augment-hub.md`
- drive feature work off the board → `feature-buildout.md` (DISCOVER→CLAIM→IMPLEMENT→RECORD→VERIFY)
The engine behind all of them—proportionate fan-out, persist-as-you-go, and boundary-triggered
verification—is `campaigns/00-orchestration-method.md`. Scale coordination to the work, and use the
disposable `verification-closer` only for a rare critical boundary.

## The two dials you should know
- **`HUB_DONE_STRICTNESS`** (`tracked` default | `strict`) — recorded vs dereferenceable evidence.
  A command is optional in both modes; when present, completion requires the worker's matching
  typed exit-0 receipt. Strict resolves evidence but never makes an ordinary task invent a test. See
  `adapters/django/MOUNTING.md` → "The strictness dial". Start `tracked`; go `strict` when completion
  claims need mechanical proof. Strict does not make an untrusted token holder safe.
- **Entity extensibility** — a new hub type is a schema + write path + tab. `campaigns/augment-hub.md`
  is the exact recipe; the base types were built this same way, so an added type is first-class.
- **`HUB_WORKER_LAUNCH_ENABLED`** (`False` default) — opt-in local process launch. Read
  `adapters/windows/README.md` before enabling it. The browser receives only a CSRF-minted,
  action/task/count-bound grant; authoritative consume remains write-token-gated.

## Security boundary you must not infer away
- **Authenticated does not mean sanitized.** Every authenticated reader (and, on a board declared
  `HUB_READ_AUTH = "public"`, everyone) sees the complete projected board. Keep sensitive data out.
- **The write token grants terminal board authority, not code execution.** A writer sets
  `verification_command`, which the
  worker runs OUT-OF-BAND, submitting a typed exit-0 receipt the hub validates — the hub never
  executes it (that was an RCE path and is gone); strict URL evidence IS still fetched by the server.
  Treat every token holder as trusted at the Hub service-account/network boundary. `SECURITY.md` is
  authoritative for this threat model.
- **Normative docs are not automatic controls.** `PROJECT/verify/`, campaigns, and patterns describe
  roles and contracts. A canary, alert, backup, verifier, or conformance scan exists only after it is
  wired and exercised through its real operation in the adopting environment.
  `docs/ARCHITECTURE.md` lists shipped guarantees.

## What is deliberately NOT here (design, not omission)
- **The memory layer** — session-recall/persistence tooling is home-environment-specific and excluded
  on purpose. If you want cross-session memory, wire your own; the plane + hub are the durable record
  this system relies on, not a memory tool.
- **A runnable conformance scanner and a runnable resume-anchor script** — shipped as *specs/patterns*
  (`patterns/conformance-scan.md`, `campaigns/maintain-audit-reconcile.md` Prompt B), not scripts,
  because both are inherently org-specific (your live-URL shape, your project list, your alert hook).
  Implement them per environment from the spec.
- **The org-specific ship step** — `patterns/deploy-runbook.md` is the shape: an agent executes
  the four laws by hand against your actual deploy target (a PaaS, Kubernetes, a VM, a container
  host, …), reading real output at each step. There is deliberately no deploy script to fill in —
  a script encodes one environment's assumptions and then rots silently against the platform it
  drives. The contract it must satisfy is fixed; the mechanism is yours.
- **Separate entity TYPES for Findings, Lessons, Decisions-log** — kept OUT of the minimal base.
  Lessons, findings, methods and reviews ride the base `note` type, distinguished by a kind tag,
  through their own verbs (`/hub/api/lesson|finding|method|review`, `patterns/knowledge-retrieval.md`).
  Promote one to its own type via `campaigns/augment-hub.md` only if you need a distinct schema.
  The client's `review-gate` verb and the MCP `record` tool (kind `review`) raise a review as a
  DELIVERED human gate (an ask tagged `review`, never taken by an unattended responder).
- **No LICENSE** — none, by choice (public repo → viewable but all-rights-reserved; add a permissive
  license only if a teammate needs to legally reuse it).

## The three non-negotiables (the point of the whole thing)
1. **The actual operation is the default proof.** Do not create tests for copy, style, or routine
   non-critical fixes. Stop once the changed behavior works and its truthful receipt is recorded.
2. **Every test is transient and rare.** Only security, destructive/data-integrity, migration,
   protocol-compatibility, or concurrency boundaries justify one. Create the smallest temporary
   probe, run it once, record the receipt, and delete every artifact before committing.
3. **Never lose or clobber concurrent work.** Targeted commits only; another session's uncommitted files
   are read, never staged; persist as you go so a killed run loses nothing.

Proof composes upward: a parent or release inherits completed child-task receipts. It may prove only
the newly created integration seam, and only when that seam is a critical boundary. Nested verifier
fan-out and replaying every child's proof are forbidden by default.

An observed failure is new task input, not permission for a delivery worker to expand scope. Record
it precisely and leave it available for a dedicated repair/error-fixing lane to claim.

## Provenance
Extracted 2026-07-07 from a working multi-project estate and updated 2026-08-03 with the portable
worker-launch trust boundary and repository-wide documentation contract; every file is scrub-verified
agnostic. If you change `PROJECT/`
templates, re-run `python tools/build_bootstrap.py`
so the bootstrap doc is regenerated. That generation is the real operation; do not add a separate
validation ladder around it.
