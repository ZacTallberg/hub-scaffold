# Changelog

**This repository IS the canonical template** (since 2026-08-09; from 2026-08-07 to then it was a
generated export of a private upstream, now retired — its engine history is fully carried here).
Improvements are committed directly, gated by `tools/scrub_check.sh` (nothing project-, host-, or
person-specific) and proven by `tools/selftest.sh` against the real example app. Changes born
inside a working instance arrive as curated, scrubbed upserts after the instance has proven them
in production — never as a bulk merge. The 2026-08-07 consolidation resolved a three-way engine
fork; committing anything here that an instance also carries divergently is how that fork starts
again, so upsert whole units, not fragments.

This file records changes to the scaffold itself. Project changelogs generated from adopted Hub
deploy events are a different artifact (`hub_core.projections.render_changelog_md`).

## Unreleased

### Hub Excellence Contract and live throughput cockpit

- Made `PROJECT/HUB-QUALITY.md` canonical for extraordinary visual design, purposeful motion,
  realtime truth, accessible interaction, performance, flow metrics, and durable agent coordination.
- Added `campaigns/elevate-hub.md` and a focused contract verifier, propagated through orientation,
  governance, mounting, construction, and bootstrap paths.
- Reconciled cockpit theme, responsive, keyboard, print, palette, and live-update contracts; strengthened
  event-time history, heartbeat presence, WIP enforcement, atomic pickup, and proof truth.

### Interop truth, portable identity, and bounded realtime correctness

- `start_task` now delegates the entire lease + `todo -> in_progress` transition to the claim
  seam. It no longer follows a successful claim with a schema-invalid `active/planning_state`
  update that hid the granted lease behind an MCP tool error. MCP argument errors are JSON-RPC
  `-32602`, `tasks/get` no longer imports a removed helper, and discovery identity is read live.
- `init.sh` now emits `PROJECT/project.json` with `key`, `brand`, `app_name`, `app_host`, and the
  per-project `worker_scheme`; the runnable example carries and proves its own identity. Receipt
  predicates, MCP, discovery signing, and the launch default share that source.
- Root agent discovery is explicit about the protocol boundary: no A2A task transport or A2A
  streaming is advertised. The only callable protocol it names is the MCP endpoint that exists.
- Snapshot ETags now cover the complete representation, including lease-only heartbeats and
  telemetry. Delta reads page to the exact folded cursor, so event 501 and append races cannot be
  omitted while the response advances past them.
- Removed three capability-looking modules that were not callable scaffold capabilities:
  `ownership` had no shipped register/builder or schema fields and its sole projection hook was a
  no-op; `bitemporal` had no route/import and targeted fact types/validity fields the base schemas
  do not admit; `caches` existed only for the deliberately deleted single-interpreter battery
  runner and had no runtime consumer. The append-only event history, live lease fencing, adaptive
  WIP, scheduling, and per-task provenance remain intact.
- Removed phantom optional entity projections with their stale registration hook. The fold, ID
  grammar, snapshot, routes, and mirrored schemas now agree on the seven shipped base types;
  optional types remain an end-to-end augmentation, and the tamper helper reads commit SHAs from
  canonical task provenance instead of an unsupported `commit` entity.
- Corrected every live contract that still claimed the Hub executes `verification_command`. The
  worker executes it out-of-band; the Hub validates the typed receipt.
- The mounted-app self-test now covers identity/discovery, MCP start/finish, cursor/delta/SSE
  framing, and representation ETag behavior.

### A design pass: the board became something to look at

The cockpit was correct and quiet — clean cards, right numbers, no presence. Three defects,
measured in the rendered page rather than argued about:

- **A ~600px dead zone in the hero.** `justify-content: space-between` on a flex row pinned the
  stat tiles to the far edge and opened a void in the middle at wide viewports. Now a grid whose
  stats claim the remaining track and wrap into their own auto-fit row: **600px → 28px**.
- **The dependency chart floated in an empty box.** First `height:auto` scaled the drawing off the
  card's width and opened a 400px void under four dots; the fix for that (`width:100%`) caused the
  opposite failure, letterboxing a small graph into the middle of a 1300px frame. The height is
  now fixed and the width FOLLOWS the viewBox, so a 2-layer board draws a small centred graph and
  a wide board draws a wide one that scrolls — the drawing is always its own size.
