# Security model

The Hub is an operations surface for trusted operators and agents. It is not a multi-tenant
authorization system. Deploy it as privileged infrastructure, even when its read pages are useful
to the public.

## Trust boundaries

| Surface | Authentication | Authority |
|---|---|---|
| `GET /hub/*` | None | Read the full projected Hub state, schemas, graph, audit output, questions/inbox, the operational error stream, observed presence, and board search |
| General `POST /hub/api/*` | `X-Write-Token` (general) | Mutate entities, manage leases, ingest errors, refresh presence, and consume launch grants. The GENERAL token additionally issues directives, answers questions, and mints identity tokens |
| Same, with a MINTED token | `X-Write-Token` (per-agent, sha256-only at rest) | Work-grade writes only, attributed to the bound agent; a payload declaring another agent is refused; operator verbs are refused by class |
| `POST /hub/api/launch-grant`, `POST /hub/api/client-error` | Same-origin CSRF, launch mint only when enabled | Mint a short-lived, action/task/count/issuer-bound launch capability / record one bounded, redacted browser diagnostic; neither can mutate board entities |
| Windows protocol handler | Local user context + configured issuer + local token file | Consume a valid grant and start the configured wrapper |

### Two token classes, one fail-closed resolver

`HUB_WRITE_TOKEN` remains the operator-grade credential. `POST /hub/api/mint-token` (general
token only) mints per-agent tokens whose sha256 alone is stored in `HUB_DIR/tokens.json` — a
lost token is re-minted, never recovered, and revocation is per-agent instead of the
all-or-nothing rotation a single shared secret forces. A minted token PROVES identity: writes
attribute to the bound agent by credential, not by claim, and an impersonating payload fails
loudly (`agent_token_mismatch`). Directive/answer/mint authority stays with the general token
by CLASS, so handing a worker its own credential never hands it the ability to command the
fleet. `GET /hub/whoami.json` reports a presented token's class without echoing it. Every 4xx
auth refusal is recorded into the operational error stream — a worker whose token was rotated
out otherwise retries forever while the board shows nothing but a seat going quietly stale.

### The write seam refuses secret-shaped payloads

Every write body is scanned (private keys, platform token formats, JWTs, bearer strings,
credential assignments) and refused `422 secret_shaped_payload` before it can reach the
ledger: the ledger is append-only under a hash chain, so a secret written into it can never be
removed without destroying the tamper-evidence. Recognizable redaction placeholders pass — the
guard must not refuse what a member types after being told to redact. This is a screen against
the accidental paste, not a DLP product; a determined writer can still encode a secret past it.

### The write token is production-grade, but it is NOT code execution

