# AGENTS.md — orientation for an agent that just pulled this repo

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
  token-gated writes, server-granted "done". Mounts at `/hub` in a Django site.
- **plane** (`PROJECT/`) — the durable on-disk tree: charter, doctrine, ADRs, registers, research,
  verification contracts, the leader/worker/verifier protocol. What a cold session reads to pick up.
- **patterns** (`patterns/`) — opt-in enforcement: deploy contract, standing canary, pre-receive gate,
  agent guard, conformance-scan spec. None runs unless you install it.
- **campaigns** (`campaigns/`) — the verbs: the robust prompts to MAINTAIN / IMPROVE / AUGMENT / BUILD.

## First-pull runbook
1. **Prove it works before trusting it:** `bash tools/selftest.sh` — runs the agnosticism scrub, the
   `hub_core` unit tests, the bootstrap-doc drift check, boots the example site, and drives the full
   write-API refusal ladder. All four steps must PASS. (Needs Python + Django — see `requirements.txt`.)
2. **Read, in order:** this file → `README.md` → `campaigns/00-orchestration-method.md` (how to run
   work well) → `OPERATING-AGREEMENT.md` (the working laws) → `adapters/django/MOUNTING.md` (how the hub
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
The engine behind all of them — fan-out, adversarial verify, persist-as-you-go, closer-commits-only —
is `campaigns/00-orchestration-method.md`. Scale the fan-out to the budget; sequential-with-checkpoints
when usage is tight.

## The two dials you should know
- **`HUB_DONE_STRICTNESS`** (`tracked` default | `strict`) — flow-first vs proof-first completion. See
  `adapters/django/MOUNTING.md` → "The strictness dial". Start `tracked`; go `strict` for untrusted
  completers (e.g. autonomous agents).
- **Entity extensibility** — a new hub type is a schema + write path + tab. `campaigns/augment-hub.md`
  is the exact recipe; the base types were built this same way, so an added type is first-class.

## What is deliberately NOT here (design, not omission)
- **The memory layer** — session-recall/persistence tooling is home-environment-specific and excluded
  on purpose. If you want cross-session memory, wire your own; the plane + hub are the durable record
  this system relies on, not a memory tool.
- **A runnable conformance scanner and a runnable resume-anchor script** — shipped as *specs/patterns*
  (`patterns/conformance-scan.md`, `campaigns/maintain-audit-reconcile.md` Prompt B), not scripts,
  because both are inherently org-specific (your live-URL shape, your project list, your alert hook).
  Implement them per environment from the spec.
- **The org-specific ship step** — `patterns/deploy.sh.example` has explicit `TODO` hooks
  (`BUILD_CMD`/`SHIP_CMD`/`ALERT_CMD`); the actual deploy target (a PaaS, Kubernetes, a VM, a container
  host, …) is yours to
  wire. The contract it must satisfy is fixed; the mechanism is not.
- **Optional entity types (Findings, Lessons, Decisions-log)** — generic and reusable, but kept OUT of
  the minimal base. Add them via `campaigns/augment-hub.md` if you want them; they're an intended
  extension, not a gap.
- **No LICENSE** — none, by choice (public repo → viewable but all-rights-reserved; add a permissive
  license only if a teammate needs to legally reuse it).

## The three non-negotiables (the point of the whole thing)
1. **Claimed-done ≠ done.** Prove results out-of-process — a gate re-run, a live probe, a re-read of the
   cited file — never an agent's own say-so.
2. **Verify before you record.** Findings and completions are adversarially re-checked before they enter
   the durable record.
3. **Never lose or clobber concurrent work.** Targeted commits only; another session's uncommitted files
   are read, never staged; persist as you go so a killed run loses nothing.

## Provenance
Extracted 2026-07-07 from a working multi-project estate; hardened by an adversarial review pass; every
file scrub-verified agnostic. If you change `PROJECT/` templates, re-run `python tools/build_bootstrap.py`
so the bootstrap doc stays byte-exact, and `bash tools/scrub_check.sh` before every commit/push.