- **Every card was the same flat slab**, so the eye had nowhere to land.

**The living backdrop** is the new piece, and it is a READOUT rather than decoration: an aurora
field behind the whole board whose brightness and tempo are driven by how many workers actually
hold a lease, and whose hue follows the attention rail — amber when something needs a human, red
when the audit fails. An idle board is nearly still. Glance at it from across the room and you
know whether anything is happening before you read a number. It is inert by construction
(`pointer-events:none`, `z-index:-1`, `aria-hidden`) and fully disabled under
`prefers-reduced-motion`.

Also: cards now sit ON that field with a translucent, blurred ground (so the aurora reads as depth
without eating contrast), the hero carries the page's only full-bleed treatment, the frontier
chart gained curved flowing edges / breathing halos / per-layer width labels, and the overview
cards ARRIVE staggered on mount — once, never on a live patch, which would make the page flinch
every time a worker heartbeated.

### The interop edges, the missing writers, and the rest of the engine

Everything the instances carried that is not domain-specific now lives here.

**Standards-speaking edges.** `POST /hub/api/mcp` is an MCP server (Model Context Protocol
2026-07-28 + the tasks extension) over the board: JSON-RPC 2.0, token-gated, stateless, with
`board_next` / `spec_task` / `start_task` / `finish_task`. It never touches the ledger directly —
every mutation goes back through the same `/hub/api/*` seam a worker uses, so the receipt gate,
lease fencing, OCC and schema validation apply unchanged. `/.well-known/agent-card.json` is signed
agent discovery: one skill per task `work_kind`, read live from the schema so it cannot drift, and
an explicit pointer to the real MCP transport — the token value never appears.

**Four entity types had schemas and no writer.** `gap`, `feat`, `note` and `deploy` could be read
and validated with no way to create one through the API. Added, with identity DERIVED where the
content supports it: `feat`/`note` mint a slug from their own name and `deploy` keys on its sha
(one release, one record), so a retried POST updates instead of minting a twin.

**Delivery.** `done` is a claim about a receipt; *landed*, *deployed* and *live* are three other
questions, and collapsing them is how a board reports success for work sitting on a branch. Each
leg has one evidence source, and a leg that cannot be measured here reports UNMEASURED — never
false, never quietly true. Counts are COUNTED from what measured true, never done-minus-alerts,
because subtraction silently promotes every task whose ancestry nobody asked about.

**Engine.** `verifier` (argv-form execution, scrubbed environment, and a spec-time exfil gate),
`metamorphic` (properties that must hold between two folds — the corruption class no oracle can
see), `collision` (mint-time twin detection), `judge` (position-swap invariance + a calibration
floor), `bitemporal` (valid-time/transaction-time `as_of` replay), and `caches` (structural
discovery of process-level memos).

Fixed while porting, because a port is not done until it runs here:
- **`collision` read a hardcoded `game:` id prefix** — origin-specific residue the scrub gate
  cannot see, because the word is ordinary English. On any other board that pattern matched
  nothing, and a detector that quietly finds nothing is indistinguishable from a clean board. Now
  matches the real id grammar. It also ignored `touches` — the one field that exists to state
  which surfaces a task changes — and parsed prose instead. Proven to fire on a seeded twin AND
  stay quiet on unrelated work.
- **The agent card 500'd on a missing identity field** and imported `cryptography`
  unconditionally. Portable identity now guarantees the field, and an unsigned card is served
  with a stated `signatureStatus` when signing support is unavailable.
- `work_kind` added to the task schema (with the conditional rules that make it enforce rather
  than label), since the agent card publishes one skill per kind.

Deliberately NOT ported, as instance-specific rather than template material: a gameplay-balance
tuning console, a single-domain site packager, and a domain-specific directory card.

### The board became LIVE, and became a cockpit

The scaffold rendered a correct board that never moved: no SSE, no delta, and a client that was
~27% of what the working instances had grown. A dashboard you must reload is a dashboard nobody
watches, so this closes the whole gap in one unit.

