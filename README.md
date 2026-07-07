# hub-scaffold

An **event-sourced, verification-gated project hub** plus a **content-agnostic PROJECT/
management plane** plus the **enforcement patterns** that keep both honest — extracted from a
working multi-project system and scrubbed of every origin-specific detail so it can be adopted
by any team, on any machine.

Three ideas, one package:

1. **The hub** (`hub_core/` + `adapters/`) — a small, framework-free event store and audit
   engine. Every task, decision, and ADR is an append-only event; state is a projection;
   "done" always carries who/what/evidence. Writes are token-gated over HTTP; reads are
   public-safe. The Django adapter mounts it at `/hub` in an existing site.

**Flow-first by design.** The tracking floor is deliberately cheap — create, claim, complete
with a note and a link; nothing else is mandatory. Proof requirements are a **dial, not a
default** (`HUB_DONE_STRICTNESS`, see `adapters/django/MOUNTING.md`): day-to-day work runs
`tracked`; flip a project to `strict` (dereferenced evidence + server-run verification
commands) only where completions can't be taken on trust, e.g. autonomous agents. Likewise
everything in `patterns/` is opt-in — adopt a gate when a failure mode has earned it.
2. **The plane** (`PROJECT/`) — a canonical on-disk tree (charter, doctrine, handoff, ADRs,
   registers, worklogs, verification specs) that any agent or human can pick up cold. It is
   the durable half; the hub is the operable half.
3. **The enforcement patterns** (`patterns/`) — the anti-false-green kit. The core failure
   mode this system exists to kill: gates that are self-attested, bypassed, or "passed" on
   code that never shipped. Every pattern here is an *out-of-process* check: a deploy contract
   with a build-SHA canary, a pre-receive gate on the git host, a standing canary for the live
   site, and an agent guard.
4. **The campaigns** (`campaigns/`) — the robust agent-prompt playbooks that operate all of the
   above: how to maintain, improve, and augment a project through multi-agent (or single-agent)
   work without losing state or shipping false-green. The *verbs* to the hub+plane's nouns.

## Layout

```
README.md                       you are here (human orientation)
AGENTS.md                       machine-first orientation — read first if you're an agent
requirements.txt                runtime deps (hub_core is stdlib; the Django adapter needs Django)
.github/workflows/ci.yml        sample CI: runs the scrub + selftest as a required, un-self-attestable gate
.gitignore                      keeps selftest/runtime artifacts (event logs, sqlite, pycache) untracked
init.sh                         stamp a new project from this scaffold (the only sanctioned way)
OPERATING-AGREEMENT.md          the human/agent working agreement adopters take on
PROJECT-PLANE-BOOTSTRAP.md      full spec of the plane, with every template embedded verbatim
PROJECT/                        the canonical plane tree (copied into your project by init.sh)
hub_core/                       pure-python engine: event store, projections, audit, validation
  tests/                        stdlib-unittest self-tests (no framework, run anywhere)
adapters/django/hub/            Django app: /hub pages, read API, token-gated write API
adapters/django/MOUNTING.md     how to wire the app into an existing Django site
adapters/django/HUB-API.md      agent-facing API reference: the operate-as-a-loop contract + every endpoint
example/                        minimal runnable Django site wired to the adapter (selftest uses it)
campaigns/                      the robust agent-prompt playbooks that RUN the system
  00-orchestration-method.md    fan-out → verify → close → roll up; the adversarial-verify rule
  maintain-audit-reconcile.md   MAINTAIN: reconcile code ↔ hub ↔ live ↔ docs; regenerate the anchor
  improve-moe-review.md         IMPROVE: multi-expert review → adversarial verify → committed report
  augment-hub.md                AUGMENT: add an entity type + tab, or backfill structure across repos
  feature-buildout.md           BUILD: the DISCOVER→CLAIM→IMPLEMENT→RECORD→VERIFY loop + roles
patterns/
  deploy-contract.md            what a trustworthy ship step must guarantee
  deploy.sh.example             reference deploy script implementing the contract
  standing-canary.sh            recurring liveness/freshness probe for the deployed site
  pre-receive-gate.sh           git-host hook: rejects credential-shaped pushes server-side
  agent-guard.py                guardrail for autonomous agents operating the hub
  conformance-scan.md           periodic drift scan across adopted projects
governance/
  CLAUDE.md.template            agent governance file (init.sh renames to CLAUDE.md)
  AGENTS.md.template            same, for other agent runtimes (renamed to AGENTS.md)
tools/
  build_bootstrap.py            (re)embeds PROJECT/ templates into the bootstrap doc; --check gates drift
  scrub_check.sh                agnosticism gate: fails on any origin-specific residue
  selftest.sh                   end-to-end proof: scrub + unit tests + doc check + example boot + API refusal ladder
```

