# HUB API — the reference for an agent operating the hub

Everything an agent needs to read and drive the Hub over HTTP. Reads are unauthenticated; writes are
token-gated. An unauthenticated read returns the complete projected board, so it is safe to expose
only when entity contents are intentionally publishable. You do not need to read the source—this is
the contract.

- **Base path:** wherever the app is mounted, e.g. `{{LIVE_URL}}/hub` (locally `http://127.0.0.1:8000/hub`).
- **Write auth:** general `POST /hub/api/*` operations require the header
  `X-Write-Token: <token>` and fail closed when it is absent. Reads need nothing. Two token
  classes exist (`GET /hub/whoami.json` reports which one a request presented):
  - **general** — `HUB_WRITE_TOKEN`, the operator-grade credential: terminal board authority,
    identity declared per call. Only it can issue directives, answer questions, or mint tokens.
  - **minted** — a per-agent token from `POST /hub/api/mint-token`: identity is PROVEN (writes
    attribute to the bound agent; a payload declaring someone else is refused `422
    agent_token_mismatch`), revocation is per-agent, and operator verbs are refused `403
    operator_token_required`. Only the sha256 is stored server-side.
  The optional browser endpoints are the narrow exceptions: the launch mint and `client-error`
  are same-origin CSRF-gated, cannot mutate board entities, and the authoritative launch consume
  remains write-token-gated.
- **Write-token power:** the hub never executes a task's `verification_command` (the worker runs
  it out-of-band and submits a typed receipt), but a general-token holder can grant `done`,
  record deploys, rule ADRs, and command agents; strict-mode evidence URLs ARE fetched by the
  server. Treat both classes as production credentials and read [SECURITY.md](../../SECURITY.md)
  before distributing either.
- **Every write is scanned for secret shapes** (private keys, platform tokens, JWTs, credential
  assignments) and refused `422 secret_shaped_payload`: the ledger is append-only, so a secret
  written into it can never be removed, only rotated. Recognizable redaction placeholders pass.
- **Ids** are `{{PROJECT_KEY}}:<type>:<local>`, e.g. `{{PROJECT_KEY}}:task:0001`. Allocated once, never renumbered.
- **Content type:** send `Content-Type: application/json`; bodies are JSON objects.
- **Presence headers (optional, any write):** `X-Hub-Machine`, `X-Hub-Session`, `X-Hub-Cwd`,
  `X-Hub-Focus` — the board's live-console view is built from these; an authenticated write
  refreshes your seat's observed presence automatically.

## Operate the hub as a LOOP, not a pile of endpoints

The endpoints exist to serve one loop. Follow it and you won't hit the common refusals:

```
DISCOVER  GET  /hub/next.json?n=1      → the top unblocked, unclaimed task (your entrypoint)
CLAIM     POST /hub/api/claim          → take the lease BEFORE touching anything; keep the returned token
IMPLEMENT (do the work; new work you find becomes a NEW task via POST /hub/api/task first)
RECORD    POST /hub/api/complete       → done, WITH the lease token + accept_note + evidence
VERIFY    (the server re-runs the audit inside complete; a red audit refuses the done)
```

- **CLAIM before COMPLETE** — completing an unclaimed task returns `409 must_claim`. Claim first, hold the token.
- **`done` never goes through `POST /hub/api/task`** — setting `status:"done"` there returns `409 use_complete`.
  Terminal completion is only `POST /hub/api/complete`, which is evidence- and audit-gated.