**Realtime.** `GET /hub/live/events` is a bounded Server-Sent Events cursor emitting event
IDENTITY only (`{seq, ts, event, aggregate, version, agent}`) — never payload content — with
`Last-Event-ID` resume, heartbeats to defeat proxy buffering, and a closing `reconnect`. The
browser learns THAT something moved and re-reads the canonical board to learn what, so animation
never becomes a second source of truth. `GET /hub/delta.json?since=` patches a held snapshot
(entities *and* the cockpit blocks, so the most-watched part of the page is not the last to
move); `GET /hub/cursor.json` is a contents-free liveness cursor for canaries; `hub.json` now
answers **304** on a matching `If-None-Match`. The client degrades to polling, then to a manual
sync, and says which mode it is in.

**Cockpit.** Progress hero with monotonic counters and a completion sparkline, the "needs the
operator" attention rail, per-agent fleet cards with live plan-step progress, an in-flight task
stage, the work queue split into ready / needs-spec / waiting-on-a-timer, an activity feed
carrying the receipt that granted each completion, facet bars, and a real focus trap with `inert`
— which the shell had *promised* since it was written and never implemented.

**Two blocks that did not exist anywhere.**
- `hub_core/adherence.py` — **is the board still being followed and kept current?** Six
  dimensions (specced, proven, evidenced, fresh, current, moving), each carrying its denominator
  and its unmeasured count. An empty denominator reports `null`, never 100%: a board with no done
  tasks is not a perfectly-proven board. The composite averages only measured dimensions and
  names the ones it skipped; the ring draws unmeasurable segments as ghosts so "nothing to
  measure" cannot look like "everything passed".
- `hub_core/dag.py` gained `critical_path()` and layer membership, so the cockpit can DRAW the
  dependency frontier and the longest chain instead of asserting a number the operator has to
  take on faith.

Also ported, environment-agnostic: `failure_taxonomy` (what kind of refusal the fleet keeps
hitting), `telemetry` + `cost` (the OTLP GenAI aggregate and its dollarized fold), and
`identity` — rewritten to resolve from `PROJECT/project.json`, then env, then a packaged default,
because the scaffold must boot on a fresh clone with nothing edited.

### Fixes the audit surfaced

- **The RCE description outlived the RCE.** `governance/AGENTS.md.template` and
  `CLAUDE.md.template` still told every agent the write token was "command-execution-grade
  because task verification commands run on the server". The 2026-08-08 sweep corrected nine
  surfaces and missed these two because they are named `*.md.template`, so every `-- '*.md'`
  pathspec skipped them. The first file a worker reads was still describing a vulnerability that
  no longer exists as if it were the security model. Same stale wording removed from
  `task.schema.json` and the example seed.
- **`not_before` and `poison_blocked` were unreachable.** `hub_core/project.py` reads both, but
  `task.schema.json` omits them under `additionalProperties: false`, so durable timers and the
  poison circuit-breaker could not be set through the write API at all. Added, with
  `poison_reason`.
- **The base type contract had drifted.** The schemas and routes shipped seven entity types while
  the fold, ID grammar, and snapshot projected unsupported optional nouns. The base is seven again;
  extensions must be added end-to-end via the augmentation recipe.

### 2026-08-09 (later) — licensed, and the last battery-era doctrine out

- **MIT LICENSE added** (Copyright (c) 2026 Zac Oberg); README's "no license granted" section
  replaced accordingly.
- **The gate doctrine is now proven-at-write everywhere it is stated**: OPERATING-AGREEMENT and
  PROJECT/DOCTRINE (+ regenerated bootstrap) no longer send a gate's refusal fixture to a
  "release battery" that no longer exists — seed a positive, watch it fire, quiet on a negative,
  receipt both runs, leave no fixture file.
- SECURITY.md's adopter checklist no longer implies the hub can execute commands; it now warns
  against re-adding server-side `verification_command` execution (the removed RCE).

### 2026-08-09 — re-export: the no-battery regime, the operator off-switch, and the last ops scripts out

