# HUB API — the reference for an agent operating the hub

Everything an agent needs to read and drive the hub over HTTP. Reads are public and safe; writes are
token-gated. You do not need to read the source — this is the contract.

- **Base path:** wherever the app is mounted, e.g. `{{LIVE_URL}}/hub` (locally `http://127.0.0.1:8000/hub`).
- **Write auth:** every `POST /hub/api/*` requires the header `X-Write-Token: <HUB_WRITE_TOKEN>`. Writes
  are **fail-closed**: if the server has no token configured, every write is `403`. Reads need nothing.
- **Ids** are `{{PROJECT_KEY}}:<type>:<local>`, e.g. `{{PROJECT_KEY}}:task:0001`. Allocated once, never renumbered.
- **Content type:** send `Content-Type: application/json`; bodies are JSON objects.

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

## READ endpoints (GET, public)

| Endpoint | Returns |
|---|---|
| `GET /hub/hub.json` | full snapshot: `tasks, adrs, feats, gaps, caps, deploys, notes, build, audit`. Add `?served=<sha>` to have coherence checked against a live-observed build. |
| `GET /hub/next.json?n=N` | DISCOVER — up to N ranked unblocked+unclaimed tasks (urgency = priority + blocker count). `n` clamps 1–50. |
| `GET /hub/audit.json` | the computed audit: `{ok, exit_code, counts, violations[]}`. exit_code 0=pass, 3=warn, 2=violation. |
| `GET /hub/graph.json` | dependency edges + dangling references. |
| `GET /hub/<type>.json` | a whole collection — type ∈ `task, adr, feat, gap, cap, deploy, note`. |
| `GET /hub/<type>/<local>.json` | one entity by local id, e.g. `GET /hub/task/0001.json` (includes computed flags). |
| `GET /hub/schema/<type>.schema.json` | the JSON schema for a type — read it to know the exact fields before you write. |

## WRITE endpoints (POST, `X-Write-Token` required)

| Endpoint | Key body fields | Success | Notable refusals |
|---|---|---|---|
| `/hub/api/task` | `title`, `agent`; to update: `id` + `expected_version`; optional `priority`(P0–P3), `status`(todo/…, NOT done), `verification_command`, `depends_on` | `200 {data:{id,version,event}}` | `409 use_complete` (status=done), `428 precondition_required` (update without expected_version), `422 schema` |
| `/hub/api/claim` | `id`, `agent`, optional `ttl_s`(default 900) | `200 {ok:true, token, …}` — **keep `token`** | `409` (held by another agent) |
| `/hub/api/heartbeat` | `id`, `token`, optional `ttl_s` | `200 {ok:true}` | `409` (lost/stale lease) |
| `/hub/api/complete` | `id`, `token`(from claim), `agent`, `accept_note`, `evidence_uri`(string or array), optional `verified_by`, `expected_version`, `idem_key` | `200 {data:{id,version,event}}` | see the completion gate below |
| `/hub/api/adr` | `agent`, `title`, `status`(proposed/accepted/…); `number`+`id` are auto-assigned. `status:"accepted"` ALSO requires `context_md`+`decision_md`+`consequences_md`; `superseded` requires `superseded_by[]`. Update: `id` | `200 {data}` | `422 schema` (missing a required field), `409 adr_immutable` (editing an accepted ADR's context/decision) |
| `/hub/api/capability` | `agent`, `name`, optional `local` | `200 {data}` | `400 need_name` |
| `/hub/api/decision` | `agent`, `topic`, `choice`, optional `rationale`,`invalidates`,`refs` | `200 {data:{event}}` (idempotent on topic+choice) | `400 need_topic_choice` |

### The completion gate (`/hub/api/complete`) — what it checks, in order
1. Lease — you must hold a valid claim (`409 must_claim` if unclaimed, `409 lease` if the token is stale/another agent's).
2. `accept_note` + at least one `evidence_uri` — else `422 need_evidence`.
3. **strict mode only** (`HUB_DONE_STRICTNESS=strict`): every `evidence_uri` must dereference — a URL returning
   <400, a commit sha in this repo, or an existing path **relative to the project root** — else
   `422 evidence_unresolvable` (checked BEFORE the verification_command check); and the task must carry a
   `verification_command` (set it via `/hub/api/task` first) — else `422 need_verification_command`.
4. If the task has a `verification_command`, the server RUNS it; a non-zero exit is `422 verify_failed`
   (this happens in `tracked` mode too, whenever a command is present).
5. The server re-runs the audit; a `critical` violation is `422 audit_unsound` — fix the unsoundness, don't retry.

See `MOUNTING.md → The strictness dial` for `tracked` (flow-first, the default) vs `strict` (proof-first).

## Error codes you'll see (all as `{errors:[{code, msg, …}]}`)

`forbidden`(403 missing/invalid token) · `bad_json`(400) · method not POST (405) · `use_complete`(409) ·
`precondition_required`(428 OCC) · `conflict`(409 OCC version race) · `must_claim`/`lease`(409) ·
`need_evidence`/`evidence_unresolvable`/`need_verification_command`/`verify_failed`/`audit_unsound`(422) ·
`adr_immutable`(409) · `not_found`(404). A refusal is guidance — read `msg`, fix the cause, don't hammer.

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
curl -s $H -d '{"id":"{{PROJECT_KEY}}:task:0001","token":"<lease-token>","agent":"me",
                "accept_note":"shipped + verified","evidence_uri":["<commit-sha-or-url-or-path>"]}' "$BASE/api/complete"
```

Reads are a plain `curl "$BASE/hub.json"`. That's the whole API — reach for the loop, not the endpoints.
