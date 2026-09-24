# Changelog

**This repository IS the canonical template** (since 2026-08-09; from 2026-08-07 to then it was a
generated export of a private upstream, now retired — its engine history is fully carried here).
Improvements are committed directly and kept portable with the explicitly invoked
`tools/scrub_check.sh` (nothing project-, host-, or person-specific). Changes born
inside a working instance arrive as curated, scrubbed upserts after the instance has proven them
in production — never as a bulk merge. The 2026-08-07 consolidation resolved a three-way engine
fork; committing anything here that an instance also carries divergently is how that fork starts
again, so upsert whole units, not fragments.

This file records changes to the scaffold itself. Project changelogs generated from adopted Hub
deploy events are a different artifact (`hub_core.projections.render_changelog_md`).

## Unreleased

### Delivery: addressed mail that lands, asks that never get stuck, a visibility veil

- **Busy ledger answers 503, never 500.** `StoreBusy` + jittered `BEGIN IMMEDIATE` retries inside
  one budget; `LedgerBusyMiddleware` answers any hub path `503 {code: busy}` + `Retry-After`; the
  write seam's own busy 503 and the middleware's record the same `ledger_busy` warning row (path,
  method, wait) through one helper; the client retries only refusals that state nothing was written.
- **Agents narrate their own work.** A bounded, append-only first-person updates feed
  (`POST /hub/api/agent-update`, `GET /hub/agent-updates.json`, `update --note --evidence`, MCP
  `post_update`, auto-posted by answer/ack/finish under `HUB_AUTOWORKER=1`); a lost write is a
  retryable 503 with its reason.
- **Asks never get stuck.** Every question carries `waited_s`/`age`, longest wait first (unknown
  ages last); `ask --to <agent>` addresses one agent; an ask unanswered past `HUB_ASK_UNSTICK_S`
  reaches every console except its asker's; `questions.json` and the board's Questions card lead
  with `N stuck — oldest X` (`HUB_ASK_STUCK_S`), and the attention rail ranks stuck asks first and
  orders every rank longest wait first (rows carry `waited_s`; unknown ages last, id only on ties).
  Anyone holding `ask:answer` may answer (the reply is built from the question, so it can neither
  broadcast nor carry a standing instruction).
- **Human-only gates.** `ask --human-only` (or `HUB_HUMAN_GATE_PATTERN` over title + body) files a
  gate that reaches the operator and is never widened; `HUB_GATE_RESOLVER` (cache-only) can report
  that the approval already landed, which turns the gate back into an ordinary question.
- **Agent-to-agent mail.** `POST /hub/api/message` / `message/ack`, `msg <agent> [--session]`,
  `inbox --ack`, MCP `send_message` / `ack_message`. Mail and answers are routed to the CONSOLE that
  asked (`from_session`); mail for a console that has ended falls through to the agent's most
  recently active live console, naming the original. A corrected answer bumps `delivery_revision`;
  an ack must name the revision it read (`428` missing, `409` stale). Old deliveries say when they
  were written.
- **Notification receipts.** `hub_core.receipts`: offered (only when the addressed set changes),
  delivered, failed (with the refusal), resolved; `GET /hub/receipts.json`, `receipts
  --undelivered`.
- **Per-console presence bound to projects and tasks.** Consoles report name, repo, app, state,
  runtime and recent files (the board shows a runtime chip on each console row);
  `GET /hub/activity.json` and `consoles` bind each console to the task
  THAT console claimed (leases record the claiming session; a claim is never inferred from a
  directory) and name the projects being worked with no task. `start` publishes the task title as
  focus and `finish`/`release` retract it; a bare id or stub is never a focus; agent cards headline
  the freshest working console. A presence observation that would only move a timestamp on a fresh
  row is skipped, and the inbox wait takes its slot before folding and reuses its last projection.
- **Performance, named for what it computes.** `RouteTimingMiddleware` + `GET /hub/perf.json`
  (`p50_worst_ms`/`p95_worst_ms` over process windows, samples expire after an hour, long-polls
  exempt, a worst-window slow-route verdict that also reaches the attention rail), per-phase
  snapshot timings in `live.timings_ms`, `?profile=snapshot` (one profiled build, `perf:profile`),
  audit schema checks reused for byte-identical entities, one lease read per snapshot, and lock
  waits bounded including in-process contention. The git head is memoized on what HEAD resolves
  through (stat only, no `git` spawn per snapshot or write); a write folds only its own aggregate,
  never the ledger; presence rows are memoized on the directory fingerprint; the incremental fold
  forces a full replay every 15 minutes.
