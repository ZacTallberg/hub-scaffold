# HUB API — the reference for an agent operating the hub

Everything an agent needs to read and drive the Hub over HTTP. Reads are unauthenticated; writes are
token-gated. An unauthenticated read returns the complete projected board, so it is safe to expose
only when entity contents are intentionally publishable. You do not need to read the source—this is
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
  `X-Hub-Focus` — feed the board's live-console view; `POST /hub/api/presence` is the seat
  heartbeat between tasks. An authenticated write refreshes your observed seat automatically.

## READ endpoints (GET, public)

| Endpoint | Returns |
|---|---|
| `GET /hub/` | Human dashboard. `?format=json` returns the same snapshot as `hub.json`. The running identity comes from the artifact's pre-build `HUB_BUILD_STAMP`; optional `?served=<sha>` adds an external comparison and a mismatch is explicit. |
| `GET /hub/hub.json` | Full snapshot: `tasks, runs, adrs, feats, gaps, caps, deploys, notes, graph, dangling, build, audit`, derived counts/coverage, worker-launch capability metadata, and the `live` cockpit block (below). Production delivery is derived directly from the artifact stamp plus exact deploy closures. |
| `GET /hub/next.json?n=N` | DISCOVER — up to N ranked unblocked tasks without a live lease (urgency = priority + blocker count). `todo` tasks have `stale_reclaim:false`; abandoned `in_progress` tasks whose lease is absent/expired have `stale_reclaim:true`. `n` clamps 1–50; `metadata.available` counts all available rows before truncation (`metadata.unblocked` is retained as a compatibility alias). |
| `GET /hub/audit.json` | the computed audit: `{ok, exit_code, counts, violations[]}`. exit_code 0=pass, 3=warn, 2=violation. |
| `GET /hub/graph.json` | dependency edges + dangling references. |
| `GET /hub/<type>.json` | a whole collection — type ∈ `task, run, adr, feat, gap, cap, deploy, note, directive, ack`. |
| `GET /hub/<type>/<local>.json` | one entity by local id, e.g. `GET /hub/task/0001.json` (includes computed flags). A task also carries `holder` (`{agent, expires_in_s, last_heartbeat}` of the live lease, or `null`; the fencing token never leaves the claims directory), `readiness` (the same classification as `next.json`, so `stale_reclaim:true` tells a launcher "a run that ended left this in progress" without attempting a claim) and `handed_back` (runs that ended with it unfinished). |
| `GET /hub/schema/<type>.schema.json` | the JSON schema for a type — read it to know the exact fields before you write. |
| `POST /hub/api/gap` `feat` `note` | Upsert the remaining mutable entity types. Identity is derived from their content. |
| `POST /hub/api/mcp` | **MCP** (Model Context Protocol, 2026-07-28 + Tasks extension) over the board: JSON-RPC 2.0, token-gated, stateless. Board tools cover pull/claim/heartbeat/release/fail/finish; run tools create, message, command, checkpoint, request input, hand off, resume, cancel, complete, and fail durable executions. `tasks/get`, `tasks/update`, and `tasks/cancel` operate only real AgentRun handles and return current top-level result shapes. MCP task notifications are not advertised because this view has no subscription transport. Hub SSE is the shipped immediate-push rail; MCP task methods are interoperable point control, never a UI polling cycle. Every mutation goes back through the ordinary write seam. |
| `GET /.well-known/agent-card.json` | Signed **agent discovery** mounted at the ROOT. It uses current AgentCard discovery vocabulary but truthfully advertises no A2A interface because this adapter implements no A2A task transport. `x-hub.callableProtocols` points to the real MCP endpoint; one skill per task `work_kind` is read live from the schema. Authentication metadata names `X-Write-Token`; its value never appears. |
| `GET /hub/live/events` | **Persistent push stream.** Emits `ready`, cumulative canonical `patch` payloads, and transport-only `heartbeat` keepalives. A patch has the same `{changed, removed, cursor, audit, live, metadata}` shape as `delta.json`, contains every change through its exact numeric cursor, and is applied directly—there is no steady-state follow-up fetch or polling interval. Resume with `Last-Event-ID` or `?since=<seq>`; cursor catch-up and a full live re-ground happen once on reconnect. |
| `GET /hub/cursor.json` | `{seq, hash, ts}` — the liveness cursor alone, no board contents. What a canary or supervisor polls to prove the board is advancing. |
| `GET /hub/delta.json?since=<seq>` | Reconnect/recovery form of the cumulative patch: `{changed[], removed[], cursor, audit, live}`. The normal connected path receives this payload inside SSE and does not call this endpoint. `since >= head` still returns refreshed live blocks for lease-only truth; a `cursor.seq` below your `since` means the head regressed—fall back to a full snapshot. |
| `GET /hub/questions.json` | every question with the numbers the feed is about: per-row `open/answered/acked` plus `waiting_seconds`/`reply_seconds`, and metadata with the longest wait (and who), median reply time, per-asker lanes, and a 14-day asked/answered strip. "Answered" and "delivered" are different facts; only the asker's ack closes the loop. |
| `GET /hub/inbox.json?agent=<name>` | what is addressed to that agent right now — directives aimed at it, the answer to its own question, and (for the operator, `HUB_OPERATOR_AGENT`) every open question. `{items[], fingerprint, count}`. |
| `GET /hub/inbox/wait?agent=<name>&fp=<fingerprint>&wait=<s>` | **long-poll**: returns the moment the addressed set differs from `fp` (≤25s, bounded waiter pool — past the ceiling it answers immediately with `degraded:true`, an honest poll). A supervisor loop spends its sleep blocked here; that is the mechanism behind "an ask reaches the operator in about a second". The FINGERPRINT of the addressed set, not the event cursor, decides a wake — unrelated board traffic never trains anyone to ignore the channel. |
| `GET /hub/errors.json` | the operational error stream — the failures the ledger audit cannot see — with the BAR applied at read (critical/high in this system's own surfaces; foreign-scanner noise and recovered transport blips deferred, `?include=deferred` shows everything). Metadata carries the 24h histogram, trend, top sources, unclaimed count, and per-channel `coverage` so an empty list says whether it is everything. |
| `GET /hub/search.json?q=…` | ranked multi-term search over the whole board (titles weighted over bodies, exact phrase boosted) — the pull half of "push pointers, pull content". |
| `GET /hub/whoami.json` | what the hub actually received on THIS request: the presented credential's `mode` and `subject` (or why it is invalid), its scopes, and which `X-Hub-*` headers survived any proxy. Never echoes tokens. |
| `GET /hub/dag.graphml` | the open dependency DAG as GraphML, for any graph tool that reads the format. |

`GET /hub/hub.json` also honours `If-None-Match` and returns **304** when the head cursor hash is
unchanged, so an idle poll or a re-grounding pull costs an empty body.

### The `live` block — what the cockpit reads

Every key is derived from the same fold the rest of the snapshot uses; none of it is a second
source of truth, and every ratio carries its denominator.

| key | what it answers |
| --- | --- |
| `cursor` | `{seq, hash, ts}` — the head this payload was folded at. |
| `activity` | recent canonical events; a done task carries the `receipt` that granted it. |
| `inflight` | open tasks under a LIVE lease — agent, age, `stalled`, and plan progress. Under the receipt gate the lease (not a status word) is the true in-flight signal. |
| `fleet` | per-agent cards: current lease, plan step, the last checkpoint note, recent action trail, completions, machine, and every live console (`sessions`). An agent with no claim but a fresh console focus reads `active` — working, just not on a board task — never `idle`. |
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
unfiltered canonical ready rail.

### Hand-back: a run ended with its task unfinished

`POST /hub/api/hand-back` (`task:release`) with `id`, `token` (the fencing token), `agent`, and a
`note` (1..600 characters: why the run ended unfinished, what is left). Under the lease lock the
hub proves the lease (token, subject, agent), requires the task to be `in_progress`
(`409 not_in_progress` otherwise), returns it to `todo`, and replaces any earlier hand-back row
with ONE plan row `{kind: "handed_back", lifecycle: true, times: N}` re-appended at the end; then
it releases the lease. Response: `{ok, task, handed_back: N, lease_released, version}`.
`lifecycle` rows are shown on the task and never counted as done steps by any progress surface.
`release` alone returns the lease and leaves the task `in_progress` (a resumable
`stale_reclaim`); hand-back is the whole transition a launcher performs at teardown. Client:
`python -m hub_core.client hand-back <task> --lease-token … --note …`; MCP: `hand_back_task`.
`claim` and `start` append each granted lease to the file named by `HUB_RUN_LEASES` when it is
set, which is how a launcher later proves which leases its run held.

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
| `/hub/api/claim` | Existing task `id`, non-empty string `agent`, optional `ttl_s` (default 900; 1–86400) | Atomically acquires/renews the lease and transitions `todo` to `in_progress`; `200 {ok:true,token,expires,version,…}`. A same-agent retry renews without rotating the token. **Keep `token`.** | `404 not_found`; `409 deps_blocked`, `not_claimable`, or `{ok:false,reason:"held"}`; `422 bad_ttl` |
| `/hub/api/heartbeat` | `id`, `token`, optional `ttl_s` (default 900; 1–86400) | `200 {ok:true,expires}` | `400 need_id_token`; `409 {ok:false,reason:"no/stale lease"}`; `422 bad_ttl` |
| `/hub/api/run` (`run:write`) | `task`, `lease_token`; optional `title`, `goal`, `parent_run`, `trace_id`, `ttl_ms`, `idem_key` | Durable folded AgentRun plus `id`, version, event | `404 not_found`; `409 lease`; `422 parent_run` / schema |
| `/hub/api/run/update` (`run:write`) | `id`, `lease_token`, lifecycle `action` plus its typed fields; optional `expected_version`, `idem_key` | Updated folded run, operation result, and no-replay recovery envelope | `404 not_found`; `409 lease` / `owner` / `transition` / `conflict`; `422 schema` |
| `/hub/api/fail` (`task:fail`) | `id`, fenced `token`, stable `signature`, concrete `note`; optional `kind`, `evidence_uri[]`, `consequential` (default false) | Atomically records the attempt, returns the exact lease, applies bounded backoff/circuit state, and creates or reuses a `hub.repair`-routed task | `400 need_failure`; `409 must_claim`/`lease`/`identity`/`not_in_progress`; `422 bad_failure_evidence`/`bad_consequential` |
| `/hub/api/complete` | `id`, `token` (from claim), `accept_note`, `evidence_uri` (string or array), `verification_run` (required when the task carries a `verification_command`); optional `agent`, `verified_by` (array), `expected_version`, `idem_key` | `200 {data:{id,version,event}}` | See the completion gate below |
| `/hub/api/adr` | Create: `title`, `status`, optional `agent`; `number` and `id` auto-assign. Accepted/superseded/deprecated rows require `context_md`, `decision_md`, and `consequences_md`; superseded also requires `superseded_by[]`. Update: `id` + `expected_version`. | `200 {data}` | `422 schema`, `428 precondition_required`, `409 adr_immutable` for frozen context/decision |
| `/hub/api/capability` | `agent`, `name`, optional `local` | `200 {data}` | `400 need_name` |
| `/hub/api/deploy` | Post-canary `sha`, exactly matching `served_sha`, explicit `tasks_closed[]` (every id must already be a done task), `at`; optional `build`, `method`, `audit_ok`, `agent`, `idem_key` | Creates one immutable SHA-keyed release record; an exact retry returns `idempotent:true` without writing | `422 bad_deploy_fields`, `bad_sha`, `release_not_observed`, `need_tasks_closed`, `duplicate_task_closure`, `invalid_task_closure`; `409 immutable_deploy` |
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
   also accepted) — else `422 evidence_unresolvable`. Strict changes evidence resolvability; it does
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
| `/hub/api/ask` (`ask:write`) | `agent` (the asker), `question`, optional `context`, `relates_to[]`, `anyway`, `hop` | Mints a note tagged `question`+`open` carrying its `asker` in a STABLE field (provenance is rewritten by answering, so deriving the asker from it mis-addresses every re-answered reply). Id keyed on the wording — a retry updates, never twins. Refuses `409 duplicate_question` with the matching ids when the board already has it (open, answered, or crystallized); pass `anyway:true` for a genuinely different question. The guard fails OPEN: a search outage never silences a real ask. `hop` (integer 0..9, else `422 bad_hop`) is the escalation depth of a question raised by an unattended run — set by the process from `HUB_RESPONDER_HOP`, never by the model — and the inbox carries it with the question's `age_s`, so a responder retakes a hop-1 escalation once after a cooldown and leaves hop 2 to a person (`patterns/unattended-responder.md`). |
| `/hub/api/answer` (`directive:write`) | `question` (id or local), `text`, optional `crystallize:true` | ONE verb: mints/updates the answer **directive** targeted at the asker (idempotent per question — re-answering updates in place) AND retires the question (`open`→`answered`). `data.question_still_open:true` flags a retire that failed. `crystallize` additionally mints a standing knowledge note the duplicate-ask guard will match — OPT-IN, because most answers are one-offs and minting indiscriminately taxes the knowledge surface to remember something true for one afternoon. |
| `/hub/api/directive` (`directive:write`) | Create: `title`, `body_md`, optional `targets[]` (default `["all"]`), `remediation_cmd`, `deadline`; update: `id` + `expected_version` | An operator instruction addressed to named agents. `directive:write` is an authority tier above ordinary board writes — the shared-root credential holds it; issue it to a worker credential only deliberately. |
| `/hub/api/ack` (`ack:write`) | `agent`, `directive` (id or local), optional `note` | One agent's record that delivery landed. Stable id (replay-safe). The item leaves that agent's inbox; a directive whose every NAMED target has acked retires itself to `fulfilled`. |
| `/hub/api/presence` (`presence:write`) | `agent` (+ the `X-Hub-*` headers) | The seat heartbeat; the response carries the shared freshness contract. Ordinary writes stamp activity on their own. |
| `/hub/api/forget-presence` (`presence:manage`) | `machine` and/or `target` (the agent name) | Drop a phantom or retired seat row — a decommissioned laptop otherwise sits on the fleet strip looking like a teammate until the retirement horizon. Refuses to drop everything (`422 need_machine_or_target`); connected cockpits wake immediately. |

