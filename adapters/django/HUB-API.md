# HUB API — the reference for an agent operating the hub

Everything an agent needs to read and drive the Hub over HTTP. Reads require an authenticated
principal by default — a host-site signed-in user, any live `X-Agent-Token`, or the shared-root
`X-Write-Token` — and answer `401 {"errors":[{"code":"read_auth_required"}]}` otherwise; writes are
additionally scope-gated. A deployment that sets `HUB_READ_AUTH = "public"` serves the complete
projected board to anyone, so that setting is safe only when entity contents are intentionally
publishable (the computed audit reports it as a high finding outside `DEBUG`). You do not need to read the source—this is
the contract.

This served API is the only mutation entrance for an active board. Using it makes durable append,
authorization, fencing, and realtime publication one operation. Never mutate an active ledger via
`EventStore`, `hub_app.store()`, `events.jsonl`, or `events.db`; direct access is reserved for an
explicitly drained offline recovery boundary. `python -m hub_core.client` is the dependency-free
agent/operator wrapper for the core create, claim, heartbeat, and complete loop.

- **Base path:** wherever the app is mounted, e.g. `{{LIVE_URL}}/hub` (locally `http://127.0.0.1:8000/hub`).
- **Write auth:** normal agents send a scoped, expiring, revocable `X-Agent-Token`. Missing,
  invalid, expired, revoked, or insufficiently scoped credentials fail closed. The legacy
  `X-Write-Token` works only while `HUB_SHARED_TOKEN_COMPAT=True` and is visibly recorded as the
  `shared-root-compat` actor. Reads need nothing. The optional browser launch-mint endpoint is the
  one narrow exception: it is same-origin CSRF-gated and cannot mutate board entities.
- **Authority:** a properly scoped writer can grant terminal board states and, for a rare critical boundary,
  can set an optional `verification_command`. The server never executes that command; the worker
  runs the temporary probe out-of-band and submits its typed receipt. Strict evidence URLs are
  fetched by the server. Treat the token as production credentials and read
  [SECURITY.md](../../SECURITY.md) before issuing it. A caller-authored `agent` never overrides the
  immutable subject in a scoped credential.
- **Ids** are `{{PROJECT_KEY}}:<type>:<local>`, e.g. `{{PROJECT_KEY}}:task:0001`. Allocated once, never renumbered.
- **Content type:** send `Content-Type: application/json`; bodies are JSON objects.

## Operate the hub as a LOOP, not a pile of endpoints

The endpoints exist to serve one loop. Follow it and you won't hit the common refusals:

```
DISCOVER  GET  /hub/next.json?n=1      → the top unblocked, unclaimed task (your entrypoint)
CLAIM     POST /hub/api/claim          → take the lease BEFORE touching anything; keep the returned token
IMPLEMENT (do the work; the real operation is the default proof; discovered work becomes a task)
RECORD    POST /hub/api/complete       → done, WITH the lease token + accept_note + evidence
INTEGRITY (the server re-runs its board audit inside complete; a critical violation refuses done)
```

- **CLAIM before COMPLETE** — completing an unclaimed task returns `409 must_claim`. Claim first, hold the token.
- **`done` never goes through `POST /hub/api/task`** — setting `status:"done"` there returns `409 use_complete`.
  Terminal completion is only `POST /hub/api/complete`, which is evidence- and audit-gated.