- **The contributor veil.** `PROJECT/facets.json` declares facets hidden from lower tiers
  (`tiers.json`, `POST /hub/api/tier`, `tier <agent> --set`). Every read route declares `open`,
  `veiled` or `member` (the audit flags an undeclared one); hidden records are OMITTED from veiled
  JSON, the board's inlined snapshot, entity reads and the inbox (before fingerprinting); an answer
  naming a hidden facet to a contributor needs `disclose` + `veil:disclose`; `veil-audit` renders
  every veiled route as a contributor and reports any term that got through. Open for everyone when
  no facets are declared.
- **Per-prompt context in three channels.** `prompt-context` re-sends the doctrine only when it
  changes (or on session start), and live items whenever they move, with a per-session receipt.
- **Update Core Systems.** `HUB_MAINTAIN_URL` puts the manual repair pass in the navbar.
- **Patterns.** `patterns/agent-client-daemon.md` (delivery into consoles, a second runtime as an
  adapter, a self-updater that cannot break itself, headless children on Windows) and
  `patterns/route-reconciler.md` (per-entry refusal behind a mass-refusal guard, a report on every
  pass).

### Tasks that say what is happening to them

- **Claims are fenced and attributed.** A retried claim keeps the lease it renewed (only a lease
  the request CREATED is released when the transition loses a race), and a lease records the
  console that claimed it, so one of several consoles in a repo binds to its own task.
- **Typed checkpoints.** Plan items take `kind` (`checkpoint`, `pushed`, `deployed`, and scheduler
  kinds that are shown but never counted), `sha`, `pipeline_id`, `pipeline_url`; grown `auto`
  placeholders leave the denominator. `client step --sha --pipeline`, `plan --take`, and a bounded
  re-apply on a version race. One definition (`hub_core.checkpoints`) feeds every counter.
- **Task health.** `live.task_health` buckets every in-progress task as moving / ready to close /
  stalled / orphaned (moving wins; "ready to close" never claims the outcome and is withheld when a
  checkpoint says work remains); the board's Workstream card and the operator inbox (`task-stall`,
  `client inbox --text` → TASK ROT) read it, including unattended requests nobody started.
- **The unattended lane.** `task.unattended`, `next.json?unattended=1` (P0–P2, never a decision,
  never a task a live run is on), `client create --unattended`, `hand`, `unclaim`, and the
  orphaned-lease remedy: a lease held by a non-live console of the same agent can be let go of
  without its token; a live console's never.
- **Decisions are a person's call.** `work_kind: decision` is never unattended; open decisions reach
  the deciders' inbox with a board link; `POST /hub/api/task/decide` (`file | close | reply`) only
  for a `HUB_DECIDERS` credential, closing through the one done path; `task.decision` records it.
  Turning a task into a decision always clears `unattended`. A `reply` pages the decision's
  FILER: the fold now stamps `provenance.created_by` once from an entity's first event (schema
  and bootstrap updated), so an unrelated later edit no longer redirects the page to its editor;
  a task's stall owner and `recall` owner read the same field instead of the last writer.
- **Annotated task rows and a per-project feed.** Rows carry holder, responder, pushed, handed-back,
  deployed and a CI problem joined on the task's own sha/pipeline; `GET /hub/project/<slug>/tasks.json`
  with an ETag that moves only when a reader-visible fact does; `client recall` prints the trail.
- **A verified deploy closes the loop.** Tasks whose recorded commit is an ancestor of the deployed
  sha are stepped once per commit; unattended ones are finished by the hub; unanswerable ancestry is
  reported, never read as "no" (`HUB_VCS_ANCESTRY`).
- **Evidence lists.** Completion evidence may be a comma/space list of shas or URLs, each
  dereferenced and a bad one named; an unknown bare sha names the repository searched.
- **Idempotent create.** A create's idem key is looked up across every task (`idem_scope`), so a
  retried create answers `replayed: true` with the first record instead of minting a twin.
- **Consoles, honestly.** Presence takes a supervisor's session digest; `consoles.json` splits
  attended / unattended / finished runs with "idle N min, last did …"; crossovers between consoles
  (file, task, subsystem, configured topic) carry peer evidence and a suggested split, delivered
  once per side through the inbox (`patterns/coordination.md`).
- **Needs attention.** `hub_core.attention` → `GET /hub/attention.json`, `client attention`, MCP
  `board_attention` and a board card: each fixable condition with owner, exact fix, values and a
  persisted age, delivered to its owner and then the operator; unreadable sources silence only
  their own detectors.
