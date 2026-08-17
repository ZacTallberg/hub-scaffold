# Security model

The Hub is an operations surface for trusted operators and agents. It is not a multi-tenant
authorization system. Deploy it as privileged infrastructure, even when its read pages are useful
to the public.

## Trust boundaries

| Surface | Authentication | Authority |
|---|---|---|
| `GET /hub/*` | None | Read the full projected Hub state, schemas, graph, and audit output |
| General `POST /hub/api/*` | Scoped `X-Agent-Token` (preferred) or explicitly enabled `X-Write-Token` compatibility | Mutate only operations named by the credential; claimed-task writes also require its fenced lease |
| `POST /hub/api/agent-credential` | `credential:manage` scope or shared-root compatibility | Issue/list/revoke short-lived agent credentials; the bearer token is returned once |
| `POST /hub/api/launch-grant` | Same-origin CSRF, only when enabled | Mint a short-lived, action/task/count/issuer-bound launch capability; cannot mutate board entities |
| Windows protocol handler | Local user context + configured issuer + local token file | Consume a valid grant and start the configured wrapper |

### Scoped worker authority and the shared-root migration bridge

Normal workers authenticate with `X-Agent-Token`. Each credential freezes one subject, carries an
explicit operation-scope list and expiry, and can be revoked independently. The host-local registry
under `HUB_DIR` stores only a SHA-256 token digest. A caller cannot override its authenticated
subject with the JSON `agent` field.

A live task lease binds its fencing token to that authenticated subject and credential id. Updating
an in-progress/leased task, completing it, heartbeating it, or releasing it requires the same
credential plus the current fencing token. A leaked token therefore cannot mutate another
worker's claimed task, and a stale lease token cannot outlive expiry/reclaim.

`X-Write-Token` remains a migration bridge for existing installations while
`HUB_SHARED_TOKEN_COMPAT=True`. It has root scope, so restrict it to operators. Its ledger events,
response headers, and fresh leases identify `shared-root-compat` as the actor even when an old
request carries a named worker-seat label; that label never masquerades as authentication. Disable
the bridge after issuing scoped credentials to the fleet.

### Write authority is production-grade, but it is NOT code execution

The hub NEVER executes a task's `verification_command`. It used to — `subprocess.run(vc,
shell=True)` inside `complete()` — which made the write token equivalent to arbitrary shell on
the machine serving the hub, reachable by anyone who could write a task. That is removed.

The RECEIPT GATE replaces it and is stronger evidence: the WORKER runs the command out-of-band
and submits a typed receipt — `{command, exit_code, output_sha256, ran_by}` — bound so it cannot
be borrowed into place (`command` must equal the task's own frozen `verification_command`,
`exit_code` must be 0, `ran_by` must be the completing agent). The hub validates what it is
handed; it never becomes the thing that runs untrusted strings.

Treat every credential according to its scopes all the same: completion, deploy, ADR, and credential
management scopes grant consequential authority. They do not grant a shell.

## Unauthenticated reads

“Public read” means unauthenticated, not automatically sanitized. Every entity field can appear in
the snapshot or a type/entity endpoint. Never store credentials, private URLs, internal topology,
personal data, confidential prompts, or sensitive evidence in Hub entities if the read surface is
reachable by people who must not see them. If the board is private, enforce authentication and
access control in Django or at the reverse proxy and test that boundary.

For a private board, middleware may make one routing-only exception so a **valid scoped agent** can
reach `/hub/api/*` without gaining `/hub/`, collection, audit, schema, or live-stream reads. Preserve
an opaque boundary: header presence alone would turn an anonymous 404 into the API's 403 and reveal
the route. Instead, pre-authenticate the token against the canonical registry; invalid, expired,
revoked, malformed, or missing credentials receive the same opaque response as every other private
request. `@writer` then authenticates again and remains the final identity + scope authority.

For example, in middleware placed after Django's authentication middleware:

```python
from django.http import HttpResponseNotFound
from hub import hub_app
from hub_core.agent_auth import CredentialError, CredentialRegistry

path = request.path_info
token = request.headers.get("X-Agent-Token") or ""
if path.startswith("/hub/api/") and token:
    try:
        CredentialRegistry(hub_app.HUB_DIR).authenticate(token)
    except CredentialError:
        pass
    else:
        return self.get_response(request)  # routing only; @writer reauthenticates + checks scope
if path.startswith("/hub/") and not request.user.is_authenticated:
    return HttpResponseNotFound()  # or the adopter's existing identical private-login response
```

This pre-authentication grants no read or write authority, creates no user session, and must never
be cached as authorization. Do not exempt any read route. A valid credential reaches the endpoint;
only its `@writer` decorator decides whether that credential's scopes permit the operation.

## Secret handling

- Generate a distinct high-entropy `HUB_WRITE_TOKEN` for each deployment while compatibility is
  enabled, and set `HUB_SHARED_TOKEN_COMPAT=False` once migration is complete.
- Issue worker credentials with the smallest needed scopes and shortest useful expiry through
  `POST /hub/api/agent-credential`; revoke a seat immediately when its work or host lifetime ends.
- Inject it through the deployment secret mechanism. Never commit it, place it in query strings,
  paste it into browser storage, or include it in a grant URL.
- Keep workstation token files readable only by the intended operating-system user. The registry
  stores the token file path, not the token value.
- Rotate a token by updating the server and every authorized workstation/client together. Old
  clients fail closed immediately.
- Protect and back up `HUB_DIR`. It contains the event ledger, hashed agent-credential registry,
  and the launch signing secret when worker launch has been used.

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
- Prefer scoped agent credentials; monitor the explicit `shared-root-compat` event actor and disable
  `HUB_SHARED_TOKEN_COMPAT` when no legacy client remains.
- Invoke `hubaudit` deliberately for structural governance maintenance, never as an automatic
  per-task or copy/style gate.
- Back up `events.jsonl`; when restore integrity is a critical concern, perform a transient restore
  drill and discard its temporary environment. Hash chaining is tamper evidence, not backup.

## Security limitations worth preserving in reviews

- The Hub provides first-party agent credential scopes, expiry, and revocation, not end-user login,
  delegated OAuth, tenant isolation, or policy-as-code. Place human access behind the host's real
  authentication/authorization boundary.
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