- **Decisions are ADRs** (`POST /hub/api/adr`), not task notes. Accepted ADRs are immutable — supersede, don't rewrite.
- **Blocked on a fact only the operator has? `POST /hub/api/ask`** — never stall in silence. The
  question is delivered (the operator's inbox long-poll returns it within a second), the answer
  comes back addressed to you (`GET /hub/inbox.json?agent=<you>`), and your ack closes the loop.
  Search first (`GET /hub/search.json?q=…`) — an ask the board already has is refused with the
  matching ids.

## READ endpoints (GET, public)

| Endpoint | Returns |
|---|---|
| `GET /hub/` | Human dashboard. `?format=json` returns the same snapshot as `hub.json`; `?served=<sha>` adds a caller-observed build to coherence checks. |
| `GET /hub/hub.json` | Full snapshot: `tasks, adrs, feats, gaps, caps, deploys, notes, graph, dangling, build, audit`, derived counts/coverage, worker-launch capability metadata, and the `live` cockpit block (below). Add `?served=<sha>` to compare a live-observed build. |
| `GET /hub/next.json?n=N` | DISCOVER — up to N ranked unblocked tasks without a live lease (urgency = priority + blocker count). `todo` tasks have `stale_reclaim:false`; abandoned `in_progress` tasks whose lease is absent/expired have `stale_reclaim:true`. `n` clamps 1–50; `metadata.available` counts all available rows before truncation (`metadata.unblocked` is retained as a compatibility alias). |
| `GET /hub/audit.json` | the computed audit: `{ok, exit_code, counts, violations[]}`. exit_code 0=pass, 3=warn, 2=violation. |
| `GET /hub/graph.json` | dependency edges + dangling references. |
| `GET /hub/<type>.json` | a whole collection — type ∈ `task, adr, feat, gap, cap, deploy, note, directive, ack`. |
| `GET /hub/<type>/<local>.json` | one entity by local id, e.g. `GET /hub/task/0001.json` (includes computed flags). |
| `GET /hub/questions.json` | every question with the numbers the feed is about: per-row `open/answered/acked` plus `waiting_seconds`/`reply_seconds`, and metadata with the longest wait (and who), median reply time, per-asker lanes, and a 14-day asked/answered strip. "Answered" and "delivered" are different facts; only the asker's ack closes the loop. |
| `GET /hub/inbox.json?agent=<name>` | what is addressed to that agent right now — directives aimed at it, the answer to its own question, and (for the operator) every open question. `{items[], fingerprint, count}`. |
| `GET /hub/inbox/wait?agent=<name>&fp=<fingerprint>&wait=<s>` | **long-poll**: returns the moment the addressed set differs from `fp` (≤25s, bounded waiter pool — past the ceiling it answers immediately with `degraded:true`, an honest poll). A supervisor loop spends its sleep blocked here; that is the mechanism behind "an ask reaches the operator in about a second". |
| `GET /hub/errors.json` | the operational error stream — the failures the ledger audit cannot see — with the BAR applied at read (critical/high in this system's own surfaces; foreign-scanner noise and recovered transport blips deferred, `?include=deferred` shows everything). Metadata carries the 24h histogram, trend, top sources, unclaimed count, and per-channel `coverage` so an empty list says whether it is everything. |
| `GET /hub/search.json?q=…` | ranked multi-term search over the whole board (titles weighted over bodies, exact phrase boosted) — the pull half of "push pointers, pull content". |
| `GET /hub/whoami.json` | what the hub actually received on THIS request: the presented token's class (`general`/`minted`/null), the identity it proves, and which `X-Hub-*` headers survived the proxy. Never echoes the token. |
| `GET /hub/schema/<type>.schema.json` | the JSON schema for a type — read it to know the exact fields before you write. |
| `POST /hub/api/gap` `feat` `note` `deploy` | upsert the remaining entity types. Identity is DERIVED where the content supports it — `feat`/`note` mint a slug from their own name, `deploy` keys on its sha (one release, one record) — so a retried POST updates rather than minting a twin. |
| `POST /hub/api/mcp` | **MCP** (Model Context Protocol, 2026-07-28 + tasks extension) over the board: JSON-RPC 2.0, token-gated, stateless. Tools: `board_next`, `spec_task`, `start_task`, `finish_task`. It never touches the ledger directly — every mutation goes back through the write seam above, so the receipt gate, lease fencing, OCC and schema validation all apply unchanged. |
| `GET /.well-known/agent-card.json` | **A2A AgentCard** (mounted at the ROOT by the host urlconf, where the spec fixes it). Read-only discovery: one skill per task `work_kind`, read live from the schema so it cannot drift, plus `securitySchemes` DESCRIBING the `X-Write-Token` header — the token value never appears. Signed as an ES256 JWS when `cryptography` is installed; served unsigned with a stated `signatureStatus` when it is not. |
| `GET /hub/live/events` | **Server-Sent Events.** A bounded (~52s) stream of `{seq, ts, event, aggregate, version, agent}` envelopes — event IDENTITY only, never payload content. Resume with `Last-Event-ID` or `?since=<seq>`. Emits `ready`, `hub`, `heartbeat`, sidecar ticks `presence` and `errors` (those stores change with NO ledger event — a stream watching only the ledger leaves the fleet strip frozen until unrelated traffic moves), and a closing `reconnect`. Learn THAT something moved, then re-read the canonical board to learn what. |
| `GET /hub/cursor.json` | `{seq, hash, ts}` — the liveness cursor alone, no board contents. What a canary or supervisor polls to prove the board is advancing. |
| `GET /hub/delta.json?since=<seq>` | Everything CHANGED since your cursor: `{changed[], removed[], cursor, audit, live}`. Patch a held snapshot in place instead of re-pulling the whole fold. `since >= head` yields an empty set; a `cursor.seq` below your `since` means the head regressed — fall back to a full snapshot. |
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
| `inflight` | open tasks under a LIVE lease — agent, `machine`, age, `stalled`, plan progress and the `last_note` checkpoint. A lease is a promise, not an observation, so it is checked against one: a holder whose presence went silent is flagged `abandoned` (still shown — somebody's work stopped mid-task — it just stops counting as somebody working). |
| `fleet` | per-agent cards: current lease (with `machine`), plan step, the last checkpoint note, recent action trail, completions, and every live console (`sessions`). An agent with no claim but a fresh console focus reads `active` — working, just not on a board task — never `idle`. |
| `sessions_live` | every live console, flat: agent, machine, session id, cwd, focus, age. The surface that stops two sessions from unknowingly working the same thing. |
| `asks` / `asks_open` | open questions (who, what, since when). They also ride the attention rail. |
| `errors` / `error_log` | the operational stream (bar-annotated rows) and its shape — histogram, trend, top sources, unclaimed count, per-channel coverage. |
| `readiness` | `ready` / `needs_spec` / `snoozed`, with the top few of each. A task with no `verification_command` is not ready — a worker handed one stalls. |
| `adherence` | **is the board still being FOLLOWED and kept current** — six dimensions (`specced, proven, evidenced, fresh, current, moving`), each `{ok, total, unmeasured, pct}`. An empty denominator reports `pct: null`, never 100. `score` averages only the MEASURED dimensions and `unmeasurable` names the rest. |
| `dag` | critical path length, widest frontier, layer widths, the critical `path` itself, and the min-makespan `eta_tasks` for the fleet actually present. `acyclic: false` means the numbers are a floor, not a schedule. |
| `progress` | done/total/pct plus MONOTONIC signals — `completed_total`, `last_1h`, `last_24h`, and a per-bucket `spark`. A ratio alone does not climb when the fleet discovers work as fast as it finishes it. |
| `attention` | the ranked "needs the operator" rail. |
| `worker_health` | receipt outcomes and completions per seat, with denominators. |
| `failure_modes` | what KIND of refusal the fleet keeps hitting, plus the unclassified count. |
| `wip` | the adaptive concurrency ceiling the claim seam enforces. |
| `telemetry` / `cost` | OTLP GenAI aggregate and its dollarized fold; absent until a first instrumented run exists. |

## WRITE endpoints (POST, `X-Write-Token` required)

| Endpoint | Key body fields | Success | Notable refusals |
|---|---|---|---|
| `/hub/api/task` | Create: `title`, optional `agent`; update: `id` + `expected_version` plus changed fields. Optional `priority` (P0–P3), `status` (not `done`), `verification_command`, `deps`, `acceptance`, `phase`, `touches`, `plan`, `implements`, `decided_by`, `surfaced_by`, `source` | `200 {data:{id,version,event}}` | `409 use_complete` (status=done), `428 precondition_required` (update without expected_version), `409 conflict`, `422 schema` |
| `/hub/api/claim` | Existing task `id`, non-empty string `agent`, optional `ttl_s` (default 900; 1–86400) | Atomically acquires/renews the lease and transitions `todo` to `in_progress`; `200 {ok:true,token,expires,version,…}`. A same-agent retry renews without rotating the token. **Keep `token`.** | `404 not_found`; `409 deps_blocked`, `not_claimable`, or `{ok:false,reason:"held"}`; `422 bad_ttl` |
| `/hub/api/heartbeat` | `id`, `token`, optional `ttl_s` (default 900; 1–86400) | `200 {ok:true,expires}` | `400 need_id_token`; `409 {ok:false,reason:"no/stale lease"}`; `422 bad_ttl` |
| `/hub/api/complete` | `id`, `token` (from claim), `accept_note`, `evidence_uri` (string or array), `verification_run` (required when the task carries a `verification_command`); optional `agent`, `verified_by` (array), `expected_version`, `idem_key` | `200 {data:{id,version,event}}` | See the completion gate below |
| `/hub/api/adr` | Create: `title`, `status`, optional `agent`; `number` and `id` auto-assign. Accepted/superseded/deprecated rows require `context_md`, `decision_md`, and `consequences_md`; superseded also requires `superseded_by[]`. Update: `id` + `expected_version`. | `200 {data}` | `422 schema`, `428 precondition_required`, `409 adr_immutable` for frozen context/decision |
| `/hub/api/capability` | `agent`, `name`, optional `local` | `200 {data}` | `400 need_name` |
| `/hub/api/decision` | `agent`, `topic`, `choice`, optional `rationale`,`invalidates`,`refs` | `200 {data:{event}}` (idempotent on topic+choice) | `400 need_topic_choice` |
| `/hub/api/ask` | `agent` (the asker), `question`, optional `context`, `relates_to[]`, `anyway` | `200 {data}` — a note tagged `question`+`open` carrying its `asker`, id keyed on the wording (a retry updates, never twins). Delivered: the operator's inbox wait returns it. | `422 need_agent`; `409 duplicate_question` with the matching ids when the board already has it (open, answered, or crystallized) — read them first, or pass `anyway:true` for a genuinely different question. The guard fails OPEN: a search outage never silences a real ask. |
| `/hub/api/answer` | `question` (id or local), `text` | ONE verb: mints/updates the answer **directive** targeted at the asker (idempotent per question — re-answering updates in place) AND retires the question (`open`→`answered`). `data.question_still_open:true` flags a retire that failed. | **General token only** — `403 operator_token_required`; `404 no_such_question`; `409 unknown_asker` |
| `/hub/api/directive` | Create: `title`, `body_md`, optional `targets[]` (default `["all"]`), `remediation_cmd`, `deadline`; update: `id` + `expected_version` | `200 {data}` — addressed: each targeted agent's inbox carries it until that agent acks. | **General token only** — `403 operator_token_required` |
| `/hub/api/ack` | `agent`, `directive` (id or local), optional `note` | `200 {data}` — stable id (replay-safe). The item leaves that agent's inbox; a directive whose every NAMED target has acked retires itself to `fulfilled`. | `404 unknown_directive`; `422 need_agent` |
| `/hub/api/mint-token` | `target` (agent name); or `target` + `revoke:true` | `200 {data:{token}}` — shown EXACTLY once, sha256-only at rest; the mint is recorded on the ledger. Revocation is per-agent. | **General token only** — `403 operator_token_required`; `422 bad_target` |
| `/hub/api/presence` | `agent` (+ the `X-Hub-*` headers) | `200` with the shared freshness contract — the seat heartbeat; ordinary writes stamp activity on their own. | `422 need_agent` |
| `/hub/api/app-error` | `app` (slug), `message`, optional `kind`, `severity`, `code`, `details`, `component`, `operation`, `path`, `host` | `201 {data:{fingerprint}}` — a satellite service's server exception/job death, attributed to the APP. Forward only what belongs on a queue a human drains. | `400 need_app` |
| `/hub/api/agent-error` | `agent`, `message`, optional `source`, `severity`, `details`, `machine` | `201 {data:{fingerprint}}` — a worker-side operational failure (launcher, tooling), so the stream covers more than the hub's own host. | — |
| `/hub/api/ack-error` | `fingerprint`, optional `note`; or `fingerprint` + `reopen:true` | `201` ack (collapses the signature off the queue without deleting rows) / `200` reopen | `409 not_acked` — reopening a signature that was never acked is a refusal, not a 200 over an untouched row |
| `/hub/api/clear-errors` | `older_than_hours` and/or `only_acked:true` | `200 {data:{removed,kept}}` — bounded by AGE or ACK, never "everything"; `only_acked` is a restriction (an unacked row never drops, however old). | `400 need_bound` |
| `/hub/api/launch-grant/consume` | `consume`, `action:"start"`, `task`, `count` (must match the grant) | `200 {data:{authorized:true,count}}` | `403 launch_refused` (bad/expired/replayed/re-aimed grant) |

### Optional browser launch mint (CSRF, not write-token auth)

`POST /hub/api/launch-grant` accepts `{action:"start", task:"", count:1}` (`count` 1–8) only when
`HUB_WORKER_LAUNCH_ENABLED=True`. It requires the CSRF cookie/header pair emitted by the Hub page and
returns a signed, short-lived single-use grant. This endpoint is for the in-page control, not agents;
agents should never copy the general write token into browser storage. See `adapters/windows/README.md`
for the workstation half of the issuer-bound consume protocol.

### The completion gate (`/hub/api/complete`) — what it checks, in order
1. Lease — you must hold a valid claim (`409 must_claim` if unclaimed, `409 lease` if the token is stale/another agent's).
2. `accept_note` + at least one `evidence_uri` — else `422 need_evidence`.
3. **strict mode only** (`HUB_DONE_STRICTNESS=strict`): every `evidence_uri` must dereference — a URL returning
   <400, a commit sha in this repo, or an existing path resolved from `BASE_DIR` (absolute paths are
   also accepted) — else
   `422 evidence_unresolvable` (checked BEFORE the verification_command check); and the task must carry a
   `verification_command` (set it via `/hub/api/task` first) — else `422 need_verification_command`.
4. If the task has a `verification_command`, you must supply a typed `verification_run` receipt —
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
   agent. The receipt is stored on the entity, so the completion stays falsifiable afterwards.
5. The server re-runs the audit; a `critical` violation is `422 audit_unsound` — fix the unsoundness, don't retry.
6. Immediately before append, the server rechecks the fencing token under the lease lock and binds
   completion to the exact entity version whose command was verified. A concurrent edit or expired/
   reclaimed lease refuses completion. Success releases the lease; an abandoned `in_progress` task
   becomes discoverable again after expiry.

See `MOUNTING.md → The strictness dial` for `tracked` (flow-first, the default) vs `strict` (proof-first).

## Error responses

Most write refusals are `{errors:[{code, msg, …}]}`:

`forbidden`/`operator_token_required` (403) · `bad_json`/`missing_id`/`need_id_agent`/`need_id_token`/
`need_name`/`need_topic_choice`/`need_question`/`need_question_and_text`/`need_app`/`need_bound`/
`need_fingerprint`/`missing_grant` (400) · `use_complete` (409) ·
`precondition_required` (428 OCC) · `conflict` (409 OCC version race) ·
`must_claim`/`lease`/`deps_blocked`/`not_claimable`/`duplicate_question`/`unknown_asker`/`not_acked` (409) ·
`bad_ttl`/`need_agent`/`agent_token_mismatch`/`secret_shaped_payload`/`bad_target` (422) ·
`need_evidence`/`evidence_unresolvable`/
`need_verification_command`/`verify_failed`/`verify_error`/`audit_unsound` (422) ·
`adr_immutable` (409) · `bad_grant_request` (422) · `launch_disabled`/`not_found`/`no_such_question`/`unknown_directive` (404) ·
`launch_refused` (403) · `launch_unavailable` (503).

An actively held claim and a stale heartbeat are the exceptions: they return `{ok:false, reason:…}`
with status 409. A wrong method returns Django's 405 response, and missing read entities use Django's ordinary
404 response. Treat a refusal as guidance—fix its cause rather than retrying blindly.

## Worked example (the full loop, curl)

```bash
BASE={{LIVE_URL}}/hub ; TOK=$HUB_WRITE_TOKEN ; H="-H Content-Type:application/json -H X-Write-Token:$TOK"
# DISCOVER
curl -s "$BASE/next.json?n=1"
# CREATE (if you need a new task) — capture the id from the response
curl -s $H -d '{"title":"Wire the export endpoint","agent":"me","priority":"P1"}' "$BASE/api/task"
# CLAIM — capture token
curl -s $H -d '{"id":"{{PROJECT_KEY}}:task:0001","agent":"me"}' "$BASE/api/claim"
# COMPLETE — with the lease token + evidence
# You run the verification_command YOURSELF first, then report it — the hub never runs it.
curl -s $H -d '{"id":"{{PROJECT_KEY}}:task:0001","token":"<lease-token>","agent":"me",
                "accept_note":"shipped + verified","evidence_uri":["<commit-sha-or-url-or-path>"],
                "verification_run":{"command":"<the task'"'"'s verification_command>","exit_code":0,
                                    "output_sha256":"<sha256 of its output>","ran_by":"me"}}' "$BASE/api/complete"
```

Reads are a plain `curl "$BASE/hub.json"`. That's the whole API — reach for the loop, not the endpoints.