- **Client telemetry.** Every client call sends `X-Hub-Client-Version`; stale seats are named, and
  `HUB_CLIENT_SELF_UPDATE=1` opts a client into a rate-limited, fail-soft `pull --ff-only`.
- MCP gains `create_task`, `hand_task`, `unclaim_task`, `recall_task`, `decide_task`,
  `board_attention`, `board_consoles`, `project_tasks`, `step_task`, `plan_task`.

### Tasks: who work is offered to, who is really holding it, and where it went

- **One offer rule** (`hub_core.offer`) for `next.json` and `take`: a task GIVEN to a named agent
  (`POST /hub/api/hand`, client `hand --to`, MCP `hand_task`) is offered only to them and sits in
  their inbox until claimed; a task with MACHINE AFFINITY (`machine`, client `create/hand
  --only-on`) only to a caller on that machine; an unattended run's escalation (`hop`, stamped by
  the Hub from `HUB_UNATTENDED`/`HUB_RESPONDER_HOP`) once more after a cooldown at hop 1 and never
  at hop 2. Withheld rows are counted by reason, never dropped silently.
- **A lease is held by a live console** (`hub_core.liveness`): another console of the same agent
  cannot renew a live one's lease; absence from a partial roster is never read as gone; in-flight
  rows and the board name the holding console and whether it is gone. **A GONE console releases
  what it held** after `HUB_GONE_GRACE_S` from its last-seen stamp: the sweep hands its task back
  and voids that lease, a claim or item claim takes it over (`took_over_from`), a renewal from
  another console takes over the recorded holder, and every refusal inside the grace says when it
  frees. UNPROVABLE never releases.
- **The Hub hands back abandoned work** (`hub_core.lease_sweep`) with one self-counting
  `handed_back` row, and **lifecycle rows are never counted as work** (`hub_core.plan`).
- **A lock timeout names its holder**: claims-lock waits that run out answer `503 lock_busy`
  with the holder pid (alive/dead), lock age and which lock ran out.
- **Evidence in the task's own project** (`hub_core.commits`, `HUB_PROJECT_REPOS`,
  `HUB_COMMIT_RESOLVER`): strict completion accepts a commit of the project the task is about,
  and "could not be asked" is never reported as "not a commit".
- **Lineage ladder** (`?lineage=1`, client `lineage`, MCP `task_lineage`, a Trace button on the
  task drawer): recorded commit → verified deploy → first release → serving now, with an
  ancestry cache that never stores an unknown.
- **The promotion lane** (new `held` entity, `hold`/`promote`/`abandon`/`held` verbs, MCP tools, a
  Held tab, rail items that escalate with age): finished work held back from live ages in public
  and is freed only with evidence; a commit on no remote is allowed only with a recorded reason.
  The Hub confirms a hold by FETCHABILITY (a remote-tracking ref or the forge resolver), never by
  a commit merely existing in a local checkout (`hub_saw: "local_only"` is kept on the record).
- **One responder per item** (`POST /hub/api/item-claim`, client `item-claim`, MCP
  `claim_item`): a per-machine TTL claim on a question or error fingerprint; a claimed item reads
  "in flight on <machine>" on the rail and the error card instead of "unclaimed".
- **Unattended answers are stamped** (`unattended: true` plus one line in the text).
- **The client survives non-latin-1 header values** (transliterated) and reports a request it
  could not build as a local fault, never as an unreachable Hub.
- `patterns/ci-evidence.md`: zero-job pipelines, raw event retention (rotate before write),
  fail-soft vs fail-silent, line writers, skipped schedules.

### Operations core: authenticated reads, CI verdicts, deploy records that survive, components

- **Reads are authenticated by default.** Every read route is wrapped by `read_auth.reader`; an
  anonymous read answers 401 (or redirects to a sign-in page that actually exists) and is
  recorded. `HUB_READ_AUTH = "public"` is the only way to open a board, a typo never does, and
  the audit flags it outside DEBUG. The deploy runbook adds the anonymous-request check.
- **Server-side read-then-append retries both concurrency refusals** (409 and the 428 of a
  first-ever write that lost its create race), bounded; when attempts run out the last status and
  code travel on the response and into the error stream.
- **A failed CI job is classified by what its log says** (`hub_core/ci_trace.py`):
  `POST /hub/api/ci-failure`, the `ci-failure` client verb and the `report_ci_failure` MCP tool.