- **Decisions are ADRs** (`POST /hub/api/adr`), not task notes. Accepted ADRs are immutable — supersede, don't rewrite.
- **Blocked on a fact only the operator has? `POST /hub/api/ask`** — never stall in silence. The
  question is delivered (the operator's inbox long-poll returns it within a second), the answer
  comes back addressed to you (`GET /hub/inbox.json?agent=<you>`), and your ack closes the loop.
  Search first (`GET /hub/search.json?q=…`) — an ask the board already has is refused with the
  matching ids, and the guard fails OPEN so a search outage never silences a real question.
- **Optional presence headers on any write** — `X-Hub-Machine`, `X-Hub-Session`, `X-Hub-Cwd`,
  `X-Hub-Focus`, `X-Hub-Console` (the console's name), `X-Hub-Repo`, `X-Hub-App`, `X-Hub-State`,
  `X-Hub-Runtime`, `X-Hub-Files` (comma-separated recent edits), `X-Hub-Focus-Retract` (clears the
  focus it names, never a newer one); a bare id or one-word stub is not a focus and is ignored — feed the board's live-console view; `POST /hub/api/presence` is the seat
  heartbeat between tasks. An authenticated write refreshes your observed seat automatically.
  Three more are sent by the client kit: `X-Hub-Files` (comma-separated `<project>/<path>` this
  console edited recently; absent = keep the last claim, empty = clear it — the input of the
  board's FILE crossover, derived by `patterns/presence-gate.py`), `X-Hub-Client` (the client's
  `version+sha`) and `X-Hub-Artifacts` (`name=sha16,...` for every artifact the seat runs). A row
  that names a machine but carries neither kit header is a CALLER, not a computer: it is listed
  under `phantoms` and kept out of every device list, count and distribution grade.

## READ endpoints (GET, public)

| Endpoint | Returns |
|---|---|
| `GET /hub/` | Human dashboard. `?format=json` returns the same snapshot as `hub.json`. The running identity comes from the artifact's pre-build `HUB_BUILD_STAMP`; optional `?served=<sha>` adds an external comparison and a mismatch is explicit. |
| `GET /hub/hub.json` | Snapshot: `tasks, runs, adrs, feats, gaps, caps, deploys, notes, graph, dangling, build, audit`, derived counts/coverage, worker-launch capability metadata, and the `live` cockpit block (below). Production delivery is derived directly from the artifact stamp plus exact deploy closures. `collection_counts` is exact for every collection; `partial` names each collection served as a HEAD (see below). |
| `GET /hub/next.json?n=N` | DISCOVER — up to N ranked unblocked tasks without a live lease (urgency = priority + blocker count). `todo` tasks have `stale_reclaim:false`; abandoned `in_progress` tasks whose lease is absent/expired have `stale_reclaim:true`. `n` clamps 1–50; `metadata.available` counts all available rows before truncation (`metadata.unblocked` is retained as a compatibility alias). |
| `GET /hub/audit.json` | the computed audit: `{ok, exit_code, counts, violations[]}`. exit_code 0=pass, 3=warn, 2=violation. |
| `GET /hub/graph.json` | dependency edges + dangling references. |
| `GET /hub/<type>.json` | a WHOLE collection as `{data, count, cursor, metadata}`, rows in exactly the snapshot's shape — type is the singular (`task, run, adr, feat, gap, cap, deploy, note, directive, ack`) or the snapshot key (`tasks`, `notes`, …). Conditional on a per-collection tag (304 when unchanged). |
| `GET /hub/<type>/<local>.json` | one entity by local id, e.g. `GET /hub/task/0001.json` (includes computed flags). A task read with `?lineage=1` also carries its **lineage ladder** (see below). |
| `GET /hub/held.json[?repo=]` | the promotion queue: every OPEN hold, oldest first, with `age_s`, `urgency` (info/warn/critical, four times faster for a commit on one disk only), its holder and a one-line `detail`; metadata counts promoted and abandoned. |
| `GET /hub/item-claims.json` | every per-machine item claim still in force: `{item: {machine, session, agent, age_s, releases_in_s, holder_state, gone_s, frees_in_s}}`; a claim whose console is GONE past the grace is omitted. |
| `GET /hub/schema/<type>.schema.json` | the JSON schema for a type — read it to know the exact fields before you write. |
| `POST /hub/api/gap` `feat` `note` | Upsert the remaining mutable entity types. Identity is derived from their content. |
| `POST /hub/api/mcp` | **MCP** (Model Context Protocol, 2026-07-28 + Tasks extension) over the board: JSON-RPC 2.0, token-gated, stateless. Board tools cover pull/claim/heartbeat/release/fail/finish; run tools create, message, command, checkpoint, request input, hand off, resume, cancel, complete, and fail durable executions. `tasks/get`, `tasks/update`, and `tasks/cancel` operate only real AgentRun handles and return current top-level result shapes. MCP task notifications are not advertised because this view has no subscription transport. Hub SSE is the shipped immediate-push rail; MCP task methods are interoperable point control, never a UI polling cycle. Every mutation goes back through the ordinary write seam. |
| `GET /.well-known/agent-card.json` | Signed **agent discovery** mounted at the ROOT. It uses current AgentCard discovery vocabulary but truthfully advertises no A2A interface because this adapter implements no A2A task transport. `x-hub.callableProtocols` points to the real MCP endpoint; one skill per task `work_kind` is read live from the schema. Authentication metadata names `X-Write-Token`; its value never appears. |
| `GET /hub/live/events` | **Persistent push stream.** Emits `ready`, cumulative canonical `patch` payloads, and transport-only `heartbeat` keepalives. A patch has the same `{changed, removed, cursor, audit, live, metadata}` shape as `delta.json`, contains every change through its exact numeric cursor, and is applied directly—there is no steady-state follow-up fetch or polling interval. Resume with `Last-Event-ID` or `?since=<seq>`; cursor catch-up and a full live re-ground happen once on reconnect. On the thread-holding (WSGI) path at most `HUB_LIVE_STREAMS_MAX` streams (default 3; 0 = unlimited) are open per process; a stream past the cap receives one `busy` frame `{reason, limit, retry_ms}` (jittered 8–20 s) and closes — the board treats that as capacity, not a transport failure, shows "Waiting for a live slot" and reconnects after `retry_ms`. ASGI streams hold no worker thread and are not capped. |
| `GET /hub/cursor.json` | `{seq, hash, ts}` — the liveness cursor alone, no board contents. What a canary or supervisor polls to prove the board is advancing. |
| `GET /hub/delta.json?since=<seq>` | Reconnect/recovery form of the cumulative patch: `{changed[], removed[], cursor, audit, live}`. The normal connected path receives this payload inside SSE and does not call this endpoint. `since >= head` still returns refreshed live blocks for lease-only truth; a `cursor.seq` below your `since` means the head regressed—fall back to a full snapshot. |
| `GET /hub/activity.json[?agent=&session=]` | every live console in one uniform shape — agent, machine, `session`, `name`, `runtime`, `cwd`, `repo`, `app`, `project`, `state` (working/idle), `focus`, recent `files`, `age_s` — bound to the task THAT console holds (`has_task`, `task_id`, `task_title`; a claim is never inferred from a directory). Metadata counts consoles with/without a task and the projects worked with no task; with `session`, `no_task_for` is that console's nudge. |
| `GET /hub/receipts.json[?ref=\|undelivered=1&kind=\|stage=&agent=]` | the notification lifecycle (`offered`/`delivered`/`failed`/`resolved`): one item's thread, the offers nobody acknowledged, or the newest receipts. Repeats inside 15 min collapse to a count. Member visibility. |
| `GET /hub/perf.json[?profile=snapshot]` | per-route latency over the last hour as `count`, `p50_worst_ms`/`p95_worst_ms` (the WORST single process window — not an average), `max_ms`, `last_ms`, `windows`; `routes_verdict` and `slow_routes` (≥ `HUB_SLOW_ROUTE_MS`, default 8000; long-polls exempt); the snapshot's per-phase build timings (`last_ms`, `worst_ms`). `?profile=snapshot` (shared-root or `perf:profile`) profiles ONE snapshot build on the request thread and returns the 30 costliest functions. |
| `GET /hub/tiers.json`, `GET /hub/veil-audit.json` | the visibility tiers and declared facets; the leak audit renders every veiled route AS A CONTRIBUTOR and lists any hidden term that got through (a hit is also recorded as an error row). Member visibility. |
| `GET /hub/questions.json` | every question with the numbers the feed is about: per-row `open/answered/acked`, `to` (its addressee), `waiting_seconds`/`reply_seconds` and `stuck`; open rows LONGEST WAIT FIRST (an unknown age last). Metadata: the longest wait (and who), median reply time, per-asker lanes, a 14-day asked/answered strip, and `stuck`/`stuck_after_seconds`/`stuck_ids` (open past `HUB_ASK_STUCK_S`, default 2 h) plus `unstick_after_seconds`. "Answered" and "delivered" are different facts; only the asker's ack closes the loop. |
| `GET /hub/inbox.json?agent=<name>[&session=&machine=]` | what is addressed to that agent right now: messages to it, the questions it should see (addressed to it; for the operator, `HUB_OPERATOR_AGENT`, every unaddressed one; for EVERY console, any ask unanswered past `HUB_ASK_UNSTICK_S` — never back to its own asker, never a human-only gate), directives aimed at it, and the answer to its own question. Questions carry `waited_s`/`age`; a gate is `kind:"gate"`. Naming a console (`?session=` or `X-Hub-Session`) delivers only that console's mail plus mail addressed to a console of this agent that is no longer live (`rerouted_from` names it); with no session named, the agent-wide view. A delivery older than 10 min carries `written: "written N min ago"`. An answer carries `delivery_revision`. Filtered by the reader's veil before fingerprinting; an OFFER receipt is recorded when the set changes. `{items[], fingerprint, count}`. |
| `GET /hub/inbox/wait?agent=<name>&fp=<fingerprint>&wait=<s>[&session=]` | **long-poll**: returns the moment the addressed set differs from `fp` (≤25s, bounded waiter pool, `HUB_INBOX_WAITERS_MAX` per process). THE SLOT COMES FIRST: a caller turned away at the ceiling answers immediately with `degraded:true` from its last projection (≤30 s old) instead of paying for a fresh fold, and an admitted caller reuses its projection while nothing it depends on moved. The change signal is the ledger head plus the presence stamp bucketed to 15 s. The FINGERPRINT of the addressed set, not the event cursor, decides a wake. |
| `GET /hub/errors.json` | the operational error stream — the failures the ledger audit cannot see — with the BAR applied at read (critical/high in this system's own surfaces; foreign-scanner noise and recovered transport blips deferred, `?include=deferred` shows everything). Metadata counts the QUEUE, never the listing: `on_board`, `unclaimed`, `claimed` and `oldest_unclaimed_s` are taken over on-bar rows only, and what the bar held back is reported under `deferred` and `off_board` `{rows, open, reasons}` whatever `?include` listed. `?app=<slug>` narrows the stream (and every count) to one service's rows — what an app's own chrome reads as its recent errors; its `coverage` is then that service's channel only. Also carries the 24h histogram, trend, top sources, and per-channel `coverage` (with each channel's last-report age) so an empty list says whether it is everything. An ack covers the occurrences at or before it: a later recurrence of the same signature is unacked again. |
| `GET /hub/components.json[?kind=component\|skeleton]` | Standard components and app skeletons, resolved from the capability graph on THIS read (`hub_core/catalog.py`). A component is a `cap` of `kind: component` (`what`, `when`, `get`, `entry`, `delivery` copy/hosted/package, `hosted_at`, `exemplar`, `default`, `depends_on`); a skeleton is a `cap` of `kind: skeleton` that names components (`applies`, in order) or takes all of them (`applies_all`, ordered by `depends_on`). Each skeleton carries `applied[]` with every component's CURRENT get/entry/delivery, `missing[]` for anything it names that is not a registered component, and `order_problems[]` for an unknown dependency or a cycle — reported, never guessed around. `template` is accepted as an older spelling of `skeleton`; any other kind is `400 unknown_kind`. |
| `GET /hub/<type>/<local>.json` | One whole record plus its derived flags. The board's detail dialog reads this on open, so fields the snapshot row does not carry are still shown; deep links (`?tab=<view>#<type>-<local>`) to a record outside the snapshot resolve through it. |
| `GET /hub/problems.json` | the error stream FOLDED into problems — one per thing somebody fixes: (project, job) for CI, (app, kind, normalized message) for a service, (agent, component, code) for a worker. Each carries `state` (`unclaimed` / `in_flight` / `escalated` / `resolved`), `holder` + `holder_phrase` (a holder is a CONSOLE; "in flight" only while it is live), `count`, `since_last_s`, `cause`, `owners`, `escalation`, `reopened`. `?include=resolved\|all` (all adds what the bar holds back, with `defer_reason`), `?app=<slug>`, `?id=<p-id>` (one problem with its full stored trace). The counts always describe the QUEUE. |
| `GET /hub/app_health.json` | every service (declared in `HUB_APPS` or ever reporting) with a verdict from evidence only — `observed`, `partial` (the gap named as an observation), `dark` (neither forwarder nor CI ever reported: silence is not health), `unbuilt` — plus forwarder/CI/agent-chat/liveness state and open problems. Reading it starts a bounded background liveness sweep of each declared `health_url`; every row says how old its probe is. `metadata.sources_seen.writable` says whether a "never" can be believed. |
| `GET /hub/doctor.json?app=<slug>` | one service diagnosed: its health row (synthesized for an undeclared service that ever reported), OPEN problems, resolved ones apart as history, and a verdict — `blocked` (an unclaimed problem), `waiting` (held or escalated: somebody's work in flight), else its health verdict. `404` for a name nothing knows. |
| `GET /hub/overlap.json[?agent=&session=]` | crossovers between live consoles, computed once on the hub: `file` > `problem` > `task` > `project` (same subtree or subject) > `topic` (same external system from `HUB_OVERLAP_SYSTEMS`, or the same distinctive file). With `session`/`agent`: only that side's signals, phrased from it, with who, what they are on, how to reach them, and a proposed split; `unseen` marks what it has not been told. Without: the roster and every pair. |
| `GET /hub/enroll/status.json?credential=<id>` | `active` / `revoked` / `expired` / `unknown` for one scoped credential — never anything secret — so an un-enrolled machine learns it instead of retrying a dead token. |
| `GET /hub/search.json?q=…` | ranked multi-term search over the whole board (titles weighted over bodies, exact phrase boosted) — the pull half of "push pointers, pull content". |
| `GET /hub/perf.json` | the ANSWERING process: `process{role, pid, uptime_s, background_clock{status, age_s, tick report}}` and `prewarm{state, steps}`. See `docs/OPERATIONS.md` → Process roles. |
| `GET /hub/whoami.json` | what the hub actually received on THIS request: the presented credential's `mode` and `subject` (or why it is invalid), its scopes, and which `X-Hub-*` headers survived any proxy. Never echoes tokens. |
| `GET /hub/agent-updates.json?limit=N` | the agents' own first-person feed of what they did — `{at, epoch, agent, machine, kind, summary, evidence, item, by}`, newest first, bounded to the last 200 lines; metadata carries `last_24h`. The same rows ride `live.updates`. |
| `GET /hub/distribution.json` | every seat graded against what this hub publishes (the client it serves, `CHARTER-CORE.md` when present, and each `HUB_DISTRIBUTED_ARTIFACTS` file): per seat `current` / `drifted` (with the stale artifacts) / `offline` (silent past 2h — named, never graded as drift) / `phantom` / `legacy`; seats gone 72h+ collapse to a count. The `verdict` leads and states what it did NOT grade. Hashes compare LF-normalized bytes (CRLF and raw forms accepted) and split `version+sha` before grading. A seat silent 6h+ becomes an OPERATOR inbox item of kind `offline`; drift on an ONLINE seat becomes kind `drift` only once the artifact has been published 6h+. |
| `GET /hub/built.json[?person=]` | what each PERSON built, derived from the ledger, never curated: completed tasks, releases, authored gaps/feats/ADRs/decisions/notes. A machine identity folds into the agent that reports from it (raw presence rows, so an offline laptop still folds); a machine name never labels a person; service identities are never people. |
| `GET /hub/ci-events.json?pipeline=\|job=\|project=[&limit=]` | **credential with `ci:read` required** — the raw CI deliveries behind a CI row, newest first, plus a `store` block (path, bytes, writability) so an empty answer says whether nothing arrived or nothing can be kept. Two rotated files of 4 MB each. |
| `GET /hub/dag.graphml` | the open dependency DAG as GraphML, for any graph tool that reads the format. |
| `GET /hub/next.json?unattended=1` | The **unattended lane**: only tasks marked `unattended`, priority P0–P2 (P3 is a wish list), never a `work_kind: decision`, and never one a live run is already on. Work a run handed back is `todo` again and re-offered here. Every row (on either form of `next.json`) is annotated like the task feed below. |
| `GET /hub/project/<slug>/tasks.json` | One project's task feed for a consuming app: `{project, open[], finished[], counts}` — open tasks plus those finished in the last 14 days, each annotated with `holder` (agent, console, `live`/`abandoned`), `responder` (latest run: waiting/working/finished + outcome), `pushed` (typed `pushed` checkpoint first; a note sha only as `source: note`, never hex glued to an id), `handed_back`, `deployed`, and `ci_problem` (an unacked error row joined on the task's OWN recorded sha or pipeline, never on project alone). Carries an `ETag` that moves only when something a reader shows moves; `If-None-Match` answers **304**. |
| `GET /hub/task/<local>.json` | Also carries the same annotations; `python -m hub_core.client recall <id>` prints it as state, holder, commits and the checkpoint trail in order. |
| `GET /hub/attention.json` | **Needs attention**: every fixable operational condition — seats gone silent, clients older than the one this hub serves, consoles working with no task, orphaned leases, stalled or unclosed tasks, unstarted unattended requests, expiring/expired credentials, unanswered questions, unclaimed errors — each with `severity`, `who` acts, the exact `fix`, the `evidence` values that produced it, and a real `age_s` (first-seen is persisted). Cleared conditions move to `recently_cleared` with how long they stood. A source that could not be read is named in `sources` and silences only its own detectors. `ETag`/304 (ages are excluded from the tag). |
| `GET /hub/consoles.json[?session=<id>]` | Every live console split `attended` / `unattended` (live runs) / `finished` (runs that ended in the last 30 min, kept as a recap with their outcome), each with an honest state (`working` under two minutes of quiet, else `idle` with "idle 12 min, last did …"), the digest a supervisor reported, and the task IT claimed. `crossovers[]` lists every pair of consoles on the same file, task, subsystem or subject; `?session=` adds `addressed[]` — the signals for that console with the peer's focus, task, latest checkpoint (a peer report), how to reach them and a suggested split. |

`GET /hub/hub.json` (and `GET /hub/?format=json`) answer **304** when the caller already holds the
current tag, so an idle poll or a re-grounding pull costs an empty body. The tag is WEAK (`W/"…"`):
it hashes the whole board with the clock-derived fields (`generated_at`, every `age_s`/`idle_s`)
removed plus a five-minute bucket, because those fields change on every rebuild and made two reads
of an unchanged board carry different tags. The caller's last tag is accepted from any of three
carriers — `If-None-Match`, `X-Hub-ETag`, or `?etag=` — because a proxy in front of an adopting
host may drop or rewrite `If-None-Match`; the board's own client sends all three. Responses carry
the tag in both `ETag` and `X-Hub-ETag`.

**Snapshot heads.** A collection larger than `HUB_SNAPSHOT_HEAD_ROWS` (default 60; 0 disables)
is served on the wire as a head: every row that is still LIVE (open tasks, active directives, open
questions, open/investigating gaps) plus the newest of the rest by `provenance.updated_at`.
`partial[<key>] = true` names it and `collection_counts[<key>]` stays exact. The board fetches the
whole list from `GET /hub/<type>.json` after first paint (tasks first, then the open tab, then the
rest one at a time) and merges every later snapshot into it by id, so a head never shrinks a tab.
The head exists only on the wire: every server-side derivation — the attention rail, counts,
search, the MCP tools — reads the whole cached snapshot, so no derived number is computed over a
head. A client of `hub.json` that needs a whole collection reads `partial` and fetches it.

### The `live` block — what the cockpit reads

Every key is derived from the same fold the rest of the snapshot uses; none of it is a second
source of truth, and every ratio carries its denominator.

| key | what it answers |
| --- | --- |
| `cursor` | `{seq, hash, ts}` — the head this payload was folded at. |
| `activity` | recent canonical events; a done task carries the `receipt` that granted it. |
| `inflight` | open tasks under a LIVE lease — agent, age, `stalled`, and plan progress. Under the receipt gate the lease (not a status word) is the true in-flight signal. |
| `fleet` | per-agent cards: current lease, plan step, the last checkpoint note, recent action trail, completions, machine, and every live console (`sessions`). An agent with no claim but a fresh console focus reads `active` — working, just not on a board task — never `idle`. |
| `updates` | the newest 40 lines of the agents' own feed (what they fixed, answered, acked or shipped, with evidence). |
| `sessions_live` | every live console, flat: agent, machine, session id, cwd, focus, age. The surface that stops two sessions from unknowingly working the same thing. |
| `asks` / `asks_open` | open questions (who, what, since when). They also ride the attention rail. |
| `errors` / `error_log` | the operational stream (bar-annotated rows) and its shape — histogram, trend, top sources, unclaimed count, per-channel coverage. |
| `readiness` | `ready` / `needs_spec` / `snoozed`, with the top few of each. Readiness comes from actionable acceptance and dependencies, never from the presence of a test command. |
| `adherence` | **is the board still being FOLLOWED and kept current** — six dimensions (`specced, proven, evidenced, fresh, current, moving`), each `{ok, total, unmeasured, pct}`. An empty denominator reports `pct: null`, never 100. `score` averages only the MEASURED dimensions and `unmeasurable` names the rest. |
| `dag` | critical path length, widest frontier, layer widths, the critical `path` itself, and the min-makespan `eta_tasks` for the fleet actually present. `acyclic: false` means the numbers are a floor, not a schedule. |
| `progress` | done/total/pct plus MONOTONIC signals — `completed_total`, `last_1h`, `last_24h`, and a per-bucket `spark`. A ratio alone does not climb when the fleet discovers work as fast as it finishes it. |
| `delivery` | Per-task accepted-operation proof / `landed` / `deployed` / `live`. For ordinary done work, required `verified_by` plus `evidence_uri` is its proof; only a task that explicitly declares a critical `verification_command` additionally needs a matching exit-0 transient receipt. Production delivery is the exact immutable deploy closure (`sha == served_sha`, task in `tasks_closed`) matching this running artifact's normalized build SHA; Git ancestry is optional legacy/source enrichment. |
| `attention` | the ranked "needs the operator" rail. |
| `worker_health` | receipt outcomes and completions per seat, with denominators. |
| `failure_modes` | what KIND of refusal the fleet keeps hitting, plus the unclassified count. |
| `wip` | the adaptive concurrency ceiling the claim seam enforces. |
| `task_health` | Is "in progress" true? Every `in_progress` task in exactly one bucket — `moving` (a step or event inside 30 min; moving WINS, even over a fully ticked plan), `complete_unclosed` (every work checkpoint recorded, nothing since; `closeable` only when a checkpoint names a commit/URL/path AND none says work remains), `stalled`, `orphaned` — with `counts`, the operator's `hygiene` items and `unstarted` unattended requests. A decision is never stalled. Grown placeholders and scheduler rows never count. |
| `sessions` / `crossovers` | The attended / unattended / finished console split with `counts`, and the crossover pairs (`consoles.json` above). |
| `needs_attention` | The operational attention list (`attention.json` above). |
| `telemetry` / `cost` | OTLP GenAI aggregate and its dollarized fold; absent until a first instrumented run exists. |

## WRITE endpoints (POST, scoped `X-Agent-Token` preferred)

Issue credentials with root compatibility or an existing `credential:manage` credential:

```json
{"action":"issue","subject":"worker-7","scopes":["task:*","run:*","mcp:call"],"ttl_s":3600}
```

The `/hub/api/agent-credential` response returns the bearer token once. Revoke it with
`{"action":"revoke","credential_id":"..."}`; `{"action":"list"}` exposes metadata only.
Endpoint operation scopes are enforced before the request body reaches business logic. A scoped
credential's body `agent` must be absent or equal its immutable subject.

### Capability-aware atomic take

`POST /hub/api/take` (`task:claim`) accepts `agent`, optional `ttl_s`, and an optional `worker`
placement profile: `capabilities[]`, `risk_clearance`, current `availability`, `localities[]`, and
observed `outcomes`. Tasks may declare a `routing` contract with required capabilities, risk,
resource budget, required/preferred locality, hard outcome constraints, and soft outcome weights.

The endpoint derives the ordinary dependency/lease/WIP ready frontier first, filters incompatible
tasks, then uses locality and quality/latency/cost fit only inside an equal urgency/critical-path
cohort. Success returns the task, lease token, and a routing summary. `409 no_compatible_task`
returns structured exclusion reasons; `422 bad_worker_profile` identifies a malformed declaration.
Missing worker facts never satisfy explicit requirements, while `/hub/next.json` remains the
canonical ready rail (it applies only the offer rule below, never worker placement).

### Who a ready task is offered to

One rule (`hub_core.offer`) decides it for both `next.json` and `take`, from three task fields:

- `assigned_to` — the task was GIVEN to a named agent (`POST /hub/api/hand`, `task:assign`,
  body `{id, to, [machine]}`; `to:""` clears it). `agent` in the body stays WHO IS WRITING. The
  recipient is checked against the known roster (credential subjects, presence, lease holders);
  an unreadable roster lands the assignment with a `recipient_unchecked` warning rather than
  refusing. Until somebody holds a lease the board shows the recipient as owner, their inbox (and
  its long-poll) carries an `assignment` item, and nobody else is offered it. A lease beats an
  assignment, and a task somebody holds cannot be handed.
- `machine` — MACHINE AFFINITY: the task's input exists on one machine only. It is offered only to
  a caller that declares that machine (`?machine=`/`X-Hub-Machine`); a caller that names no
  machine is not offered it either.
- `hop` — ESCALATION DEPTH, stamped by the Hub on a task or question an UNATTENDED run creates
  (`X-Hub-Unattended: 1` + `X-Hub-Hop: N`, sent by the client when `HUB_UNATTENDED=1` and
  `HUB_RESPONDER_HOP=N`; a payload `hop` counts too; the deepest wins and an update can never
  lower it). An unattended caller is offered a hop-1 item only after a 30-minute cooldown and never
  a hop-2 item — that one is a person's. Attended callers see everything.

A caller's empty `take` answers `409 no_ready_task` with `withheld` counts by reason.

### Leases are held by a live console

- A lease records the claiming console and machine (`X-Hub-Session`, `X-Hub-Machine`). A renewal
  from a DIFFERENT console of the same agent is refused `409 held_by_console` while the recorded
  console is provably LIVE; when liveness cannot be established (presence unreadable, the
  holder's machine quiet) the renewal goes through but never rewrites the recorded holder.
  `hub_core.liveness` answers `live` / `gone` / `unprovable`; absence from a PARTIAL roster is
  never read as gone. In-flight rows carry `holder_session`, `holder_machine`, `holder_state`,
  `holder_gone_s`, `holder_frees_in_s`.
- **A GONE console releases what it held.** Once a holder has been provably GONE for
  `HUB_GONE_GRACE_S` (default 30 min) measured from its console's last-seen stamp, its task
  lease is void: the sweep hands the task back and expires that lease in place (by its fencing
  token), a claim from another agent is granted with `took_over_from`, and a renewal from another
  console of the same agent takes over the recorded holder. Inside the grace the claim is refused
  `409 leased` with `holder_state: "gone"` and `frees_in_s`. UNPROVABLE never releases anything; only the clock does.
- **The hub hands back abandoned work.** On its own read paths (throttled) the Hub returns an
  `in_progress` task to `todo` when its lease expired more than `HUB_LEASE_SWEEP_GRACE_S`
  (default 1 h) ago, or when no lease holds it and nothing moved for `HUB_LEASE_SWEEP_UNHELD_S`
  (default 4 h), or when the console holding its live lease is GONE past `HUB_GONE_GRACE_S`. It
  writes ONE self-counting `handed_back` lifecycle row naming who held it and how long ago it
  lapsed (or went). Decision tasks are never handed back.
- **Lifecycle rows are not work.** A plan row with `lifecycle: true` or a lifecycle `kind`
  (`handed_back`, `lease_released`, `reaped`, `launcher_timeout`, `claim_expired`, `lifecycle`) is
  shown but never counted in "N of N done"; a recurring one counts itself in `times`.
- A claims-lock wait that runs out answers `503 lock_busy` with `Retry-After` and a diagnosis
  naming the holder pid (alive or dead), the lock file's age and which lock ran out.

### One responder per item: item claims

`POST /hub/api/item-claim` (`task:claim`, explicit `@writer`) `{item, machine, [release, session]}` claims
a NON-task item — a question id or an error fingerprint — for ONE machine. A different machine
gets `409 claimed_elsewhere` naming the holder and when the claim releases; the same machine
re-claims idempotently (renewing the TTL, 30 minutes). The claim records its console (`session`,
or the `X-Hub-Session` header): when that console is provably GONE past `HUB_GONE_GRACE_S` the
claim is no longer in force and another machine is granted it with `took_over_from`; inside the
grace the `409` says `holder_state: "gone"` and `frees_in_s`. A claimed question or error is
reported IN FLIGHT on the rail and the error card, never as unclaimed; a gone holder's reads
"holder gone · frees in …".

### The lineage ladder

`GET /hub/task/<local>.json?lineage=1` adds `lineage: {hops[], complete, shipped, project}`:
`recorded` (commits the task itself recorded with `step --sha`), `verified` (a deploy record with
`audit_ok` whose sha contains one), `first_release` (the EARLIEST such deploy) and `serving_now`
(does the newest verified deploy still contain it — `superseded` if not). Every hop is `known`,
`no`, `superseded` or `unknown`, and an unknown says why. Containment is asked of the task's
project checkout and the Hub's repository (`git merge-base --is-ancestor`); definite answers for
full shas are cached in `HUB_DIR/ancestry.json`, an unavailable one never is.

### The promotion lane (`held`)

`POST /hub/api/held` (`held:write`) `{repo, sha, reason, rebuild, [branch, from_gap, attested,
unpushed_reason, local_path, title]}` records a finished commit deliberately NOT live yet. It is
refused `422 sha_not_pushed` unless the client attests a remote branch contains it or the Hub
confirms a SERVER has it — a remote-tracking ref in a configured checkout contains it, or the
`HUB_COMMIT_RESOLVER` adapter says the forge has it — or `unpushed_reason` records why it is on no
remote (then marked ON ONE DISK ONLY). Mere existence in a local checkout is never confirmation:
a commit the Hub finds locally on no remote-tracking ref is refused unless attested or explained,
and the record keeps `hub_saw: "local_only"`. The refusal's `searched` names every place asked
and what each saw. `POST /hub/api/held/promote` requires `evidence` (the pipeline, sha or URL of the
rebuild that ran); `POST /hub/api/held/abandon` requires `reason`. Open holds ride the rail with
urgency climbing by age.

### Evidence in the task's own project

In `strict` mode a bare commit sha dereferences when it is a commit of the Hub's repository OR of
the task's project (its `project` field, or a `<project>: ...` title prefix naming a configured
project). Projects map to checkouts through `HUB_PROJECT_REPOS`, or to a remote resolver through
`HUB_COMMIT_RESOLVER`. A repository that could not be asked is reported as such, never as "not a
commit".

### Unattended answers are stamped

An answer written by an unattended run (`X-Hub-Unattended`, or body `unattended:true`) carries
`unattended: true` on its directive and one line in its text, so the asker knows which kind of
answer they got.

### Durable AgentRun lifecycle

`POST /hub/api/run` (`run:write`) creates a first-class run only for a task whose current fenced
lease belongs to the caller. Send `task`, `lease_token`, and optional `title`, `goal`, `parent_run`,
`trace_id`, `ttl_ms`, and `idem_key`. The response includes the folded run; it is durable and
resolvable before a task-augmented MCP call can return its handle.

`POST /hub/api/run/update` (`run:write`) applies one operation to that run: `message`, `command`,
`checkpoint`, `input_request`, `input_response`, `handoff`, `resume`, `request_cancel`,
`ack_cancel`, `complete`, or `fail`. Send `id`, `lease_token`, `action`, operation fields, and
optionally `expected_version`/`idem_key`. Every operation rechecks the task fence at commit,
appends one canonical event, and immediately wakes the live SSE rail. A process-local run cache is
never authoritative.

Checkpoints carry resumable state plus completed step IDs. Handoff names the target and latest
checkpoint. Resume transfers ownership only to the current task-lease holder and returns a
recovery envelope with the latest checkpoint, unfinished commands, recent messages, and inherited
child receipts. Completed child runs compose into the parent's receipt chain, so replacement
workers do not replay finished work merely to reconstruct proof. Cancellation is cooperative:
request first, checkpoint at a safe boundary, then acknowledge; completion may win the race.

For MCP Tasks calls, declare `io.modelcontextprotocol/tasks` in that individual request's client
capabilities. Set `Mcp-Name` to the AgentRun `taskId` for `tasks/get`, `tasks/update`, and
`tasks/cancel`. Mutating methods also carry the task fencing token in
`params._meta["io.github.hub-scaffold/leaseToken"]`. The optional protocol `pollIntervalMs` hint is
intentionally omitted: canonical Hub coordination is committed-event push, not periodic sync.

| Endpoint | Key body fields | Success | Notable refusals |
|---|---|---|---|
| `/hub/api/task` (`task:write`) | Create: `title`; update: `id` + `expected_version` plus changed fields. Updating active/leased work also requires its fencing `token`. Optional `priority` (P0–P3), `status` (not `done`/`in_progress`), `verification_command`, `deps`, `acceptance`, `phase`, `touches`, `plan`, `implements`, `decided_by`, `surfaced_by`, `source` | `200 {data:{id,version,event}}` | `409 use_complete` / `use_claim`; wrong/stale lease; `428 precondition_required`; `409 conflict`; `422 schema` |
| `/hub/api/agent-credential` (`credential:manage`) | `action:issue` + `subject`, `scopes[]`, `ttl_s`; `action:revoke` + `credential_id`; or `action:list` | One-time bearer token on issue; sanitized metadata otherwise | `403 insufficient_scope`; `422 credential` |
| `/hub/api/claim` | Existing task `id`, non-empty string `agent`, optional `ttl_s` (default 900; 1–86400), optional `session` (the claiming console; defaults to the `X-Hub-Session` header) | Atomically acquires/renews the lease and transitions `todo` to `in_progress`; `200 {ok:true,created,token,expires,version,session,…}`. A same-agent retry renews without rotating the token (`created:false`). A renewal whose transition lost an OCC race keeps its lease and answers `200` with `transition_raced:true` and the current version — only a lease this call CREATED is ever released on a failed transition. The recorded `session` is what binds the claim to one of the agent's consoles on the board (a sessionless legacy lease is attributed only when the agent has exactly one live console). **Keep `token`.** | `404 not_found`; `409 deps_blocked`, `not_claimable`, or `{ok:false,reason:"held"}`; `422 bad_ttl` |
| `/hub/api/heartbeat` | `id`, `token`, optional `ttl_s` (default 900; 1–86400) | `200 {ok:true,expires}` | `400 need_id_token`; `409 {ok:false,reason:"no/stale lease"}`; `422 bad_ttl` |
| `/hub/api/run` (`run:write`) | `task`, `lease_token`; optional `title`, `goal`, `parent_run`, `trace_id`, `ttl_ms`, `idem_key` | Durable folded AgentRun plus `id`, version, event | `404 not_found`; `409 lease`; `422 parent_run` / schema |
| `/hub/api/run/update` (`run:write`) | `id`, `lease_token`, lifecycle `action` plus its typed fields; optional `expected_version`, `idem_key` | Updated folded run, operation result, and no-replay recovery envelope | `404 not_found`; `409 lease` / `owner` / `transition` / `conflict`; `422 schema` |
| `/hub/api/fail` (`task:fail`) | `id`, fenced `token`, stable `signature`, concrete `note`; optional `kind`, `evidence_uri[]`, `consequential` (default false) | Atomically records the attempt, returns the exact lease, applies bounded backoff/circuit state, and creates or reuses a `hub.repair`-routed task | `400 need_failure`; `409 must_claim`/`lease`/`identity`/`not_in_progress`; `422 bad_failure_evidence`/`bad_consequential` |
| `/hub/api/complete` | `id`, `token` (from claim), `accept_note`, `evidence_uri` (string or array), `verification_run` (required when the task carries a `verification_command`); optional `agent`, `verified_by` (array), `expected_version`, `idem_key` | `200 {data:{id,version,event}}` | See the completion gate below |
| `/hub/api/adr` | Create: `title`, `status`, optional `agent`; `number` and `id` auto-assign. Accepted/superseded/deprecated rows require `context_md`, `decision_md`, and `consequences_md`; superseded also requires `superseded_by[]`. Update: `id` + `expected_version`. | `200 {data}` | `422 schema`, `428 precondition_required`, `409 adr_immutable` for frozen context/decision |
| `/hub/api/capability` | `agent`, `name`, `maturity`, optional `local`, `kind` (incl. `component` / `skeleton`), the component fields above, `expected_version` to update | `200 {data}` | `400 need_name`; `422` schema; `409` version |
| `/hub/api/deploy` | Post-canary `sha`, exactly matching `served_sha`, explicit `tasks_closed[]` (every id must already be a done task), `at`; optional `build`, `method`, `audit_ok`, `agent`, `idem_key` | Creates one immutable SHA-keyed release record; a repeat of the same proof (`sha`, `served_sha`, `tasks_closed`, `build`, `method`, `audit_ok`) returns `idempotent:true` with the first record's `at` and writes nothing, so a re-run job whose `at` differs is not a failure — which is what makes `python -m hub_core.client deploy` safe to retry against a cold hub (growing per-attempt timeouts, one `DEPLOY_RECORD_RETRY` stderr line per transport failure, a refusal never retried) and the `record_deploy` MCP tool (which stamps `at` when omitted) safe to repeat | `422 bad_deploy_fields`, `bad_sha`, `release_not_observed`, `need_tasks_closed`, `duplicate_task_closure`, `invalid_task_closure`; `409 immutable_deploy` |
| `/hub/api/decision` | `agent`, `topic`, `choice`, optional `rationale`,`invalidates`,`refs` | `200 {data:{event}}` (idempotent on topic+choice) | `400 need_topic_choice` |
| `/hub/api/launch-grant/consume` | `consume`, `action:"start"`, `task`, `count` (must match the grant) | `200 {data:{authorized:true,count}}` | `403 launch_refused` (bad/expired/replayed/re-aimed grant) |

### Optional browser launch mint (CSRF, not write-token auth)

`POST /hub/api/launch-grant` accepts `{action:"start", task:"", count:1}` (`count` 1–8) only when
`HUB_WORKER_LAUNCH_ENABLED=True`. It requires the CSRF cookie/header pair emitted by the Hub page and
returns a signed, short-lived single-use grant. This endpoint is for the in-page control, not agents;
agents should never copy any agent or root credential into browser storage. See `adapters/windows/README.md`
for the workstation half of the issuer-bound consume protocol.

### The completion gate (`/hub/api/complete`) — what it checks, in order
1. Lease — the authenticated subject and credential id must match the valid fenced claim
   (`409 must_claim` if unclaimed, `409 lease` if stale, reclaimed, or owned by another credential).
2. `accept_note` + at least one `evidence_uri` — else `422 need_evidence`.
3. **strict mode only** (`HUB_DONE_STRICTNESS=strict`): every `evidence_uri` must dereference — a URL returning
   <400, a commit sha in this repo, or an existing path resolved from `BASE_DIR` (absolute paths are
   also accepted) — else `422 evidence_unresolvable`. A single evidence string may be a comma/space list of
   shas or URLs (a task spanning several commits is ordinary); it splits only when EVERY part is
   sha- or URL-shaped, each part must dereference, and a bad part is named. A bare sha this
   repository does not contain is refused naming the repository searched and asking for the
   commit's forge URL instead. Strict changes evidence resolvability; it does
   not require a `verification_command`.
4. If the task has an optional `verification_command`, you must supply a matching typed
   `verification_run` receipt —
   else `422 need_verification_run`. **The server does NOT run the command.** It used to
   (`shell=True`, from `BASE_DIR`), which made the write token equivalent to arbitrary shell on
   the hub's host; that path is removed. You run it yourself, out-of-band, and report what
   happened:

   ```json
   "verification_run": {"command": "<the task's own verification_command, verbatim>",
                        "exit_code": 0,
                        "output_sha256": "<sha256 of the captured stdout+stderr>",
                        "ran_by": "<your agent id>"}
   ```
   Refused as `422 bad_verification_run` if the command is not the task's own (a receipt cannot be
   borrowed from another task), if `exit_code` is non-zero, or if `ran_by` is not the completing
   agent. The receipt is stored on the entity, so the completion stays falsifiable afterwards. The
   probe itself is transient: create it only for a critical security, destructive-data, migration,
   protocol-compatibility, or concurrency boundary, run it once, record the receipt, and remove the
   probe artifact before commit. Do not create tests for ordinary fixes, copy, styling, spacing,
   color, animation polish, or routine implementation.
5. Completion stops after recording the result. It does not fan out into a repository audit or
   make every task pay for unrelated proof.
6. Immediately before append, the server rechecks the fencing token under the lease lock and binds
   completion to the exact entity version whose command was verified. A concurrent edit or expired/
   reclaimed lease refuses completion. Success releases the lease; an abandoned `in_progress` task
   becomes discoverable again after expiry.

Completed dependency receipts compose upward: downstream and release tasks inherit them and examine
only a newly introduced critical integration seam. They do not rerun child proof or nest verifier
fan-out. Once the actual changed behavior succeeds and no critical boundary remains, stop.

### The ask/answer loop and the directive plane

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/ask` (`ask:write`) | `agent` (the asker), `question`, optional `context`, `relates_to[]`, `anyway`, `to` (address one agent), `human_only` (a gate only a person can satisfy) | Mints a note tagged `question`+`open` carrying its `asker` in a STABLE field (provenance is rewritten by answering, so deriving the asker from it mis-addresses every re-answered reply). Id keyed on the wording — a retry updates, never twins. Refuses `409 duplicate_question` with the matching ids when the board already has it (open, answered, or crystallized); pass `anyway:true` for a genuinely different question. The guard fails OPEN: a search outage never silences a real ask. The asking console (`X-Hub-Session`) is recorded as `from_session`, so the answer returns to that console. Routing: addressed → that agent; unaddressed → the operator; unanswered past `HUB_ASK_UNSTICK_S` (default 4 h) → every console except the asker's. A `human_only` ask, or one matching `HUB_HUMAN_GATE_PATTERN` (title + body), is a GATE: operator-only, never widened; `HUB_GATE_RESOLVER` (a cache-only callable) can report the approval already landed, which turns it back into an ordinary question carrying `granted`. |
| `/hub/api/answer` (`ask:answer` or `directive:write`) | `question` (id or local), `text`, optional `crystallize:true`, `disclose:true` | ONE verb, open to anyone who may answer: mints/updates the answer **directive** targeted at the asker read from the question (never chosen by the caller) and at the asking console (`session`), idempotent per question — a changed re-answer updates in place and bumps `delivery_revision` AND retires the question (`open`→`answered`). `data.question_still_open:true` flags a retire that failed. `crystallize` additionally mints a standing knowledge note the duplicate-ask guard will match — OPT-IN, because most answers are one-offs and minting indiscriminately taxes the knowledge surface to remember something true for one afternoon. |
| `/hub/api/directive` (`directive:write`) | Create: `title`, `body_md`, optional `targets[]` (default `["all"]`), `remediation_cmd`, `deadline`, `machine`, `session`; update: `id` + `expected_version` | An operator instruction addressed to named agents. `machine` pins delivery to one computer; `session` pins it to one console and may be the console's NAME — the console must be live now (`409 console_not_live` / `console_ambiguous`), so a pinned message never lands nowhere while answering 201. `directive:write` is an authority tier above ordinary board writes — the shared-root credential holds it; issue it to a worker credential only deliberately. |
| `/hub/api/ack` (`ack:write`) | `agent`, `directive` (id or local), optional `note`, `delivery_revision` | One agent's record that delivery landed. Stable id (replay-safe). A revisioned answer needs the revision read: missing → `428 need_delivery_revision`, stale → `409 answer_changed` (a correction is never closed by a receipt for the text it replaced). Records a `delivered` receipt. The item leaves that agent's inbox; a directive whose every NAMED target has acked retires itself to `fulfilled`. |
| `/hub/api/message` (`message:write`) | `to`, `note`, optional `title`, `session` (one recipient console), `machine` | Agent-to-agent mail: a note tagged `message`+`open` with `from_agent` and the sender's console in `from_session` (so a reply targets that console). Idempotent on (sender, recipient, text). Delivered by the recipient's inbox. |
| `/hub/api/message/ack` (`message:write`) | `id`, optional `via` | Retire a delivered message. Only its recipient may (`403 not_recipient`, recorded as a `failed` receipt); a message pinned to a machine is retired only from that machine (`409 other_machine`). |
| `/hub/api/tier` (`credential:manage`) | `target`, `tier` (`operator`/`member`/`contributor`, or `""` to clear) | Set an agent's visibility tier. A tier is a fact about an identity, never a header a reader sends. |
| `/hub/api/presence` (`presence:write`) | `agent` (+ the `X-Hub-*` headers) | The seat heartbeat; the response carries the shared freshness contract. Ordinary writes stamp activity on their own. |
| `/hub/api/forget-presence` (`presence:manage`) | `machine` and/or `target` (the agent name) | Drop a phantom or retired seat row — a decommissioned laptop otherwise sits on the fleet strip looking like a teammate until the retirement horizon. Refuses to drop everything (`422 need_machine_or_target`); connected cockpits wake immediately. |

### The agents' updates feed

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/agent-update` (`update:write`) | `summary`, optional `kind` (`fixed`/`answered`/`acked`/`shipped`/`escalated`/`noop`), `evidence`, `item`, `by` | `201 {row}`. The agent is the write seam's bound identity (a scoped credential cannot post as someone else); the machine comes from `X-Hub-Machine`. Stored append-only, so a reader holding the file cannot cost a write; a write genuinely lost to contention is `503 update_write_failed` with its `reason` (retry is safe) plus a `hub.agent-updates` warning row. `hub_core.client update --note … --evidence …` posts one; under `HUB_AUTOWORKER=1` the client's `answer`, `ack` and `finish` post their own line. MCP: `post_update`. |

### The operational error stream

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/app-error` (`error:report`) | `app` (slug), `message`, optional `kind`, `severity`, `code`, `details`, `component`, `operation`, `path`, `host` | `201 {fingerprint}` — a satellite service's server exception/job death, attributed to the APP. Forward only what belongs on a queue a human drains. |
| `/hub/api/ci-failure` (`error:report`) | `project`, `job`, `trace` (the failing job's log TAIL), optional `pipeline`, `job_id`, `ref`, `sha`, `source` (push/schedule/api/...), `deployless`, `url` | `201 {fingerprint, verdict, detail, note, severity}` — the hub classifies what the LOG says (`rollback` → critical, `real` → error naming the first `[FAIL]`/`*_FAILED`, `not_deployed` → warning, `unclear` → error naming the first failure-shaped line, `unreadable` → error) and records one `ci.<project>.<job>` row. The trigger is only ever a hint; the raw log is never stored. |
| `/hub/api/agent-error` (`error:report`) | `agent`, `message`, optional `source`, `severity`, `details`, `machine` | `201 {fingerprint}` — a worker-side operational failure, so the stream covers more than the hub's own host. |
| `/hub/api/ack-error` (`error:manage`) | `fingerprint`, optional `note`; or `fingerprint` + `reopen:true` | Ack collapses the signature's occurrences UP TO NOW off the queue without deleting rows (a later recurrence surfaces again — the ack never became a permanent mute); reopening a never-acked signature is `409 not_acked`, not a 200 over an untouched row. The id must be a 16-hex fingerprint (`422 not_a_fingerprint`): an ack CREATES its record, so a mistyped directive or problem id would otherwise mint an acknowledgement for something nothing reported. |
| `/hub/api/ci-event` (`error:report`) | `project`, `status` (`failed`\|`success`), optional `ref`, `sha`, `jobs[]` (`{name, status, url}` or bare names), `url`, `actor`, `source`, `details` | The neutral shape any CI adapter posts. A failed job becomes a row under `ci.<project>.<job>`; a failure with no job list lands as one `ci.<project>.pipeline` roll-up. A PASS is stamped per (project, job, ref) and retires that job's earlier failures on the same ref — only a pass of THAT job does, never a later green deploy (a job moved to manual never fails again). Branch refs are recorded and held below the bar unless listed in `HUB_DEPLOY_REFS` (default `main,master`). |
| `/hub/api/clear-errors` (`error:manage`) | `older_than_hours` and/or `only_acked:true` | Bounded by AGE or ACK, never "everything"; `only_acked` is a restriction (an unacked row never drops, however old). Unbounded is `400 need_bound`. |
| `/hub/api/client-error` (same-origin CSRF) | `source`, `message`, optional `severity`, `code`, `location` `{path, line, col}` | Bounded browser diagnostics from the board itself (the board reports its own uncaught errors and rejections, throttled). `location.path` is kept only when it is a same-origin PATH (no scheme, no query); never a stack. Transport blips are held below the bar. |
| `/hub/api/ci-event` (**webhook secret**, not an agent credential) | a GitLab pipeline/job webhook body, or the generic shape `{"kind": "pipeline"\|"job"\|"deploy", "project", "status", "ref", "sha", "pipeline", "job", "jobs": [{"name","status","allow_failure"}], "trigger", "url", "finished_at", "allow_failure", "failure_reason", "restored_sha"}` | Authenticated by `X-Hub-Webhook-Token` (or GitLab's `X-Gitlab-Token`) against `HUB_CI_WEBHOOK_SECRET`; with no secret configured EVERY request is refused `404`. A failure becomes a row `ci.<project>.<job\|pipeline>`: deploy/verify/release/bootstrap jobs are `critical`, others `error`; allowed failures and `HUB_CI_IGNORE_JOBS` are dropped; a failed pipeline with NO jobs is a critical `ci_config_rejected`; a non-push trigger is named in the row. A later green on the same job AND ref, finishing after the failure, retires (acks) it — a pipeline success only the jobs it RAN. A recurrence of an acked signature reopens it. `kind: deploy` + `status: rolled_back` (or `restored_sha`) records a critical rollback that no green retires. Duplicate deliveries are dropped; the raw body is kept. Always `200` with what was done, even for an uninterpretable body (recorded as the hub's own warning), because an ingest that errors teaches the sender to disable the hook. |

### Problems: the queue's verbs

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/problem/claim` (`problem:claim`) | `problem` (p-id), optional `note`, `take`, `session`, `machine`, `name` (or the `X-Hub-*` headers) | Put this CONSOLE's name on it for every other console. Another live console's claim is `409 claimed_elsewhere` naming the holder and its age — the refusal is the coordination; `take:true` displaces it and records `taken_from`. A claim frees itself after 4 h, or 30 min after its console is provably gone (its machine still reports, the console does not). |
| `/hub/api/problem/resolve` (`problem:resolve`) | `problem`, `note` (the ROOT CAUSE, required), optional `evidence` | Acks EVERY row behind the problem and records the cause; drops the claim and any escalation. A fix closes a problem whoever holds it — the note then names both. A later recurrence REOPENS it, presumed held by the console that resolved it while that console is live. |
| `/hub/api/problem/release` (`problem:claim`) | `problem` | Hand it back (own claim; the operator may release any). Releasing nothing is `409 not_held`. |
| `/hub/api/problem/escalate` (`problem:escalate`) | `problem`, `blocked_on` (an ask `q-...` or task id), optional `note` | A DIAGNOSED problem leaves the unclaimed queue (state `escalated`) until the named ask is answered or the task closes — at that moment it is workable again. Free text, an unknown id, or an already-closed blocker is refused. |
| `/hub/api/overlap/seen` (`presence:write`) | `ids[]` (the addressed `ov-...` item ids) | Record that crossovers were put in front of this console, so each is announced once per side (again only when it changes, or after 6 h). |
| `/hub/api/leave` (`enroll:leave`) | optional `dry_run` | Un-enroll THIS machine: the presented scoped credential revokes ITSELF and the machine's presence rows are dropped. Self-revocation only reduces authority; the shared-root token is `409 not_a_machine_credential`. |

`python -m hub_core.client` routes by id type so a caller never has to know which store an id
lives in: `claim <p-id>` claims a problem (a task id claims the task), `ack <id>` records a
crossover as seen (`ov-`), resolves a problem (`p-`, needs `--note`), acks a directive or
answer, and falls through to an error-signature ack only on `404 unknown_directive` for a
16-hex id (a `403` is never retried elsewhere). `errors`, `resolve`, `release`, `escalate`,
`health`, `doctor`, `consoles`, `ci-event`, `leave`, `enroll-status` and `check-env` cover the
rest; the MCP endpoint exposes `list_problems`, `claim_problem`, `resolve_problem`,
`release_problem`, `escalate_problem`, `app_health`, `diagnose_app`, `check_crossovers` and
`ack_item`.

The sending half — LOGGING handlers for the host app, a bounded fail-soft forwarder for
satellite services — is `patterns/error-visibility.md`; wire it before the first feature.
CI results arrive through the CI system's own webhook (register it per project:
`adapters/gitlab/ensure_ci_hooks.py`) or a pipeline step (`python -m hub_core.client ci-report`);
the deploy-step failures worth reporting are in `patterns/deploy-hardening.md`.
Rows are redacted at write and throttled per fingerprint (the count is preserved) — an
unthrottled flood does not just add noise, it EVICTS every other error from a bounded store.
The fingerprint is computed over the message with per-occurrence detail removed (uuids, hex
ids, numbers, query strings, quoted values), so `job <uuid> failed` from a hundred runs is ONE
signature: one throttle bucket, one ack. The newest row of a signature carries
`occurrences_folded` — the repeats this process has collapsed since that row was written — and
the weight a reader should show is `max(1 + occurrences_since_last, occurrences_folded)`.
Every write on every endpoint above is additionally screened for secret shapes and refused
`422 secret_shaped_payload`: the ledger is append-only, so a secret written into it can never
be removed, only rotated. Recognizable redaction placeholders pass.

### Task lifecycle beyond claim/complete

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/task` (`task:write`) | create: `title`, `acceptance`, optional `priority`, `project`, `unattended`, `work_kind`, `idem_key` | **A retried create is not a new record**: the id is allocated per attempt, so with an `idem_key` the replay lookup spans every task and answers the retry with the first attempt's record plus `replayed: true` (the client stamps one key per intent). `unattended: true` offers it to the unattended lane. A `work_kind: decision` with `unattended: true` — on create, on update, or setting the flag on an existing decision — is `409 decision_not_unattended`. |
| plan items | `kind`, `sha`, `pipeline_id`, `pipeline_url`, `lifecycle`, `times`, `auto` | Typed checkpoints: `pushed` names the commit (`client step --sha --pipeline`), `deployed` is written by a verified deploy, and the scheduler kinds (`handed_back`, `lease_released`, `reaped`, `launcher_timeout`, `claim_expired`, `lifecycle`) are shown but never counted toward "N of N done", nor are `auto` placeholders grown to reach a numbered step. An unknown kind is refused by the schema. |
| `/hub/api/hand` (`task:release`) | `id`, `agent`, optional `token`, `note` | Back to the queue **for an unattended worker**: `todo`, `unattended: true`, lease released, one counted `handed_back` scheduler row. Refused on a decision. |
| `/hub/api/unclaim` (`task:release`) | `id`, `agent`, optional `token`, `note` | Let go: lease released, `in_progress` back to `todo`, `unattended` unchanged. With nothing held and nothing in flight it is a no-op (`noop: true`) and writes nothing. |
| — the orphaned-lease remedy | (either verb, no `token`) | Without the fencing token, a lease is released only when it belongs to the SAME agent (and credential subject) and its claiming console is no longer live. A live console's lease is `409 lease_live`; another agent's is `409 held`. |
| `/hub/api/task/decide` (`task:decide`) | `id`, `decision`, `then` (`file`, `close` or `reply`) | **A decision is a person's call.** Only a scoped credential whose subject is in `HUB_DECIDERS` (default the operator) may decide; the shared-root token and every other agent get `403 decision_needs_a_person`. `file` mints the build task (unattended, P0–P2, acceptance leads with the decision) and closes the decision naming it; `close` records it with nothing to build; `reply` leaves it open, puts the reply on its trail and addresses a directive to the filer — the decision leaves the decider's inbox until a newer checkpoint follows. Closing goes through the one done path; a second press returns the first result (`already_decided`). Open decisions reach the deciders' inbox (kind `decision`) with a board deep link. The board itself holds no write credential, so its decision section shows the exact command. |
| `/hub/api/overlap-seen` (`presence:write`) | `ids[]` | Records that crossover signals reached their console (a sidecar, never the ledger), so each is announced once per side until it changes. |
| `/hub/api/presence` body `session_state` | `phase`, `doing`, `narration`, `last_result`, `targets`, `kind`, `run`, `subject`, `subject_title`, `state`, `outcome`, `started`, `ended`, `bounded_s`, `project`, `files` | The digest a supervisor distilled from a session's own activity, bounded and typed; unknown keys are dropped (`client presence --doing … --kind responder …`). `kind` ∈ `responder, scheduled, autoworker, unattended` marks an unattended run. Any write may also carry `X-Hub-Project`, `X-Hub-Files`, `X-Hub-Session-Kind`, `X-Hub-Run`, `X-Hub-Subject`. |

The inbox (`inbox.json`, `inbox/wait`, `client inbox --text`) also delivers the lanes that address
themselves: `decision` to the deciders, `task-stall` (unclosed, stalled or unstarted work) to the
operator, `attention` items to their owner after 30 minutes (critical at once) and to the operator
after 24 hours, and `overlap` crossovers to each attended console's agent, once per side.

**A verified deploy closes the loop.** After `POST /hub/api/deploy` records a release, every open
task whose structured commit (`plan[].sha`) is an ANCESTOR of the deployed sha gets exactly one
`deployed` checkpoint (once per commit, however many later builds contain it). An `unattended` task
is then finished by the hub through the same done path — a short lease it holds (never when anyone
else holds the task), the deploy record and the commit as evidence; a person's task is only stepped.
A `todo` task qualifies only with a recorded commit; prose never counts. The response adds
`tasks: {closed, stepped, refused, unchecked}` when anything happened; a refused close is written
onto the task as `auto_close`. Ancestry is asked of the repository at `HUB_WORK_ROOT`
(`HUB_VCS_ANCESTRY=none` when the image carries none); a question it could not answer is reported
as `unchecked`, never read as "no". The closure is fail-soft: the release record is already
durable.

**Client telemetry.** Every `hub_core.client` call sends `X-Hub-Client-Version` (a digest of the
client); write responses carry `X-Hub-Client-Current`, and the attention list names each seat
running a different client. `HUB_CLIENT_SELF_UPDATE=1` lets a stale client fast-forward its own
git checkout — opt-in, `pull --ff-only` only, rate-limited to one attempt per 15 minutes with the
stamp written before the attempt, detached and fail-soft.

### Retries and idempotency

Every entity write accepts `idem_key`. The server binds it to the payload it arrived with (a
content hash is folded into the stored key), so an exact retry — a POST whose response was lost —
replays the first event and answers `200` with `data.replayed: true`, while a genuinely different
write that happens to reuse a key still lands. Creates of server-numbered entities (`task`,
`directive`, `gap`, an answer's first directive) scope the key to the type's id prefix, so a
retried create returns the ORIGINAL id instead of minting a twin under a newly allocated one. The
numeric id itself is chosen by the store UNDER its write lock (the first number the index does not
hold), so simultaneous creates land as distinct records -- never a refusal, never one silently
overwriting another. `python -m hub_core.client` mints a key for `create`, `ask`, `answer`,
`directive` and `ack` when none is given and retries a transport failure (timeout, reset,
502/503/504) with the same key and payload, so a write that landed is reported once, as landed.

See `MOUNTING.md → The evidence-resolution dial` for `tracked` (flow-first, the default) vs `strict`
(dereferenceable-evidence mode).

## The client kit (`python -m hub_core.client`)

The standard-library client is the sanctioned seat-side wrapper. Beyond the loop verbs it holds
four disciplines, each learned from a seat that lost work without knowing it:

- **Several routes to ONE hub.** `HUB_API_BASE` may list comma-separated addresses (a VPN route and
  an overlay route to the same process — routes, never replicas). They are swept BREADTH-FIRST, one
  attempt each, under a wall clock (`HUB_CLIENT_TOTAL_BUDGET_S`, 90); the route that answered last
  is tried first and one that failed at the transport layer is tried last for 30 minutes. Only a
  ROUTE failure (DNS, refused, connect timeout, TLS) moves to the next address; a read timeout or a
  5xx is the hub having been reached, and resending a write there could land it twice. Reads get
  `HUB_CLIENT_TIMEOUT_S` (10), writes `HUB_CLIENT_WRITE_TIMEOUT_S` (30). "Unreachable" names every
  route tried and how to point this shell at another route.
- **Three outcomes for a write, not two.** Landed; refused; or not sent. A write whose every route
  failed to connect is QUEUED in the seat's queue (`HUB_CLIENT_HOME`, default `~/.hub-client`),
  stamped with the console that queued it, and replayed in order ahead of the next write — AS that
  console (identity headers only), since a lease is held by a console. Exit `4` = queued (not on the
  board, may still be refused), exit `5` = outcome unknown (the hub received it and did not answer;
  read the board before resending). A replay refusal is dead-lettered, never a wedge; `flush --dead
  [ID]` replays dead letters as their queuer and archives what is refused again, `flush
  --dead-archive [ID]` archives by hand. Nothing is deleted. Concurrent drains remove only the
  entries they consumed, by id.
- **A lost version race is retried once.** `step` and `record` (create/amend a
  gap/feat/note/adr/decision/capability, e.g. `record gap --id <id> --set status=mitigated`) read
  the current version first; a `409`/`428` that names the current version is retried once — `step`
  recomputes its plan from a fresh read rather than replaying a stale delta. A second miss is a live
  race and is returned. A `409` that names no version (a lease or content refusal) raises at once.
- **Kit telemetry.** Every request carries `X-Hub-Client` and `X-Hub-Artifacts` (the client's own
  sha, the charter core's, and any `HUB_ARTIFACTS=name=path,...`), which is what `distribution`
  grades and what separates a computer from a phantom caller.

Read verbs: `distribution`, `built [--person]`, `ci-events --project|--pipeline|--job`. CI/deploy
steps report with `ci-report --kind pipeline|job|deploy --project P --status S [...]`, authenticated
by `HUB_CI_WEBHOOK_SECRET`, not an agent credential. MCP tools: `seat_distribution`,
`built_by_person`, `record_entity` (the same one-retry versioned upsert) and `ci_events`.

## Error responses

Authentication refusals include `forbidden` (missing/invalid/expired/revoked credential),
`insufficient_scope`, and `identity` (a scoped subject tried to name another agent), all 403.
Credential administration returns `credential` or `bad_credential_action` (422). Claimed-task
mutations additionally return `lease`, `lease_subject_mismatch`, or `use_claim` (409).

Most write refusals are `{errors:[{code, msg, …}]}`:

`forbidden` (403 missing/invalid token) · `bad_json`/`missing_id`/`need_id_agent`/`need_id_token`/
`need_name`/`need_topic_choice`/`missing_grant` (400) · `use_complete` (409) ·
`precondition_required` (428 OCC) · `conflict` (409 OCC version race) ·
`must_claim`/`lease`/`deps_blocked`/`not_claimable` (409) · `bad_ttl` (422) ·
`need_evidence`/`evidence_unresolvable`/
`need_verification_run`/`bad_verification_run`/
`verification_command_is_a_suite` (422) ·
`bad_sha`/`release_not_observed`/`need_tasks_closed`/`invalid_task_closure` (422) ·
`immutable_deploy`/`adr_immutable` (409) · `bad_grant_request` (422) · `launch_disabled`/`not_found` (404) ·
`launch_refused` (403) · `launch_unavailable` (503) ·
`need_agent`/`secret_shaped_payload`/`bad_older_than_hours` (400/422) ·
`need_question`/`need_question_and_text`/`need_app`/`need_bound`/`need_fingerprint` (400) ·
`duplicate_question`/`unknown_asker`/`not_acked` (409) ·
`no_such_question`/`unknown_directive` (404).

**`503 busy` is back-pressure, not a fault.** When the ledger lock stays held past
`HUB_LEDGER_WAIT_S` (default 30 s) any hub path — read or write — answers
`503 {errors:[{code:"busy", retry_after}]}` with a `Retry-After` header. The failure happens
before anything is written, so the same call is safe to repeat; `hub_core.client` retries it on its
own. A `LedgerBusyMiddleware` WARNING row (`source: hub.ledger`) trends the pressure instead of
putting a defect on the error queue.

An actively held claim and a stale heartbeat are the exceptions: they return `{ok:false, reason:…}`
with status 409. A wrong method returns Django's 405 response, and missing read entities use Django's ordinary
404 response. Treat a refusal as guidance—fix its cause rather than retrying blindly.

## Worked example (the full loop, curl)

```bash
BASE={{LIVE_URL}}/hub ; TOK=$HUB_WRITE_TOKEN ; H="-H Content-Type:application/json -H X-Write-Token:$TOK"
# DISCOVER
curl -s "$BASE/next.json?n=1"
# CREATE (if you need a new task) — capture the id from the response
curl -s $H -d '{"title":"Wire the export endpoint","acceptance":"the export works","agent":"me","priority":"P1"}' "$BASE/api/task"
# CLAIM — capture token
curl -s $H -d '{"id":"{{PROJECT_KEY}}:task:0001","agent":"me"}' "$BASE/api/claim"
# COMPLETE — with the lease token + evidence. The successful real operation is the default proof.
curl -s $H -d '{"id":"{{PROJECT_KEY}}:task:0001","token":"<lease-token>","agent":"me",
                "accept_note":"the changed operation succeeded","evidence_uri":["<commit-sha-or-url-or-path>"]}' "$BASE/api/complete"
```

For the rare critical task that explicitly carries a `verification_command`, run that temporary
probe yourself and add its verbatim command, exit-0 result, output hash, and agent id as
`verification_run`. Remove the probe artifact before committing; keep only the receipt. Never add
one merely to validate page copy or other ordinary visual/content edits.

Reads are a plain `curl "$BASE/hub.json"`. That's the whole API — reach for the loop, not the endpoints.

## Visibility — the contributor veil

Every `/hub` read route declares `open`, `veiled` or `member` (`urls.VISIBILITY`; the audit's
`routes:undeclared-visibility` flags a route that does not). With no `PROJECT/facets.json` the
veil is open for every reader and nothing changes. When facets are declared
(`{"facets":[{"id","label","tiers":["operator","member"],"terms":[...],"tags":[...]}]}`), a reader
whose tier does not include a facet never learns it exists: `member` routes answer 404 exactly as
an unknown route does, `veiled` JSON is scrubbed (records and strings mentioning a hidden term or
tag are OMITTED — no placeholder, no count), an entity naming one answers 404, and the board's
inlined snapshot is scrubbed the same way. The reader's tier comes from its credential
(`tiers.json`, set with `/hub/api/tier`; an unlisted agent is a contributor while facets exist),
`operator` for the shared-root token, `HUB_VEIL_USER_TIER` for a signed-in site user and
`HUB_VEIL_ANONYMOUS_TIER` (default `contributor`: fail closed) otherwise. A contributor's ask is
stamped `tier`; an answer to it naming a hidden facet is refused (`422 answer_would_disclose`)
unless the answerer holds `veil:disclose` and passes `disclose:true`, which is recorded.
