# THE PROJECT PLANE — canonical PROJECT/ framework (v1.1, 2026-08-03)

> canonical · owner: whoever leads the project · update: only by ADR (this file is the framework spec)

`HUB-QUALITY.md` is the canonical construction contract for every new or materially upgraded Hub.

Every app owns its code; **this folder owns everything about how the project is run**: decisions,
research, doctrine, gaps, verification, audit history, agent coordination. It is **content-agnostic**
— nothing in the framework refers to any particular app. It was crystallized from live-fire
multi-agent campaigns, the hub platform, and a hard-won doctrine corpus. Your organization's global
doctrine documents (lifecycle framework · method playbook · quality charter) are home-ecosystem
bindings, replaceable per §7. Where any older doc lists legacy PROJECT/ file sets
(TASKS/FEATURES/CHANGELOG/DEPLOYS markdown), **this manifest supersedes them**: those facts live in
the hub ledger now.

## Contract status: normative versus active

This tree is a portable framework and set of templates. The reference Hub makes the entity ledger,
schemas, projections, and `hubaudit` active after it is mounted. Empty directories/contracts such
as `verify/`, `runs/`, `worklogs/`, `audit/`, and the multi-seat `pm/` topology describe what an
adopting project must implement and operate; the scaffold does not silently create verifier tools,
run artifacts, monitors, alerts, backups, or deploys. A document may state a project law without
proving that its enforcement has been wired. Record that status honestly in `HANDOFF.md` and the
Hub.

## 0. Read-order for a cold agent

1. **`HANDOFF.md`** — you-are-here: current state, in-flight work, quirks. Always first.
2. **`CHARTER.md`** — what this project is, its quality bar, its definition of done.
3. **`DOCTRINE.md`** — the standing laws you must not violate.
4. **The hub** — `python manage.py hubaudit` + `/hub` (or fold `PROJECT/.hub/events.jsonl`) for
   canonical tasks/ADRs/gaps/features/deploys.
5. **`pm/PROTOCOL.md`** — only if a multi-agent campaign is active (HANDOFF says so).

## 1. The manifest

| Path | Artifact class | Canonical? |
|---|---|---|
| `README.md` | the framework spec + map | canonical (framework) |
| `CHARTER.md` | mission · scope · quality bar · definition of done | canonical |
| `DOCTRINE.md` | standing laws (operator contract + crystallized project laws) | canonical |
| `HANDOFF.md` | living continuity file — the single resume entry point | canonical, always current |
| `project.json` | portable identity: key · brand · app name/host · worker scheme | canonical identity for Hub/MCP/discovery |
| `seed.json` · `schema/` · `.hub/` | reference-Hub genesis · entity schemas · runtime hash-chained event ledger | `.hub/events.jsonl` = the entity ledger after the reference Hub is activated |
| `ADR/` | numbered decision records (full prose of record) | canonical prose; hub `adr` entity canonical for status/links |
| `research/` | deep research: dossiers, MoE panels, improvement-surface memos + `RESEARCH-HISTORY.md` chronicle | canonical |
| `registers/` | what hub schemas don't model: failure-mode taxonomy, incidents, truth matrix, blind spots, pending operator decisions, glossary | canonical |
| `audit/` | filed point-in-time audit artifacts (MoE registers, audit runs, security reviews) | canonical artifacts |
| `verify/` | independent-verification contract and target layout | canonical contract; harness is adopter-implemented |
| `runs/` | target shape for project-specific machine-readable run artifacts | canonical once a runner is wired |
| `worklogs/` | per-workstream execution logs with measured before/after | canonical |
| `ops/` | infra inventory / deploy runbook | canonical, date-stamped |
| `pm/` | multi-agent campaign kit: protocol, seats, channels | channels = operational log, NOT a governance store |

**Not in this folder:** tasks, gaps, features, deploys, capabilities — those are **hub entities**
(schema-validated, hash-chained, audit-gated). Markdown renderings of hub data are views and must
say so (see §3).

## 2. Where the audit history lives (the user-visible answer to "what happened?")

- **`.hub/events.jsonl`** — the tamper-evident spine for transitions actually recorded through the
  reference Hub: every task/ADR/gap/deploy transition,
  SHA-256 hash-chained, append-only. `hubaudit` verifies the chain + schema + referential integrity
  + build coherence; critical/high states block, while explicitly unknowable pre-first-deploy
  coherence is reported amber.
- **Hub `deploy` entities** — the required record for each deploy once the project's deploy writer
  is wired. The base HTTP API has no deploy endpoint; the deploy integration must validate and
  append it, and must derive `audit_ok` rather than accept a human assertion.
- **`audit/`** — dated point-in-time audit artifacts (MoE finding registers, review verdicts).
- **`verify/gate/`** — fail-closed ship-gate artifacts after the project implements the verifier
  contract.
- **`runs/`** — one JSON per operational run after project tooling is wired;
  `runs/status.json` is the contract's current green/red rollup, not a base-Hub output.
- **`registers/INCIDENTS.md`** — every defect instance: class, detection, resolution, detector born.

## 3. Source-of-truth law

