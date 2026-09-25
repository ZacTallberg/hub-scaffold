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

### Round 2 — app-kit: whose rows, honest tallies, slug-named migrations

- **Assistant kit: every read is the actor's.** `Entity.scope` narrows every generic family's
  reads (a scope that raises reads as no rows); `scope=SHARED` declares an app-wide noun; an
  entity with an owner column and no scope gets no generic tool (named in `build_all.skipped`,
  reported UNSCOPED by `adapter.check()`), and a scoped total says "you can see".
- **Assistant kit: tallies a person can quote.** `count`, `breakdown`, `list`, `search` and
  `recent` leave soft-deleted rows out and report `excluded_deleted`; a closed `filter`
  vocabulary is derived from the model (a choice takes a list); `breakdown` takes count's filter;
  a relation groups and displays by the related record's name; `detail` counts matches and
  refuses an ambiguous label with each candidate's id, and accepts an id.
- **Assistant routing:** `scope_for` counts the caller's whole core against `LANE_TOOL_CAP`.
- **Canon:** guarantees for logical CSV records in pastes, complete-day flow totals, zoneless and
  DST-ambiguous times, filter push-down past a row cap, related rows through their own scope.
- **Error-visibility kit:** a fetch is a failure at status >= 400 (a 304 poll is the request
  working), and the report endpoint drops `http` reports below 400 from older browser copies.
- **`kits.py add` names shared-database migrations from the app's slug** for kits that declare
  `retarget_migrations` (the gate), rewriting their dependencies; the choice is recorded and a
  different `--slug` on an already-vendored copy is refused.

### Round 2: completeness gaps

- **Component settings are editable from the banner.** The shared banner reads each app's
  component properties on every page and hands them on (`window.HubComponentProps` plus the
  `hub:component-props` event the agent, search bar and header takeover already follow). When
  the hub says the reader `can_edit`, it offers a **Component settings** box with one tab per
  component, drawn from the served schema. Each change is previewed on the page, and nothing
  is stored until **Save for everyone**. Refused values are named. `POST /hub/api/component-props`
  now has a second caller: the same-origin, CSRF-checked browser of a person the adopter's
  `HUB_COMPONENT_EDITOR(request, slug)` admits for that app, with the save attributed to them.
  Unset, failing or raising means nobody in a browser can save. Who changed what is shown to
  editors only.
- **The responder canary tags itself `synthetic`.** A synthetic ask stays on the record
  (`questions.json`) but reaches only a reader that sends `inbox.json?include=synthetic`, which
  is the responder. A person's inbox, the `inbox/wait` notifier and its fingerprint, the
  attention detector and the board's ask rail all leave it out. A broken loop is still reported
  through the canary's own verdict.
- **The idle-timer jiggler is deliberately not shipped.** `adapters/windows/README.md` explains
  why: only the wake lock (`keep_awake.py`) is included, because a jiggler exists to get around
  a screen-lock policy.

### Round 2 — unattended lane (canon): attention work, hand-off publisher, honest counts

- **Needs-attention is work.** `python -m hub_core.unattended` offers the conditions the hub marks
  `actor: agent` (never one without the field), runs them one at a time in their own lane on the
  task clock, and counts one cleared only when `/hub/attention.json` drops it (unbuilt list =
  `unverified`, refunded); attention may use a third of an hourly launch ceiling.
- **Hand-off publisher** (`hub_core/unattended/publisher.py`, `publish` verb, run first on every
  `scan --launch`): a machine that PROVED it can push (ssh keys only, a dry run, never a stored
  password) replays a hand-off bundle onto the branch, rebased and never forced, reads it back and
  reports the pushed sha; conflicts are reported with their paths. The charter tells a run whose
  push is refused for auth to hand off instead of leaving a patch.
- **Counts that mean what they say.** A run that recorded no new checkpoint or push is handed back
  `idle` (`/hub/api/hand-back` `idle`, client `--idle`, MCP `idle`) and does not count toward the
  run cap; the cap and the per-machine attempt cap write `needs_person` with the failing evidence;
  a refusal is a deferral the scan honours; a stand-down leaves no ledger row; a task another
  holder finished or took is stopped and recorded `superseded`, never cleared or charged.
- **One machine, one run, one login.** Questions and conditions take an item claim before a run
  is queued and release it when it ends (both launchers); a per-item lock stops two launchers on
  one machine; an expired agent login disarms the lane until a person logs in; a dated weekly
  usage reset is read; an abandoned task holder is offered as a resume.