- **Deploy records survive a cold hub.** `python -m hub_core.client deploy` posts the immutable
  release closure with growing per-attempt timeouts (20/45/90 s), one `DEPLOY_RECORD_RETRY` line
  per transport failure, never a retry of a refusal — safe only because the record is
  idempotent by sha, so a lost reply comes back `idempotent: true`. The record's `at` is not part
  of the proof: a re-run job re-posting the same release later is answered idempotent with the
  first `at`, not refused. `record_deploy` joins the MCP tools and stamps `at` when omitted.
- **Standard components and app skeletons.** A `cap` of `kind: component` carries what/when/get/
  entry/delivery/exemplar/depends_on; a `kind: skeleton` names components (`applies`) or takes
  all of them in dependency order (`applies_all`). `GET /hub/components.json` resolves skeletons
  on every read — each component's CURRENT get/entry, `missing[]`, and `order_problems[]` for an
  unknown dependency or a cycle, never guessed around. Reachable from the `components` and
  `capability` client verbs, the `list_components` / `register_component` MCP tools, and a
  panel leading the Capabilities view.
- **The board reads records whole and patches instead of rebuilding** (`surfaces.js`): the detail
  dialog loads the full record and renders every field it carries (checkpoint notes as a
  timeline, structured values as structure, record ids as in-place links, only http(s) as
  external links); any `?tab=<view>#<type>-<local>` address resolves even outside the snapshot,
  at load and whenever the address changes in place, with an explicit failed-read state and retry; live changes reconcile rows by record identity so
  focus and scroll survive; reads of one URL in flight share one request; the palette ranks an
  exact record address above fuzzy matches.
- **New operating write-ups:** `patterns/ci-pipeline.md` (never auto-cancel a pending
  forward-only deploy, change triggers on push only, job ceilings that count the fetch,
  publications from the protected ref), `patterns/app-slots.md` (stage / cut over / retire /
  re-adopt), `patterns/directory-sign-in.md` (401 vs 503, one bind per rejected credential),
  `patterns/shared-app-banner.md` (one linked header master), and deploy-runbook additions for
  cold-start probes with printed latency, one-deadline rendered-page passes, vault skips that
  run before the prune.

### Seats, CI results and the gate kit

- **CI results become board rows.** `POST /hub/api/ci-event` takes a GitLab pipeline/job webhook
  or a generic `{kind, project, status, ...}` event, gated by a webhook secret (refuses everything
  without one; a named `_hub_secret_gated` gate the route audit accepts). Deploy/verify failures
  are critical, a zero-job failed pipeline is a critical configuration rejection, a later green on
  the same job and ref retires the red (a pipeline success only the jobs it ran), a recurrence
  reopens, a rollback is a critical row no green retires, duplicates are dropped and the raw body
  is kept (rotated before write). `GET /hub/ci-events.json` (needs `ci:read`), client `ci-report` /
  `ci-events`, MCP `ci_events`, a "CI and deploy results" coverage channel, and
  `adapters/gitlab/ensure_ci_hooks.py` (create-only project-hook reconciler; unknown is never ok).
  The error store's context now keeps `project/ref/sha/job/jobs/trigger/restored_sha` — dropping
  them let a green on one branch retire a failure on another.
- **Distribution: is every seat running what this hub publishes?** Seats send `X-Hub-Client` and
  `X-Hub-Artifacts`; `GET /hub/distribution.json`, client `distribution`, MCP `seat_distribution`
  and a board card grade each seat (LF-normalized, version split before grading), name offline
  seats without calling them drift, collapse 72h-gone seats, list phantom callers and legacy rows,
  and lead with a verdict that states what it did not grade. Silent seats (6h) and persistent drift
  on online seats reach the operator's inbox as `offline` / `drift` items.
- **Phantom callers are not computers.** A presence row with a machine but no kit telemetry is
  kept out of every device list and count; the agent card shows every computer a person has with
  its own check-in state, per-computer consoles, and a same-file warning.
- **Built.** `GET /hub/built.json`, client `built`, MCP `built_by_person` and a board card derive
  what each person built from the ledger, folding machine identities into their person through raw
  presence rows (most recent reporter wins; a machine name never labels a person).
- **Files touched and file crossovers.** `X-Hub-Files` on presence, `file_overlaps` on the live
  block, and `patterns/presence-gate.py`: a harness hook that derives a console's edited files
  from its transcript per command segment (heredocs, redirects, commits, copies, `cd`, scripts and
  subagents handled), folds indented env-report continuation lines, and optionally restores native
  terminal text selection (the blunt fallback announces itself).
