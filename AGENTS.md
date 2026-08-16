# AGENTS.md — orientation for an agent that just pulled this repo

For any new or materially upgraded Hub, `PROJECT/HUB-QUALITY.md` is the canonical product,
realtime, accessibility, visual, and throughput contract; execute it through
`campaigns/elevate-hub.md`.

**This repo IS the canonical hub template — the single source every new project spins up from.**
(Until 2026-08-09 it was a generated export of a private upstream; that upstream is retired and
this repo now stands alone.) Improvements are committed HERE, under two laws. First, the
agnosticism gate is non-negotiable: nothing project-, host-, or person-specific may land —
`tools/scrub_check.sh` enforces it, and its `--selftest` proves it catches a seeded violation AND
stays quiet on boundary-safe text. Second, improvements discovered inside a working instance
arrive as **curated upserts**: the instance proves the change in production first, then the
generic form of it is carried here through the scrub gate — never a bulk merge, never a clone.
The proving grounds are the instances; the template is where the lesson is kept. The rest of the
layer: the five-step `tools/selftest.sh` (scrub · compile floor · docs · bootstrap · the real
example app), `init.sh`, `example/`, and the bootstrap embedding.

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
  at `/hub` in a Django site; reads are unauthenticated, and the workstation bridge is disabled by
  default.
- **plane** (`PROJECT/`) — the durable on-disk tree: charter, doctrine, ADRs, registers, research,
  verification contracts, the leader/worker/verifier protocol. What a cold session reads to pick up.
- **patterns** (`patterns/`) — opt-in enforcement: deploy contract, standing canary, pre-receive gate,
  agent guard, conformance-scan spec, and **worker longevity** (`worker-longevity.md`: a worker's
  green condition is COMPLETIONS, not aliveness — the nine ways a seat stops finishing work and
  the barren ladder it climbs when a cycle produces nothing). None runs unless you install it.
- **campaigns** (`campaigns/`) — the verbs: the robust prompts to MAINTAIN / IMPROVE / AUGMENT / BUILD.

## First-pull runbook
1. **Get fast signal first:** `bash tools/check.sh --all-fast` runs the cheap scaffold checks. The
   isolated `bash tools/selftest.sh` is a boundary/release verifier, not an ordinary per-task ritual.
   Use `docs/TESTING.md` and `skills/verification-closer/` to choose proportionally.
2. **Read, in order:** this file → `README.md` → `campaigns/00-orchestration-method.md` (how to run
   work well) → `OPERATING-AGREEMENT.md` (the working laws) → `SECURITY.md` (the actual trust boundary)
   → `adapters/django/MOUNTING.md` (how the hub
   mounts) → `adapters/django/HUB-API.md` (the API you drive the hub with — read this before you POST
   anything). Skim `PROJECT/DOCTRINE.md` for the in-repo law.
3. **See it:** in `example/`, `DEBUG=1 python manage.py migrate && seedhub && runserver`, open `/hub`.

## How to USE it (stamp a new project)
`bash init.sh <target-dir> <project-key> "<Brand>" [live-url]` → a governed, git-initialized project with
the plane + hub + governance files, placeholders substituted. Then: mount the adapter per
`MOUNTING.md`, `seedhub` the genesis board, wire `hubaudit` as a CI/pre-deploy gate, and adopt the
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
disposable `verification-closer` only when consequence or sampling warrants it.

## The two dials you should know
- **`HUB_DONE_STRICTNESS`** (`tracked` default | `strict`) — flow-first vs proof-first completion. A
  command is optional in tracked; when present, completion requires the worker's matching typed
  exit-0 receipt. Strict also requires a command and resolvable evidence. See
  `adapters/django/MOUNTING.md` → "The strictness dial". Start `tracked`; go `strict` when completion
  claims need mechanical proof. Strict does not make an untrusted token holder safe.
- **Entity extensibility** — a new hub type is a schema + write path + tab. `campaigns/augment-hub.md`
  is the exact recipe; the base types were built this same way, so an added type is first-class.
- **`HUB_WORKER_LAUNCH_ENABLED`** (`False` default) — opt-in local process launch. Read
  `adapters/windows/README.md` before enabling it. The browser receives only a CSRF-minted,
  action/task/count-bound grant; authoritative consume remains write-token-gated.

## Security boundary you must not infer away
- **Unauthenticated does not mean sanitized.** `/hub` reads expose the complete projected board.
  Keep sensitive data out or add a real authentication boundary.
- **The write token grants terminal board authority, not code execution.** A writer sets
  `verification_command`, which the
  worker runs OUT-OF-BAND, submitting a typed exit-0 receipt the hub validates — the hub never
  executes it (that was an RCE path and is gone); strict URL evidence IS still fetched by the server.
  Treat every token holder as trusted at the Hub service-account/network boundary. `SECURITY.md` is
  authoritative for this threat model.
- **Normative docs are not automatic controls.** `PROJECT/verify/`, campaigns, and patterns describe
  roles and contracts. A canary, alert, backup, verifier, or conformance scan exists only after it is
  wired and tested in the adopting environment. `docs/ARCHITECTURE.md` lists shipped guarantees.

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
- **Optional entity types (Findings, Lessons, Decisions-log)** — generic and reusable, but kept OUT of
  the minimal base. Add them via `campaigns/augment-hub.md` if you want them; they're an intended
  extension, not a gap.
- **No LICENSE** — none, by choice (public repo → viewable but all-rights-reserved; add a permissive
  license only if a teammate needs to legally reuse it).

## The three non-negotiables (the point of the whole thing)
1. **Evidence is truthful and proportional.** Do not call implementer evidence independent, but do
   not burden minor work with the full ladder or a second agent. There is no unit battery here at
   all: a guard is proven by watching it fire, a feature against the real example app.
2. **Independent verification is boundary-triggered.** A fresh closer handles releases, privileged
   boundaries, migrations, public contracts, regressions, and occasional samples, then exits.
3. **Never lose or clobber concurrent work.** Targeted commits only; another session's uncommitted files
   are read, never staged; persist as you go so a killed run loses nothing.

## Provenance
Extracted 2026-07-07 from a working multi-project estate and updated 2026-08-03 with the portable
worker-launch trust boundary and repository-wide documentation contract; every file is scrub-verified
agnostic. If you change `PROJECT/`
templates, re-run `python tools/build_bootstrap.py`
so the bootstrap doc stays byte-exact. Use `bash tools/check.sh` for pending changes and reserve the
full isolated self-test for a justified verification boundary.