### The operational error stream

| Endpoint (scope) | Key body fields | Behaviour |
|---|---|---|
| `/hub/api/app-error` (`error:report`) | `app` (slug), `message`, optional `kind`, `severity`, `code`, `details`, `component`, `operation`, `path`, `host` | `201 {fingerprint}` — a satellite service's server exception/job death, attributed to the APP. Forward only what belongs on a queue a human drains. |
| `/hub/api/agent-error` (`error:report`) | `agent`, `message`, optional `source`, `severity`, `details`, `machine` | `201 {fingerprint}` — a worker-side operational failure, so the stream covers more than the hub's own host. |
| `/hub/api/ack-error` (`error:manage`) | `fingerprint`, optional `note`; or `fingerprint` + `reopen:true` | Ack collapses the signature off the queue without deleting rows; reopening a never-acked signature is `409 not_acked`, not a 200 over an untouched row. |
| `/hub/api/clear-errors` (`error:manage`) | `older_than_hours` and/or `only_acked:true` | Bounded by AGE or ACK, never "everything"; `only_acked` is a restriction (an unacked row never drops, however old). Unbounded is `400 need_bound`. |
| `/hub/api/client-error` (same-origin CSRF) | `source`, `message`, optional `severity`, `code` | Bounded browser diagnostics from the board itself; rows are held below the read-time bar by default. |

The sending half — LOGGING handlers for the host app, a bounded fail-soft forwarder for
satellite services — is `patterns/error-visibility.md`; wire it before the first feature.
Rows are redacted at write and throttled per fingerprint (the count is preserved) — an
unthrottled flood does not just add noise, it EVICTS every other error from a bounded store.
Every write on every endpoint above is additionally screened for secret shapes and refused
`422 secret_shaped_payload`: the ledger is append-only, so a secret written into it can never
be removed, only rotated. Recognizable redaction placeholders pass.

See `MOUNTING.md → The evidence-resolution dial` for `tracked` (flow-first, the default) vs `strict`
(dereferenceable-evidence mode).

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