1. **The hub is THE source of truth for all trackable state** — tasks, ADRs, gaps, features,
   deploys, capabilities, notes. One canonical store per fact class: hub = entities; markdown =
   prose + the registers above; channels (`pm/`) = coordination traffic only. Anything that
   contradicts the hub is wrong until the hub is amended.
2. **Every prose artifact opens with a role header**: `> canonical | view (source: X) | channel | template`
   plus owner and update trigger. A view that could be mistaken for canon is a defect.
3. **Views declare and never lead.** A rendered table of hub data carries
   `RENDERED VIEW — canonical: hub` and is regenerated, never hand-drifted.
4. **The ledger is LIVE.** Entity transitions are recorded at the moment of the event (claim →
   `in_progress`, decision → ADR, verified → `done`+evidence, deploy → deploy entity) — never
   batched, never reconstructed later. Doctrine or decisions born in pm traffic MUST be recorded
   (ADR + register + hub) before the traffic moves on — see `pm/PROTOCOL.md` §11; in campaigns
   the LEADER owns this personally.

## 4. ID namespaces

| Prefix | Meaning | Home |
|---|---|---|
| `ADR-NNNN` | decision record, gap-free ascending | `ADR/` + hub `adr` |
| task ids (`P<phase>-<n>` or slug) | hub tasks | hub |
| `L-`, `W<n>-`, `V-` + number | pm directives per seat (leader-issued) | `pm/seats/*/DIRECTIVES.md` |
| `OP-<n>` | operator-issued directive | any channel, marked `who: operator` |
| `FM-<grp><n>` | failure-mode class row | `registers/FAILURE-MODES.md` |
| `INC-NNN` | defect/incident instance | `registers/INCIDENTS.md` |
| `DP-NN` | pending operator decision | `registers/DECISIONS-PENDING.md` |
| `BS-NN` | blind-spot / missing signal | `registers/BLINDSPOTS.md` |
| run ids (`<UTCstamp>` / `<scope>-v<n>`) | runs and gate artifacts | `runs/` · `verify/gate/` |

New namespaces must be declared in `registers/GLOSSARY.md` before first use.

## 5. Amendment & supersession (one convention, everywhere)

- **Append-only artifacts are never rewritten.** To change one: add a dated
  `**Amendment (YYYY-MM-DD):**` block stating what changes and that it is authoritative over the
  text above — or supersede the whole artifact (new number/version + `superseded_by` both ways).
- **Numbering is repaired, never reused**: a collision or skip gets a `CORRECTION` entry; existing
  ids keep their meaning forever.
- **Voiding**: artifacts discovered to be wrong/fabricated are MOVED to an `_archive/voided-<ts>/`
  under their home dir plus a `void` event on the record stream — never silently deleted (the void
  trail is itself audit history).
- **Published identifiers are immutable** (seed titles, feed UIDs, ADR numbers, external URLs):
  changing one is a supersession event, never an edit.

## 6. Lifecycle

This folder is phase-agnostic; the lifecycle spine (CREATE → REFINE → DEPLOY → INTEGRATE →
MAINTAIN), the multi-expert (MoE) review method, and the four false-green enforcement primitives
live in your organization's global doctrine documents (see the header). `DOCTRINE.md` carries the laws that must be in-context at all
times; everything else is subsumed by reference — do not re-paste global doctrine here.

## 7. Portability — rebinding the Plane to any environment

The framework is a set of **roles**, not tools. It runs anywhere that can provide three substrate
roles; everything else in this folder is plain files:

| Role the Plane requires | Home-ecosystem binding | Rebind to (examples) |
|---|---|---|
| **Tamper-evident append-only ledger** (entity transitions, hash-chained) | `hub_core` store → `.hub/events.jsonl` | any event store, signed git log, ledgered DB |
| **Schema-validated entity store with false-claim-unsatisfiable rules** (done⇒verified_by+evidence, etc.) | hub entities + `schema/*.json` + `seedhub` | Jira/Linear + required-field rules, GitHub Issues + CI schema check |
| **Fail-closed gate runner** (audit + invariant checks, exit≠0 blocks ship) | `manage.py hubaudit` + deploy gates | CI required checks, pre-receive hooks, pipeline gates |

Rebinding rules:
1. **Every path outside this folder is a BINDING, not a dependency.** The global-doctrine docs in
   the header, the deploy atlas, and hub commands are the home ecosystem's instances; when
   pivoting, replace them with the target environment's equivalents and re-point the citations —
   the laws in `DOCTRINE.md`, the registers, and `pm/PROTOCOL.md` transfer verbatim.
2. **The protocol's channel mechanics are substrate-independent** (`pm/PROTOCOL.md` §13): append-only
   files + monitors are the proven floor; any addressable bus with per-seat ACLs may replace them
   by ADR without changing the event vocabulary or duties.
3. **What may never be rebound away:** identity separation at declared independent gates,
   fail-closed behavior at those gates, re-derivation over trust, append-only history, and one
   canonical store per fact class. An environment that can't provide these isn't a binding target
   for the protected boundary—it is a gap.