## 10-minute adoption runbook

Prereqs: bash, git, python3, and (for the web hub) a Django project to mount into.

1. **Init** — stamp your project:

   ```bash
   bash init.sh ../my-project my-project "My Project" https://my-project.example.com
   ```

   This copies `PROJECT/`, `hub_core/`, `adapters/`, `patterns/`, the operating agreement, and
   the governance files (renamed into place), substitutes `{{PROJECT_KEY}}` / `{{BRAND}}` /
   `{{LIVE_URL}}` across all text files (fail-closed if any placeholder survives), and makes a
   genesis commit on `main`.

2. **Mount** — wire the hub into your Django site per `adapters/django/MOUNTING.md`: add the
   `hub` app, include its urls under `/hub/`. Never mount it at the front door of a public
   site; `/hub` is an operations surface.

3. **Seed** — set a write token and load the board genesis:

   ```bash
   export HUB_WRITE_TOKEN=<random-secret>   # writes are fail-closed until this exists
   python manage.py migrate
   python manage.py seedhub                 # imports PROJECT/seed.json into the event store
   ```

4. **Gate** — run the computed audit and make it block:

   ```bash
   python manage.py hubaudit    # exit 0 pass / 2 violation / 1 internal error (fail-closed)
   ```

   Wire `hubaudit` into CI and into your deploy script *before* the ship step. An audit that
   can be skipped is decoration.

5. **Deploy contract** — read `patterns/deploy-contract.md`, adapt
   `patterns/deploy.sh.example` to your infrastructure, install
   `patterns/pre-receive-gate.sh` on your git host, and schedule
   `patterns/standing-canary.sh`. The contract's heart is the build-SHA canary: after every
   deploy, the live site must serve back the exact SHA you shipped, checked by a process other
   than the one that claimed success.

Verify the whole scaffold itself at any time with `bash tools/selftest.sh`.

## What requires org-specific wiring

The scaffold is deliberately incomplete in exactly these places — they cannot be generic:

- **The ship step.** `patterns/deploy.sh.example` shows the contract (gate → build → ship →
  SHA canary → record), but the "ship" line is your platform's: container push, rsync,
  platform CLI, whatever. Keep the surrounding contract intact when you fill it in.
- **Alerting.** `patterns/standing-canary.sh` detects a dead or stale live site; where the
  alarm goes (pager, chat webhook, email) is yours to wire. A canary nobody hears is not a
  canary.
- **CI.** The audit gate (`manage.py hubaudit`), the bootstrap doc check
  (`tools/build_bootstrap.py --check`), and the hub_core unit tests are all plain commands —
  add them to whatever CI you run, and install the pre-receive hook on whatever git host you
  use.
- **Secrets.** `HUB_WRITE_TOKEN` must be generated per project and injected via your secret
  mechanism. It is never committed; writes are disabled (fail-closed) until it exists.

## Maintenance loop

Adoption is an event; staying honest is a loop:

- **Audit gate in CI, always blocking.** `hubaudit` runs on every push and before every
  deploy. When it fails, fix the violation or record an ADR that changes the rule — never
  bypass it. Bypasses are how false-green systems are born.
- **Conformance scan.** On a schedule (weekly is a good start), run the checklist in
  `patterns/conformance-scan.md` across every adopted project: gate still wired? canary still
  scheduled and heard? write token still fail-closed? deploy script still SHA-stamped?
  Drift is silent; the scan makes it loud.
- **ADR discipline.** Every decision that changes architecture, rules, or scope gets an ADR
  through the hub's write API (which validates and versions it) — including decisions to relax
  a gate. The board is only trustworthy if the decisions that shaped it are on it.
- **Doc integrity.** `tools/build_bootstrap.py --check` keeps `PROJECT-PLANE-BOOTSTRAP.md`
  byte-identical to the live templates, so the spec can never quietly diverge from what
  `init.sh` actually stamps.