- **Client transport and durability.** Several comma-separated routes to one hub, swept
  breadth-first under a wall clock and failed over only on a route failure; separate read/write
  timeouts; an offline queue replayed as the console that queued it, with dead letters that are
  replayed or archived, never deleted (exit 4 queued, 5 outcome unknown); `record` and `step`
  retry one lost optimistic-concurrency race (MCP `record_entity` too). Presence heartbeat
  default is 60 s.
- **Patterns.** `deploy-hardening.md` (config sync retries transport only and treats gateway 5xx
  as unreachable, snapshots never print success without a snapshot, the deploy-record post picks
  TLS trust by runtime, readiness gates adopted on evidence, failure outcomes reported) and
  `machine-push-identity.md`.

### The upsert, completed to every seam the scaffold already speaks

The first pass landed the capabilities; a re-audit found they were reachable only over raw
HTTP — invisible from the two mouths this scaffold actually gives an agent. Closed:

- **`python -m hub_core.client` speaks the whole loop.** `ask`, `inbox`, `wait` (whose
  `--follow` mode loops and prints each CHANGED addressed set as one JSON line — the building
  block for a desktop notifier or supervisor hook, with the fingerprint round-tripped so
  unrelated board traffic never produces output), `answer --crystallize`, `ack`, `directive`,
  `presence` (X-Hub-* headers from environment or flags), the error-stream verbs, `search`,
  `questions`, and `whoami`. The sanctioned seam and the capability set are one thing again.
- **MCP clients can ask.** `ask_operator`, `check_inbox`, `ack_directive`, and `search_board`
  join the board tools, routed through the same write seam as everything else.
- **Answering can crystallize.** `answer {crystallize:true}` additionally mints a standing
  knowledge note the duplicate-ask guard matches — OPT-IN, because on the origin system the
  automatic version minted thirteen "lessons" in one afternoon, most of them requests that got
  fulfilled rather than rules anyone should carry. Proven both directions: the crystallized
  note exists and tags correctly, and a RESTATED question is refused citing it.
- **The board renders the surfaces, not summaries of them.** The Questions card became a
  thread view derived client-side from the collections the snapshot already carries: open
  threads lead with their waiting age, an answered thread shows the reply as a turn plus
  whether delivery LANDED ("answered — awaiting the asker's ack" — answered and delivered are
  different facts), per-asker lanes, and a 14-day asked-vs-answered strip. The errors card
  gained the per-channel COVERAGE chips (live vs silent — an empty card whose channels are
  dark must never read as good news) and names its below-the-bar row count instead of hiding
  it.
- **`patterns/error-visibility.md`** — the sending half of the error stream: the LOGGING
  handler for the host app, a bounded fail-soft forwarder for satellite services, scoped
  `error:report` credentials, and the properties to preserve when adapting it. Wire it before
  the first feature; a service that keeps failures in its own log can only be debugged from a
  shell on its host.
- **The convergence sweep, after the sibling seat's best-of-both pass landed.** Four
  leftovers neither seat had carried: `POST /hub/api/forget-presence` (`presence:manage`) —
  the engine shipped `presence.forget()` with no door, so a phantom seat was permanent; a
  decommissioned laptop now leaves every open board immediately, and dropping everything is
  refused. Overdue directives reach the attention rail naming their unacked targets, and the
  Directives tab surfaces the deadline — red only while ACTIVE, because a fulfilled
  directive's past deadline is history, not an alarm (the schema had promised this surfacing
  since the type landed; nothing delivered it). Error rows open a real detail modal — the
  stack-trace `details`, context fields, origin, collapsed-repeat count and claim state were
  served with every row and rendered nowhere, which sent the reader to a server shell for
  the way in. Agent cards open the per-agent detail view (the coordination surface): the
  held task as a chip with its plan and last checkpoint, every live console with its focus,
  the recent trail — and a claimless-but-active console finally has somewhere to open. Plus
  one robustness fix: the client's `wait --follow` notifier now survives a transient hub
  outage with capped backoff instead of dying silently exactly when the hub comes back with
  news.
- `hubaudit` was re-run against the final tree: the route-guard audit certifies every new
  endpoint (zero critical/high), which the first pass had asserted but never executed.

Proven by a second disposable probe against the served example app: 21 more checks across the
client verbs (including live presence-header observation from the CLI environment), the MCP
tools end to end, and the crystallize round trip — plus a fresh read of the rendered Signals
section in every thread state.

### A working instance's two weeks in production, upserted: delivery, presence, the error stream