- **No credential in a URL.** Every remote the worktree module reads or builds, and every reason
  string, is stripped of userinfo (ssh keeps its user); an old launcher clone is healed in place.
  Ledger writes retry against a clock instead of vanishing. Doctrine: applications are shared
  (a core change waits on the operator's yes, never an app's builder); cite the record you act on;
  enrolment is done when the distribution row says so. `register-responder.ps1 -Launcher
  unattended` schedules the lane launcher.

### Integration: fifteen lanes on one engine, one verb per intent

- **One presence row, every field.** Console name/repo/app/runtime, the supervisor's digest and
  unattended kind, kit telemetry (`client` version+sha per machine, `client_digest` for the
  stale-seat detector, `artifacts`), project-qualified recent files (a bare name is qualified with
  the console's project), a redacted focus kept whole up to a preview, and the repo moving with
  the cwd — merged never-clobber in one `_session_merge`, projected in one `session_row`.
- **One crossover engine** (`hub_core.overlap`): file > problem > task > project > topic, with
  the peer's checkpoint and a suggested split; `items_for_agent` for an agent's consoles and
  `items` for one console, both reading the same comparison.
- **One inbox fold.** Messages, questions (stuck, gated, review, hop), directives (pinned ones
  only where they are pinned; an answer's console routed with fall-through), assignments,
  decisions, task rot, attention, owned problems, operator-only seat items and crossovers — one
  fingerprint, one veil filter, one change signal (ledger head, presence, leases, error stamp).
- **One transport.** Several routes, breadth-first failover, the offline queue and blind window,
  busy-503 retries in place, idempotent retries of retry-safe writes, safe headers, a hub 5xx
  envelope never retried, a gateway 503 page resent.
- **One verb per intent where two lanes named the same thing:** `/api/hand` gives with `to` and
  hands back to the unattended queue without it (`/api/assign`, `/api/hand-to-queue`), while
  `/api/hand-back` is the run-ended transition an unattended launcher writes; `/api/ci-event`
  takes the webhook secret or an agent credential; `consoles` carries the split and
  `--mine`; `recall` answers a task id with its trail and anything else from knowledge;
  `prompt-context` gives the three-channel payload or, with `--hook`, the knowledge block;
  `errors` reads the folded queue, `--include` the raw stream; `perf` shows route latency and
  the answering process; hosted UI components are `hosted-components` (`components` stays the
  standard-component catalog); a delivered human gate is `review-gate`.
- **Every read route carries the read gate and declares its visibility**; the hosted
  components are declared public discovery. `hub.json` and `next.json` are memoized per input
  state, and `next.json`'s memo key includes who is asking.
- app-kit records the error-visibility and csrf kits with their seams declared.

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

### Store and performance: a ledger that cannot fork itself, writes that report once, reads that scale

- **The ledger stops manufacturing forks.** `append` indexes its row BEFORE the durable line is
  written, so a failed insert leaves the log untouched instead of a line no row backs (the
  duplicate-seq fork). Opens prove currency by four facts (size, tail hash, index tip, row
  count) without restamping the ledger's mtime; the allocator reads only the tail; a rebuild
  prints `LEDGER_INDEX_REBUILT` with its reason and root. `hub_core/atomic.py` waits out the
  transient Windows sharing window on open/rename, never retrying a partial write.
- **A retry that landed reports success, once.** `idem_key` is bound to the payload it came
  with; creates scope it to the type's id prefix and answer `data.replayed`; the store allocates
  server-numbered ids under its write lock (`append(allocate=...)`), so parallel creates land as
  distinct records. `python -m hub_core.client` retries transport failures with the same key.
  A keyless re-ask of an ANSWERED question is refused as a duplicate; only a still-open question
  is its own retry.
- **Errors fold by cause.** Fingerprints ignore uuids, ids, numbers and quoted values; the
  newest row of a signature carries the repeats folded since it was written.
- **Reads scale.** `hub.json` carries a weak ETag that ignores clock fields, accepted from
  `If-None-Match`, `X-Hub-ETag` or `?etag=`; large collections ride as heads (`partial`,
  exact `collection_counts`) that the board hydrates from `GET /hub/<type>.json`
  (`client list`, MCP `list_collection`); WSGI board streams are capped
  (`HUB_LIVE_STREAMS_MAX`) with a `busy` frame the board waits out.
- **Process roles.** `HUB_ROLE=web|background` and `manage.py hubbackground` take background
  projections off the request process; `hub.prewarm.start()` warms the served process;
  `GET /hub/perf.json` / `client perf` names the answering process.
- **Verified backups.** `manage.py hubbackup` bundles `HUB_DIR` by exclusion, verifies by
  REBUILDING the board (`hub_core.reconstruct`) at a live cursor recorded before bundling (a
  copy shorter than the source is refused, never compared only as far as it reaches), prunes
  before copying, falls back to a
  git-ignored vault, ships an off-host copy, and reports staleness; restore goes only into an
  empty directory. The deploy runbook gains the pre-deploy backup and a fast-push-path section.
- **Also:** the scrub gate refuses stray control bytes; `patterns/read-access-grants.md`
  admits a private board's viewers from the ledger's own active grants.

### Errors become owned problems; every service says whether it can be seen

- **Problems.** `GET /hub/problems.json` folds the error stream into the thing somebody fixes
  — one problem per (project, job), (app, kind, normalized message) or (agent, component,
  code) — with state (unclaimed / in flight / escalated / resolved), a CONSOLE as holder,
  count, recency and the lifted cause line. `api/problem/claim|resolve|release|escalate`:
  another live console's claim is refused naming it (`take` displaces on the record), resolve
  acks every row behind a problem with the root cause, a recurrence reopens it, and a
  diagnosed problem can be parked on an open ask or task until that closes. Fresh unclaimed
  problems are delivered to their owners' inbox (critical at once, error after 10 min, the
  operator after 30). Board: a Problems card with a detail view carrying the stored trace
  and the exact commands.
- **The stream tells the truth about itself.** Channels are live, quiet or NEVER reported
  (a durable per-channel and per-service record, not the retained window); the throttle holds
  across worker processes (locked sidecar, fails open); per-occurrence ids and a forwarder's
  "[+N more]" suffix no longer split one cause; traces keep head AND tail (32 KB) with the
  exception line lifted into `cause`; an ack covers only what it saw. The read-time bar also
  holds back self-test tokens, synthetic proof rows, branch-ref CI runs, closed blind windows
  and WSGI wake-up blips — counted and listed, never queued. `errors.json` takes `?app=` and
  `?limit=`.
- **CI in one neutral shape.** `api/ci-event` records failed jobs as problems and stamps
  passes; a job's failures are retired only by that job passing later on the same ref.
- **Every service, observed?** `app_health.json` and a board card give each service a verdict
  from evidence (observed / partial / dark / unbuilt) with the gap named, its agent-chat
  channel, and bounded liveness probes of declared `health_url`s (`HUB_APPS`). An unwritable
  seen-store becomes an hourly error instead of a silent "never". `doctor.json?app=` reads one
  service as BLOCKED or WAITING.
- **Crossovers.** `overlap.json` compares live consoles (project, files edited in the last ten
  minutes, focus, held task, claimed problems) and tells only the two concerned — file,
  problem, task, project subtree/subject, or a shared system from `HUB_OVERLAP_SYSTEMS` —
  once per side, with how to reach the other and a proposed split. Presence carries project,
  files, console name and unattended; leases carry the claiming console; focus lines are
  scrubbed of email addresses.
- **Delivery honesty.** Directives can be pinned to a machine or a live console (by session
  or name; a console that is not live is refused). One `ack` routes any id by its type and a
  fingerprint ack refuses ids that are not fingerprints. The board reports its own uncaught
  errors with a same-origin path/line/col.
- **Machines.** A scoped credential can un-enroll itself (`api/leave`, `--dry-run`),
  `enroll/status.json` answers active/revoked/expired, and `check-env --report` files each
  NEEDS A PERSON line as that machine's problem. The client remembers a blind window (hub
  unreachable) across restarts and reports it on the next success, graded by what was lost.