- **The unit battery is gone, with the machinery that would regrow it** (upstream ruling
  2026-08-08). `hub_core/tests/` deleted; `selftest.sh` step 2 and `check.sh --all-fast` re-subjected
  to a compile/import floor; `docs/TESTING.md` rewritten as the verification doctrine (a guard is
  proven by watching it fire; a feature against the real example app — step 5 is unchanged and is
  the model); CONTRIBUTING/campaign prompts no longer assign test-writing obligations.
- **The write API refuses a bare suite runner as a task's proof** (`verification_command_is_a_suite`,
  422): a suite is green whenever the repo is healthy, whether or not the task's work happened.
  Proven both directions against the running example app before export.
- **`hub_core/audit.py` regenerated from canonical** (same transform: copy, LF, scrub vocabulary);
  the in-module oracle-tamper selftest left with the battery.
- **Every seat now has an operator off-switch**: `adapters/windows/launch-worker.ps1` polls
  `PROJECT/.hub/fleet-target` each cycle and disarms on ≤0; `patterns/worker-longevity.md` opens
  with the stop procedure — a fleet designed never to stop must still be stoppable, deliberately.
- **`deploy.sh.example` and `standing-canary.sh` are out; `standing-canary.md` replaces the cron.**
  The runbook is the deploy path; the standing re-check is a by-hand procedure with a receipt — a
  scheduled watcher's own death reads identical to "all green". `pre-receive-gate.sh` alone stays
  code, because a refusal that is prose does not refuse.

- **Engine forward-ported from the consolidated canonical (2026-08-07).** The July engine was a
  fossil relative to upstream: `store.py` gained the whole cross-process serialization layer it
  never had (`LedgerLock`, durable replace + fsync, jsonl tail hashing, fork linearization, and
  the heal that takes the write lock before dropping its append-only triggers); `audit.py` went
  from the generic core to the full guard suite; `ids/project/projections` gained the ADR-9
  entity types (commit, finding, lesson, method, review, telemetry) and the task-bed ordering;
  new engine modules `upcast`, `ownership`, `grandfather`, `wip`, `schedule`; the app shell
  gained its accessibility wrapper and live region. All under the agnosticism gate.

- Closed request-scoped EventStore handles across state, audit, read-snapshot, append, and decision
  paths while preserving caller ownership of explicitly supplied stores; added focused regression
  coverage for normal and exceptional exits.
- Replaced the per-change full-test expectation with impact-aware fast checks and a manually invoked,
  isolated full verifier.
- Added a reusable disposable `verification-closer` skill and campaign prompt with risk-based trigger,
  evidence, read-only, terminal-verdict, and exit contracts.
- Isolated the Django refusal ladder in a unique temporary ledger/database so repeated or concurrent
  verification cannot grow or contend on shared example runtime state.
- Completed a repository-wide documentation truth pass.
- Added canonical architecture, security, operations, testing, and contribution guides.
- Documented the command-execution and server-side-fetch authority of the general write token.
- Distinguished unauthenticated reads from automatically sanitized/public-safe data.
- Reconciled tracked and strict completion behavior across policy, API, templates, and examples.
- Closed the tracked-mode empty-evidence gap with input/schema enforcement and a refusal test.
- Made done-task evidence and accepted-ADR prose substantive schema requirements, not presence-only fields.
- Added automated documentation-link and mirrored-schema checks to the self-test.
- Made queue ownership truthful: claims now validate availability, atomically record `in_progress`,
  disappear from discovery while leased, reappear for stale reclaim, renew idempotently for the
  same owner, and release on completion.
- Fenced completion against lease-expiry and concurrent-task-edit races, and added an end-to-end
  queue refusal/recovery ladder.
- Added a Python 3.10/Django 5.2 and Python 3.12/Django 6.0 CI matrix to prove the documented
  compatibility range while excluding untested future Django feature series.

## 2026-08-03

- Added the optional fail-closed Windows local-worker launch bridge.
- Removed browser write-token/unlock UI and replaced it with a same-origin CSRF mint plus
  token-gated, issuer-bound, single-use consume.
- Added cross-process launch state locking and wrapper-lifecycle cleanup.