A working instance ran this scaffold's shape as a multi-machine agent board for two weeks and
grew, under load, capabilities this repository had only implied. Everything below is the generic
form of what that instance PROVED — curated whole units through the scrub gate, never a bulk
merge — re-derived onto this base's architecture (scoped credentials, the push-realtime plane,
proof-without-test-accumulation) rather than transplanted, and proven again here by a disposable
probe against the real served example app plus a read of the rendered board.

**The ask/answer loop, closed.** The board could record a question beautifully and still leave
the asker blocked in silence — the only delivery mechanism was somebody happening to have a
browser tab open, and on the origin system a member once waited a day on a question the answerer
never saw. Now: `POST /hub/api/ask` mints a first-class question (its `asker` is a stable field,
never derived from provenance — which answering REWRITES, so the origin's prefix-convention
mis-addressed every re-answered reply; this port fixes the class instead of carrying the
workaround), `GET /hub/inbox/wait` blocks until something is ADDRESSED to you (bounded ≤25s, a
small waiter pool that degrades to an honest poll — and the fingerprint of the addressed set,
not the event cursor, decides a wake, so unrelated board traffic never trains anyone to ignore
the channel), answering is ONE verb (the reply directive targeted at the asker AND the question
retired — a half-done answer is worse than either half), and the asker's ack closes delivery,
with a fully-acked directive retiring itself because a delivered queue that never leaves
"active" is a queue people learn to ignore. A restated question is refused with the matching ids
(Jaccard over significant tokens, both surfaces — open/answered questions and any adopted
knowledge notes), and the guard FAILS OPEN in both directions: a duplicate is cheap and visible,
a lost question is neither. `directive` and `ack` are first-class entity types (schema + writer
+ tab + fold), authority sits in the existing scope system (`directive:write` — the shared-root
credential holds it; a worker credential is issued it only deliberately), and `questions.json`
reports what the feed is actually about: how long each person waited, the longest wait and
whose, median reply time, per-asker lanes, a 14-day strip.

**Observed presence, per console.** "Who is working right now" was answered from leases alone —
promises with a TTL, not observations, and an agent working WITHOUT a claim was invisible. Now
every authenticated write refreshes a presence row keyed per (agent, machine) from optional
`X-Hub-*` headers, sessions live INSIDE the machine row (id + working directory + FOCUS —
agent+machine cannot tell four live consoles from one console checking in often, and two
consoles unknowingly on the same thing is the most expensive duplicate a fleet produces), and
`POST /hub/api/presence` is the heartbeat that separates "acted recently" from "still there".
A claimless console with a fresh focus renders ACTIVE with what it is doing, never idle — the
exact case that makes a busy fleet look asleep. Rows unseen past a horizon self-retire into
`_retired/` (archive over delete; roster surfaces otherwise become museums), every stored
timestamp is coerced (one string-shaped stamp in a years-old sidecar must degrade to "unknown",
never TypeError the whole board to a 500), and the machine-row projection is a DENYLIST — the
origin's allowlist silently dropped three collected fields on three separate occasions before
that lesson stuck. Fixed while proving it here, because a port is not done until it runs: the
card's age check read `(age or huge) < 300`, which sent a console that pinged ZERO seconds ago
— the most active possible — to idle, because 0 is falsy. The probe caught it only on a fresh
board; every earlier pass had a second of clock drift hiding it.

**The operational error stream.** The audit proves the LEDGER; nothing proved the system around
it, and a served 500 existed only in a service log on a host nobody reads. `hub_core/errorlog`
is a bounded, redacted sidecar with ingest for every surface the audit cannot see — the host
app's 5xx (a logging handler), satellite services (`app-error`, attributed to the APP), worker
tooling (`agent-error`), and the board's own browser (CSRF-gated, size-bounded). What made the
origin's stream trustworthy is ported intact: redaction at the door; per-fingerprint throttling
with the count preserved (an unthrottled flood does not just add noise — it EVICTS every other
error from a bounded store); external noise classified BEFORE the throttle so a suppressed
repeat reports its written sibling's severity; a severity bar applied at READ and shared by the
board and the API (a bar kept only in the renderer lies to every machine reader — measured on
the origin: 25 rows to 1); truthful ack/reopen (reopening a never-acked signature is a refusal,
not a 200 over an untouched row); clears bounded by AGE or ACK, never "everything", with
`only_acked` a restriction rather than a widening; and per-channel `coverage`, because an empty
card must say whether it is everything before a reader is entitled to good news. Auth refusals
land in the same stream: a worker whose credential was revoked otherwise retries forever while
the board shows a seat going quietly stale.