- **Back-pressure is not a fault.** `LedgerBusyMiddleware` answers a held ledger lock with 503
  + Retry-After and a warning row, and absorbs a view's own self-described busy 503, so
  neither becomes a red problem; real-fault 503s keep their row.
- Every capability is reachable from the client, the MCP endpoint (`list_problems`,
  `claim_problem`, `resolve_problem`, `release_problem`, `escalate_problem`, `app_health`,
  `diagnose_app`, `check_crossovers`, `ack_item`) and, where it reads, the board.

### The error queue counts itself honestly, and the overview reads as one glance

- **`errors.json` counts the QUEUE, never the listing.** `on_board`, `unclaimed`, `claimed` and
  `oldest_unclaimed_s` are taken over on-bar rows only; what the bar held back is reported under
  `deferred` and `off_board {rows, open, reasons}` whatever `?include` listed. `?app=<slug>`
  narrows the stream and every count to one service. Reachable as `python -m hub_core.client
  errors [--app] [--include deferred]` (queue summary first, below-bar remainder named) and the
  MCP `read_errors` tool.
- **The bar recognises more transport blips, and only the blip itself.** A browser cancellation
  (`AbortError` in every engine's wording), a named fetch timeout (`Hub did not answer <path>
  within N seconds`, bare or as `TimeoutError: …`), and a service reporter's once-wrapped
  no-response (`request failed: GET … - Failed to fetch`, `live stream failed: TypeError: Failed
  to fetch`) are deferred. Every fault-shaped wording is anchored at both ends, so a message that
  merely begins with the phrase (`The operation was aborted because …`) or reports a sustained
  outage (`… (2 consecutive background attempts, no response)`) still queues. The board's fetch
  timeout aborts with that named reason; the board does not itself forward fetch failures.
- **Acks are time-bounded.** An ack covers the occurrences at or before it; a recurrence is
  unacked again (`errorlog.is_acked`, shared by the row mark, the counts and `only_acked` clears,
  failing open toward visible). A change of severity writes its own row instead of folding into
  the prior severity's throttle window.
- **Ledger index rebuild is batched and named.** The heal indexes the chain in bulk statements
  under the write lock, logs `LEDGER_INDEX_REBUILT events= took= reason=torn|count|head`, and
  `busy_timeout` outlasts a rebuild.
- **Overview layout.** Asks and errors sit directly under the agent cards in one live cluster;
  the attention rail spans beside them without sizing them; agent cards keep natural height.
  Error-channel coverage chips carry their last-report age in text.
- **Patterns.** `error-visibility.md` gains: prove a reporter is SERVED, not placed; word a
  no-response failure so the bar can defer it; a per-service recent-errors consumer; a dead
  channel is only caught by a second independent record. `deploy-runbook.md` gains rolling-restart
  health waits; the example site exempts the liveness path from an opt-in `SECURE_SSL_REDIRECT`.

### Knowledge: record it by kind, find it by meaning, deliver it before each prompt

- **Record verbs.** `POST /hub/api/lesson`, `finding`, `method`, `review` (and the existing
  `gap`) with matching client verbs (`share`, `finding`, `method`, `review`, `gap`) and MCP
  tools (`share_lesson`, `record_knowledge`, `record_gap`). A lesson is ADMITTED and tagged with
  what it may duplicate or correct (`related`, `related_partial`) — never refused for
  resemblance, because a correction is near-identical text; an identical live rule returns
  `duplicate_of` and writes nothing; `supersedes` retires the target on every surface.
  `client adjudicate` settles the tags (rules first, then any OpenAI-compatible judge model),
  never guesses, and exits 2 when judging is owed and no model is configured.
- **Search that finds what it should.** `search.json` is BM25F (fielded, IDF + length
  normalized) over every live record that is knowledge rather than traffic — inter-agent
  messages and restated questions are out, answered asks are in — fused convexly with an
  optional dense channel (`HUB_EMBED_*`, `manage.py semantic_index`, CSLS hub penalty, vectors
  in a sidecar beside the ledger). The corpus is built once per ledger head; `timing_ms` per
  stage; a `<memory-partial>` notice appears exactly when something could not be seen, and an
  empty result says it is about the words used. `related.json` gives neighbours by vocabulary
  and by meaning. `manage.py retrieval_eval` measures every configuration on the board's own
  answered asks with held-out splits.
- **Per-prompt knowledge.** `guidance.json?focus=` ranks the knowledge index by what a console
  is doing (or states that it is in standing order and why); `client prompt-context --hook`
  renders it for an agent harness's prompt hook with a per-session receipt, a 9,500-character
  ceiling and a readable overflow file. `/hub/knowledge/since` is a cursor feed (put / revoke /
  reset, ETag + 304) that `client knowledge-sync` mirrors into a local file; the fold stamps
  `provenance.seq` for it.
- **A capability catalog published from another repository** (`HUB_CAPABILITY_*`), read at one
  resolved commit, refused when incomplete, cached, and merged with ledger `cap`s so one
  capability under two spellings counts once (`capabilities.json`, client `capabilities`, MCP
  `list_capabilities`).
- **Board.** A Knowledge tab: ranked search with the partiality notice (shareable as
  `?tab=knowledge&q=…`) and the agent abilities grouped by kind, collapsed by default; the
  record drawer shows tier, verification date/check, and overlap suspicions with their verdicts.
- `client recall <id|phrase>` prints one record in full with its overlap block; a title that
  merely contains a colon is searched, not fetched as an id, and a refused id prints as a
  failure, never as "nothing matches".
- Without an embedder, a console's focus still ranks the index — by its WORDS, and the block
  says so — instead of every prompt getting the same standing order. A title-only row whose record
  the session already holds is not re-sent; a row upgraded from title to full text is.
- `client adjudicate` fills whichever overlap basis a write could not run (the lexical one
  declines on a board too small to weigh terms), and a basis that still cannot run leaves the
  record untouched rather than re-versioning every waiting lesson on every pass.
- Search and `capabilities.json` share one rule for "the catalog already names this ledger
  capability"; `metadata.scored` is the denominator of what is shown.
- Components: `search-bar` (one search box any app links; suggest, learned top match, an
  app-computed smart pass, three distinct failure states) and `header-takeover` (the banner
  hides the header an app names, warns about one it did not, hides duplicate controls only
  while it draws their counterpart, keeps one tour / read-me / People and Permissions row in the
  drawer, and reads `hide_custom` from `/hub/components/props/<slug>.json`), both served from
  `/hub/components/` — `patterns/app-search-and-header-takeover.md`. The example's
  `/demo/reporting/` page adopts both, with its own suggest endpoint.
- A record written since the last `semantic_index` run is ranked by its wording at full weight
  instead of as "meaning = 0", so a just-written exact match ranks first before it is indexed
  (it had ranked below records sharing none of its words).
- One definition of a retired record (`hub_core/record_state`, now including `removed`) serves
  search, the prompt index, the mirror feed and the overlap tagger alike.
- Table tabs say when their collection last changed ("newest 4m ago"), with a `snapshot` badge
  once nothing has been written for a week. `client gap` and MCP `record_gap` take a `note`
  like the other record verbs.
- A secret-shape check no longer refuses a documented placeholder containing spaces
  (`TOKEN=<supplied by the deploy>`).
- Patterns: `knowledge-retrieval.md`, `vendored-app-kits.md`, and stale `index.lock` recovery in
  `deploy-runbook.md`.
### App services, honest write outcomes, and a cockpit that reports only what it measured

- **The hub serves the apps around it** (`patterns/app-services.md`). Hosted UI components at
  `/hub/components/` (link, never copy; version hashed from the served bytes; adopters observed),
  per-app component properties whose schema travels in each component's manifest
  (`POST /hub/api/component-props`, `component:configure`; out-of-set values refused and listed),
  one app's slice of the board (`/hub/app-feed.json`; every checklist and announcement row names
  the field that matched it), a person's cross-app preferences
  (`/hub/api/profile`, closed sets, SVG marks refused), and the hub as the one credential broker to
  an agent service (`/hub/api/agent/{ask,history,conversation}`; unconfigured, failed and empty
  answers each named). The `agent` component ships: launcher, docked resizable panel that pushes
  the page, past chats, distinct states. Client verbs `components`, `component-props`,
  `app-feed`, `profile`, `agent-ask`, `agent-history`; MCP tools `app_feed`, `list_components`,
  `get_component_props`, `set_component_props`, `ask_agent`; a board card lists hosted components;
  the example site carries a runnable adopting app at `/demo-app/`.
- **The write seam can serve a token-gated read** (`writer(methods=..., presence=False)`, scope per
  method), so server-to-server routes keep the same credential, scope and refusal recording.
- **Writes say what happened.** An unmintable id is `400 invalid_local`; sidecar writes retry a
  transient Windows sharing violation instead of 500ing; written text is stored whole and display
  lines are ellipsis previews; a refused finish is recorded on the task with its fix.
- **The cockpit reports only what it measured.** Console cwd and repo move as one fact and recent
  files age out after 15 minutes; the hub's own drive is measured (`live.host_disk`, attention item
  below `HUB_DISK_WARN_GB`); a question settled without a reply is never counted as answered;
  build coherence names what was not reported; evidence lists and prose render as what they are.

### Knowledge lifecycle, cheaper reads, a shared app banner, console histories

- **Retire a knowledge record through one verb.** `POST /hub/api/retire` (client `retire`, MCP
  `retire_record`) moves a gap, note, directive or ADR to a status its schema enumerates, appends
  the reason with a dated stamp instead of overwriting, and demands `addressed_by` for a closed
  gap. `hub_core/record_state.py` is the one definition of a dead record; dead records and
  anything a live record `supersedes` leave search at once.
- **`hub.json` and `next.json` are served once per input state.** A stats-only fingerprint of what
  they are built from answers the cached bytes (or a 304) before any snapshot work.
- **A shared app banner, linked, never copied.** `hub_core/components/banner/` is served at
  `/hub/components/banner/*` (index at `/hub/components/`, versions measured from the bytes). One
  declaration of pages drawn as a strip (hover-open dropdowns, click to pin, positioned against
  the viewport) or a collapsible sidebar (rail with flyouts, Ctrl+B, open state per person per
  app); one drawer from the avatar (identity, starred apps, the app's own rows, an Admins-only
  section, How this works, View as, Sign out); info bubbles on anything marked `data-ab-info`
  with a Hide-all switch and Undo; a guided tour and read-me derived from what is on screen; a
  picture cropper for the person's mark; People and Permissions over the documented
  `data-access-url` contract; CSRF resolved by the app's own cookie name first on a shared host;
  glyphs defended against host resets; all placement correct under the person's zoom. It is
  served by the same component host as the agent component. `patterns/app-banner.md` is the
  contract; `/demo/budget-app/` in the example wears it.
- **Preferences that follow the person, per app too.** The one profile engine and route
  (`/hub/api/profile`, client `profile`, new MCP tool `person_profile`) also keeps the reading
  face, sidebar state, help switches and starred apps, and per-app overrides of every page key
  and the agent placement (`prefs.apps.<slug>`: `{}` removes an app, `null` resets one key;
  `?app=` answers `resolved`). The GET adds "what can I reach" from the adopter's `HUB_APPS`
  directory joined with its `HUB_REACH` seam (active grants only, every spelling of the name).
  The same route answers the person's own same-origin browser through the adopter's
  `HUB_PERSON` resolver (CSRF-checked, never naming anyone else); everyone else gets 404.
- **Console chat histories (opt-in).** `history-push` uploads a workstation's prompts, replies and
  one-line tool calls, redacted twice, to a bounded sidecar the operator reads from the board's
  agent detail (`history`, MCP `console_history`); off unless `HUB_HISTORIES_ENABLED`, 404 to
  anyone without `history:read` or the adopter's viewer predicate.

### Unattended responders, record kinds, audience-fenced doctrine, and a control-byte guard

- **`hub_core.responder` — spend a session only when something happened.** A scheduled `poll`
  reads the board and launches one bounded session per workable item: a question for the
  operator, a FRESH unclaimed error, or a task created with `create --unattended`. A rotting
  in-progress task and the ready queue are counted and surfaced, never woken for. The launcher
  re-reads each item live (and stands down if it was taken), caps attempts, runs one session per
  lane (short for questions/errors, long for tasks), tells the session its kill minute and
  ship-by minute, kills and proves dead the whole tree at the clock, and reads the outcome from
  the board — never the exit code. Every terminal outcome posts a board update naming any
  uncommitted files left behind (never committed for the session). Sessions are non-interactive
  (no git credential prompt or dialog, `stdin=DEVNULL`) and, on Windows, get a hidden console of
  their own. A local run ledger records each run from the moment it waits for a lane; the run id
  rides the session environment and each run is its own presence seat. A daily canary proves the
  ask loop and never concludes from an unreachable board or a single empty read. `check-env`
  prints the OK / FIXED / NEEDS A PERSON repair table and, daily, boards what needs a person.
  `adapters/windows/register-responder.ps1` arms it under `pythonw.exe`; `-Remove` leaves.
  Asks raised under `HUB_UNATTENDED=1` are stamped `via=responder` by the client itself, so an
  automated escalation chain stops at one hop. `patterns/unattended-responders.md`.
- **Record the right kind of thing.** Client verbs `finding`, `method`, `gap`, `review` and the
  MCP `record` tool route each kind onto its own write path; a review rides the ask loop so it is
  delivered before the thing ships.
- **Audience-fenced doctrine.** `GET /hub/doctrine.json` (client `doctrine`, MCP
  `read_doctrine`) serves a standing document through `<!-- facet: X -->` fences, decided by the
  presenting credential's `facet:*` scopes; hidden blocks leave no trace and a malformed fence
  hides rather than leaks (`hub_core/facets.py`, `HUB_DOCTRINE_FILES`).
- **Control characters are refused at the write seam** (`422 control_chars`, naming field and
  offset): a lost `\a` in a Windows path would otherwise publish an invisible BEL into the
  append-only ledger. The error-report scope is exempt. Doctrine read from disk gets the same
  check (`503 control_chars`), and relayed machine output (a responder's last words) is
  neutralized first (`textguard.neutralize`) so the backstop note is never itself refused.
- **Fixes after review.** Near-miss or mixed-case fence markers now hide rather than leak; a
  review gate is tagged `review` and never taken by an unattended responder; the responder posts
  a note on every early exit too (stood-down, lane-full, unverified, exhausted) and names only the files its own session left dirty.
- **Coordination doctrine** for many consoles on one board — crossover kinds, what deliberately
  does not fire, one owner plus one integrator, reuse verification —
  `patterns/multi-agent-coordination.md`.

### The unattended responder: one bounded session per board item, then gone

- **`python -m hub_core.unattended`** (`scan [--launch]`, `respond`, `sweep`, `status`, `lanes`)
  runs Claude Code or Codex headless against one task or question, under a written charter
  (`hub_core/unattended/RESPONDER.md`). A task is taken only when its routing requires the
  `unattended` capability. The outcome is read from the board, never from the exit code; a task
  a finished run left in progress is re-offered as RESUMING.
- **`POST /hub/api/hand-back`** (client `hand-back`, MCP `hand_back_task`) returns an unfinished
  task to `todo` with the fenced lease as proof and ONE self-counting `handed_back` plan row.
  Rows marked `lifecycle` are shown but never counted as done steps anywhere progress is shown.
  `GET /hub/task/<n>.json` now carries `holder`, `readiness` and `handed_back`; `claim`/`start`
  journal granted leases to `HUB_RUN_LEASES` so a launcher hands back only what its run held.
- **Lanes and faults.** Lane locks are single-flight and kept alive by a heartbeat. There is a
  short lane and a long lane, and the long lane gets one slot per machine-sized share. A usage
  limit, a harness that never ran, or an API-ended run pauses the lane, reaches the board as an
  agent-error, and charges no attempt. A pass killed at its ceiling is explained from its own
  transcript. A pass that finished but will not exit is reaped as finished.
- **Worktrees and escalation.** Tasks run in their own worktree of a dedicated clone. The worktree
  is removed only when it provably holds nothing. Questions carry a structured `hop`. Hop 1 is
  retaken once after a cooldown, and hop 2 is a person's. Hop 1 is retaken only when the
  client is proven to stamp the next hop, by building a real `ask` payload with a sentinel
  `HUB_RESPONDER_HOP`. Searching the client's source for the variable name is not enough.
- **Doctrine.** `patterns/unattended-responder.md` and `patterns/doctrine-publish.md`. A
  `docs/TESTING.md` section says why a green signal is not evidence. `PROJECT/DOCTRINE.md` §5.5
  says to ask only for what you may not do. `skills/README.md` sets the skills contract (text only,
  size caps, binaries resolved beside the skill). `adapters/windows/keep_awake.py` holds a wake lock
  for a long unattended window.

### App errors: every failure class reaches the Hub, and the sender proves its chain

- **`app-kit/`** (new): a brand-free Django kit that pairs with the Hub. `kits/error-visibility`
  (`app_errors`) arms automatic producers for every class: request exceptions, every ERROR log
  record classified as django/data/other (also on loggers that stop propagating, and re-attached
  after a second `django.setup()` strips them), dying threads and processes, agentic-chat faults,
  and the browser's JS/promise/request/stream/socket/worker/CSP failures, with noise discipline at
  the sender. It keeps a fail-soft local recorder and a forwarder that sends one `info` arming row
  per serving process, counts deliveries, and never forwards from a test run. `error_selftest`
  fires every producer for real without leaking to the board. `kits/health` emits the readiness
  payload shape the deploy gate reads. `kits/csrf` reads the CSRF cookie by its configured name at
  send time.
- **Forwarder credential setup:** the kit README and the pattern issue the scoped
  `error:report` credential with the required `ttl_s`, say that it expires and how to rotate it,
  and name where a lapsed token shows up (`forwarding_status()` failed count with `HTTP 403`, the
  forwarding chip, a `hub.auth` refusal row).
- **Error stream:** `info` is a real severity that never passes the bar. `coverage.forwarders`
  lists each satellite that armed (and whether one degraded). Details keep a traceback's head AND
  tail (32 KB, gap stated) and lift its `ExceptionType: message` line onto the row as `cause`. A
  readiness 503 and a handled upstream 502/504 are warnings. Arming rows get a 60 s throttle
  window. `report_app_error` is an MCP tool.
- **Board:** reports its own uncaught exceptions and unhandled rejections through `client-error`,
  and reads the CSRF cookie (name rendered from settings) at send time, so an open board survives
  a sign-in rotation. The errors card shows a forwarders row.
- **Client:** retries transport failures only (3 attempts, 5 s then 10 s, `HUB_CLIENT_RETRY` on
  stderr). Writes retry only what provably never arrived. A gateway page counts as unreachable,
  never as the Hub's answer.
- **Audit:** results carry `evaluated`. `hubaudit` prints the denominator, and a run that evaluated
  nothing is an `INCONCLUSIVE` blocking finding.
- **Patterns:** `error-visibility.md` rewritten (every class forwards, automatic producers,
  designed degradation, arming row, wire contract without an `agent` field). `conformance-scan.md`
  gains honest-count rules. `deploy-contract.md` gains gate hygiene: verify inside the release
  lease, the readiness payload contract, narrow loud advisory dependencies, and prune-first
  snapshot retention.

### app-kit: a brand-free Django app kit that pairs with the hub

A new top-level `app-kit/` for the apps a hub-coordinated team ships. Kits are vendored into an
app, measured against a bar, and kept honest by two tools; nothing is always-on and there is no
standing test suite.

- **Kits.** `settings` (fail-closed posture, one fixed test posture, `TESTING` aliased so guards
  never silently disarm, a model lane with no default host, and `envcheck`, which names every
  missing or invalid production key in ONE throw instead of one per deploy); `gate` (deny by
  default, owners admitted by name, a per-app roster on per-app tables, a role ladder, 401 JSON /
  HX-Redirect / redirect by what the caller asked for, a roster command that refuses to leave the
  app ownerless, boot checks including a migration-ledger collision on shared databases);
  `shell` (declared capabilities — toasts, palette, live status, drawer, confirm, transitions,
  intent-only prefetch — inert until declared; one keyboard-scrollable region; `[hidden]` always
  wins); `health` (readiness fails on an unmigrated database); `service-runner` (strict
  manifest with declared port bands; a requested stop is not a crash; `--verify` asserts the gate
  posture); `assistant` (the 64-family canon as data, the coverage count, routing lanes that fail
  open, a model-read turn plan, the correction belt, the three write models, a real-model probe,
  and a generic core family whose rows always carry shown and total).
- **The bar** (`app-kit/BAR.md`) and **`tools/app_audit.py`**: three-state applicability (a
  half-present capability fails loudly), vendored code excluded from the app's own, declarations
  echoed, `--count` derives the bar's size, `--rules` audits the bar itself, a run that measured
  nothing exits 2, and every verdict says whether the checkout is behind its upstream.
- **`tools/kits.py`**: `record` derives per-file and per-kit provenance and refuses — listing
  every problem at once — a kit whose imports, static files or templates leave it, a multi-line
  `{# #}` comment, or a doc that types the bar's size or disagrees with the rule table; `add`
  vendors with provenance, refuses a stale checkout, a retired kit or an app's local edits
  (`--force` keeps a backup) and restores the old copy if the swap fails; `status` reports
  current / stale / edited / retired.
- **`app-kit/example/`**: a small budget app assembled from the kits, forwarding server errors to
  the hub's `app-error` stream.
- **Corrections after review.** The service runner imports the manifest's `entry` from the app
  root (the parent of `deploy/`), so the documented command runs without `PYTHONPATH`; the shell
  raises the shared busy count for its own drawer fetches and exposes `AppShell.track(promise)`,
  so intent prefetch stands aside for a request a person is waiting on with or without htmx; the
  audit reads `services.json`, so `auth-deploy-verified` applies to a kit-built app and checks
  every service declares its gate; `kits.py record` refuses a retired kit whose `replaced_by` is
  not an existing active kit.

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