The hub NEVER executes a task's `verification_command`. It used to — `subprocess.run(vc,
shell=True)` inside `complete()` — which made the write token equivalent to arbitrary shell on
the machine serving the hub, reachable by anyone who could write a task. That is removed.

The RECEIPT GATE replaces it and is stronger evidence: the WORKER runs the command out-of-band
and submits a typed receipt — `{command, exit_code, output_sha256, ran_by}` — bound so it cannot
be borrowed into place (`command` must equal the task's own frozen `verification_command`,
`exit_code` must be 0, `ran_by` must be the completing agent). The hub validates what it is
handed; it never becomes the thing that runs untrusted strings.

Treat the write token as production credentials all the same: it grants `done`, deploy records
and ruling ADRs. It just no longer grants a shell.

## Unauthenticated reads

“Public read” means unauthenticated, not automatically sanitized. Every entity field can appear in
the snapshot or a type/entity endpoint. Never store credentials, private URLs, internal topology,
personal data, confidential prompts, or sensitive evidence in Hub entities if the read surface is
reachable by people who must not see them. If the board is private, enforce authentication and
access control in Django or at the reverse proxy and test that boundary.

The new read surfaces widen what "the complete projected board" includes: questions and answers
(`questions.json`, `inbox.json`), observed presence — agent names, machine names, session working
directories and focus lines — and the operational error stream (redacted at write, but messages
originate in your services). The inbox long-poll (`inbox/wait`) additionally holds a server
thread; it is hard-bounded (≤25s, a small waiter pool that degrades to an immediate honest poll
when full), but on a public deployment it is one more reason reads should sit behind your own
authentication boundary. Error-stream ingest (`app-error`, `agent-error`) and queue actions
(`ack-error`, `clear-errors`) are all write-token-gated; the browser's `client-error` is
same-origin CSRF-gated, size-bounded, and its rows are held below the board's bar by default.

## Secret handling

- Generate a distinct high-entropy `HUB_WRITE_TOKEN` for each deployment.
- Inject it through the deployment secret mechanism. Never commit it, place it in query strings,
  paste it into browser storage, or include it in a grant URL.
- Keep workstation token files readable only by the intended operating-system user. The registry
  stores the token file path, not the token value.
- Rotate a token by updating the server and every authorized workstation/client together. Old
  clients fail closed immediately.
- Protect and back up `HUB_DIR`. It contains the event ledger and the launch signing secret when
  worker launch has been used.

## Worker-launch boundary

Worker launch is disabled by default. When enabled:

- the page receives only a short-lived signed grant;
- the final custom-protocol navigation is synchronous and uses the browser's real user activation;
- the workstation requires the grant issuer to match its configured consume endpoint exactly;
- remote issuers must use HTTPS, and redirects are not followed with the token;
- the Hub atomically records a nonce before authorizing a launch, so replay fails closed;
- the wrapper command is supplied by the operator, not by the page or grant;
- the child window is tied to wrapper lifetime and closes when the wrapper exits.

Any web page can attempt to open a registered custom URL scheme. The grant, issuer check, and
authoritative consume are therefore all required; do not replace the handler with one that starts a
process merely because a `hub-worker://` URL was opened.

## Deployment checklist

- Serve `/hub` only over HTTPS outside localhost.
- Set restrictive `ALLOWED_HOSTS`; configure proxy scheme headers correctly.
- Keep `DEBUG` off and require `SECRET_KEY` in production.
- Decide explicitly whether Hub reads are public; otherwise add and test authentication.
- Run the Hub under a least-privilege account with a dedicated durable `HUB_DIR`.
- Restrict outbound network access if strict URL evidence is enabled.
- The hub executes no caller-supplied commands — keep it that way. A fork that re-adds
  server-side `verification_command` execution silently turns the write token into arbitrary
  shell at the service account (the exact RCE removed above).
- Leave `HUB_WORKER_LAUNCH_ENABLED=False` unless the workstation workflow is intentionally deployed.
- Put `hubaudit` in CI/pre-deploy, but do not mistake its focused checks for a complete security
  scanner or penetration test.
- Back up `events.jsonl` and test restoration. Hash chaining is tamper evidence, not backup.

## Security limitations worth preserving in reviews

- The general token grants all write operations under a caller-declared identity; minted tokens
  add proven per-agent identity and per-agent revocation, but there are still no roles or scopes
  beyond the general/minted class split, and reads carry no identity at all.
- The secret-shape scan is a screen against accidental pastes, not data-loss prevention.
- Evidence URL dereferencing follows HTTP behavior available to Python's standard library and is
  not an SSRF sandbox.
- Evidence filesystem paths are resolved from `BASE_DIR` for existence checks but are not a path
  confinement sandbox; a trusted writer can name parent-relative paths.
- Verification commands are intentionally general shell commands, not an allowlist.
- Runtime data is not encrypted by the Hub.
- The route audit verifies that each mutating route declares a recognized gate; it does not prove
  that every future route's business logic is safe.
- Hash-chain verification detects edits, gaps, and reordering in the available log. A privileged
  host actor can still delete the entire store or its backups.

When reporting a vulnerability, do not include live tokens or private Hub data in a public issue.
Use the repository host's private security-reporting channel when one is available.