**The write seam refuses secrets.** The highest-volume risky behaviour on an agent board is
pasting failing command output into a question, and the ledger is append-only under a hash
chain — a secret that lands can never be removed without destroying the tamper-evidence. One
choke point now refuses secret-shaped payloads (private keys, platform token formats, JWTs,
credential assignments) with instructions, and passes recognizable redaction placeholders —
refusing what a member types AFTER being told to redact makes the error message a dead end.
Three traps the origin paid for ride along as regressions-in-comment: `_` is a word character
(so `\b` never fired before the exact `.env` paste this exists to catch), `\s` crosses
newlines, and `json.dumps` escapes them.

**Live through the push plane.** Presence and the error sidecars change with NO ledger event,
so mutations to them publish wake signals on the existing realtime bus (presence throttled —
it rides every write and the wake-up plane must not carry one signal per request; a throttled
error repeat that changed nothing on disk publishes nothing), the snapshot memo carries both
sidecar stamps, and the cumulative patches already carry the new live blocks — the fleet strip
and the error card move the moment a seat phones in or a failure lands, not when unrelated
traffic happens to append. The cockpit gained the Signals section (Asks and Errors cards), the
Directives tab, per-card machine chips, live-console rows, checkpoint notes ("working on X,
last did Y" — the fact a peer needs to decide whether to coordinate), and attention-rail rows
for open questions and unclaimed errors. Board search (`search.json`) is the pull half of
push-pointers-pull-content, and `whoami.json` answers what a request's credential actually
resolves to — a stale seat looks identical whether the caller never wrote, a proxy dropped a
header, or the credential went invalid, and diagnosing that from outside once cost hours.

**Engine and schema.** A byte-identical REPLAYED ledger line (a backup/restore re-appending the
tail) now collapses during reconcile instead of being re-chained as a second event that applies
the same payload twice — announced by name, chain unchanged, and the heal keys expected seqs on
the healed chain so a collapse cannot re-chain everything after it off by one; a genuine fork
still linearizes with nothing dropped, and content tampering is still `verify_chain`'s catch.
Plan steps carry `note`/`note_at` (the origin's checkpoint notes 422'd at this exact schema
gate). Timestamp parsing at the new read choke points is RANGE-checked — external systems emit
year-one sentinels as routine data, and a sentinel parses perfectly and then renders as a
two-thousand-year age. Slugs that survive as pure punctuation fall back to a stable digest
instead of colliding every such entity into one id per type. And the MCP lease meta key lost
its person-specific namespace — the scrub gate caught it the first time this boundary was
exercised end to end.

Deliberately NOT ported, as instance-specific rather than template material: the self-service
app provisioning lane, the credential locker, the CI/deploy doctor, machine-enrollment and kit
distribution channels, the capability catalog file, and the app portfolio with its liveness
prober — each depends on one organization's hosts, pipeline, or distribution machinery. Also
deliberately superseded rather than ported: the origin's per-agent minted-token map (this
base's scoped credential registry is the stronger successor) and its SSE side-channel ticks
(this base's push bus generalizes them).


### One live write entrance

- Added the dependency-free `python -m hub_core.client` create/claim/heartbeat/complete client so
  agents can use the authenticated HTTP mutation seam without custom scripts or framework imports.
- The client sends an explicit browser-compatible User-Agent (overridable with
  `HUB_CLIENT_USER_AGENT`) so legitimate task traffic reaches Hubs behind edges that reject
  Python's default urllib signature.
- Crystallized the active-ledger invariant across agent, operating, mounting, quality, and recovery
  contracts: direct EventStore/JSONL/SQLite mutation is offline recovery only because it bypasses
  the push publisher and can leave a Connected cockpit behind canonical state.
- Production snapshots now expose topology-free ledger storage truth. The Django audit opens a high
  finding when `HUB_DIR` is implicit or unwritable, preventing an ephemeral application image from
  masquerading as a durable live task plane.

### Proof without test accumulation

- Made the real operation the default proof and removed permanent selftests, verifier scripts, and
  automatic test workflows. Copy, style, animation polish, and ordinary non-critical work now ship
  without a validation ritual.
- Made `verification_command` optional for every task and in both completion strictness modes.
  Only rare critical boundaries may declare a one-shot out-of-band probe; its receipt persists and
  its temporary artifact is deleted before commit.
- Removed repository-audit fan-out from task completion. Child receipts compose upward, releases
  prove only a newly created critical integration seam, and observed failures become fresh repair
  tasks instead of speculative work imposed on every delivery agent.
- Delivery now treats the required `verified_by` result plus `evidence_uri` as an ordinary done
  task's accepted-operation proof; only a task that explicitly declares a critical command needs
  an additional matching exit-0 transient receipt. The cockpit no longer calls correctly untested
  work "unverified."
- Adaptive WIP now recovers one requested seat after any canonical completion carrying its required
  real-operation result and evidence, rather than only after rare `verification_run` events, while
  `receipt.failed` remains a congestion signal even when no test exit code exists. Throughput no
  longer depends on manufacturing tests.
- Adopter upgrades now hash-anchor a one-time legacy receipt cutoff. Audits separately account for
  immutable pre-contract done tasks missing only `verified_by`/`evidence_uri`, never invent evidence,
  and continue to block every other or post-cutoff schema defect.
- Added a fail-closed legacy adopter-schema lane at that original cutoff: exact portable validation
  signatures are grouped by entity type, every subject must remain untouched since capture, receipt
  debt stays separately accounted, and no ledger row, evidence claim, or canonical schema is altered.
- Documented the private-board routing boundary: middleware may registry-pre-authenticate a scoped
  token only to preserve opaque API routing; `@writer` reauthenticates and remains final authority,
  while invalid tokens reveal no route and no credential grants Hub read access.
- Fixed `hub_core.client create --plan-item` to emit canonical `{step, done:false}` plan rows instead
  of a non-schema `status` field.
- The adopter upgrader can now anchor compatibility against a read-only production-ledger snapshot.
  It uses only the exact shared immutable prefix and requires an explicit flag to repair a
  local-only cutoff; repair is backward-only and never mutates either ledger.
- Crystallized the immutable release handshake: an exact canary may classify only the sole
  self-referential pre-record `coherence:repo` finding as closure-pending, then the task-bearing
  release must immediately produce a zero-critical/high post-record audit. Every other red blocks.
- Legacy signature capture now folds the exact shared-cutoff snapshot, so a later touch on a
  divergent repository branch cannot hide debt that the production branch still carries unchanged;
  runtime post-cutoff touches remain strict.

### Hub Excellence Contract and live throughput cockpit

- Task scanning now leads with outcome title plus live age/SLE risk; the dependency visualization
  is truthfully labeled as a frontier histogram and the separately named longest chain comes from
  the actual DAG. A visible persistent density control changes real cockpit spacing, with deliberate
  1100px, 760px, and 320px recomposition and centralized live reduced-motion response.
- Made project art direction a portable scaffold input: a validated mark, paired accent field,
  display voice, surface character, and optional ambient motif now flow through the shared shell.
  The initializer creates a distinct, overridable starter for every project while semantic status,
  accessibility, responsive structure, and state-derived motion remain one upgradeable system.
- Added one canonical `public_origin()` / `host_name()` projection for portable identity. Legacy
  bare hosts remain compatible while network and allowlist consumers can no longer double-prefix a
  correct URL; adopter-owned top-level operations fields remain available through a bounded helper.
- Made `PROJECT/HUB-QUALITY.md` canonical for extraordinary visual design, purposeful motion,
  realtime truth, accessible interaction, performance, flow metrics, and durable agent coordination.
- Added `campaigns/elevate-hub.md` and propagated the contract through orientation,
  governance, mounting, construction, and bootstrap paths.
- Reconciled cockpit theme, responsive, keyboard, print, palette, and live-update contracts; strengthened
  event-time history, heartbeat presence, WIP enforcement, atomic pickup, and proof truth.
- Added compatibility-first atomic pull routing: tasks can declare capability, risk, resource,
  locality, and outcome requirements; workers declare current placement facts; incompatible work is
  filtered before quality/latency/cost scoring, and the dependency-derived ready frontier remains
  the authoritative queue.

### Interop truth, portable identity, and bounded realtime correctness

- Production delivery is now artifact-native: normalized `HUB_BUILD_SHA`, `SOURCE_VERSION`, or the
  pre-build stamp supplies running identity when `.git` is absent. Immutable deploy records require
  exact `sha == served_sha` plus explicit done `tasks_closed[]`, so named work can be proven live
  directly; Git ancestry remains optional legacy/source enrichment.
- Build coherence now prefers a valid immutable post-canary deploy closure matching the running
  artifact over stale mutable `PROJECT/state.json`, while retaining an already-matching state
  shortcut. Pre-build bytes can no longer mask the release fact written after they ship.
- Production mutations and agent discovery inherit the baked revision through `_git_head()`'s
  artifact fallback, and the audit now emits a high finding when Django `HUB_PROJECT_KEY` disagrees
  with the portable identity key.
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
