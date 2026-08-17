# Scoped agent authority at the Hub write seam

## Question

How can a Hub preserve its simple service-token deployment path while giving autonomous workers
immutable identities, least-privilege authority, expiry, revocation, and lease-bound mutation
rights?

## Findings

⚑ **Authentication identity and worker-seat labels are different facts.** A shared operator token
may coordinate a named seat for compatibility, but the canonical event must identify the shared
root credential as the actor. Treating a caller-authored `agent` field as authentication permits
silent impersonation.

⚑ **A capability is useful only when it is bounded in four dimensions:** subject, operation scope,
expiry, and revocation state. The OAuth security best-current-practice principles of least
privilege, audience restriction, and sender/credential binding apply even though the Hub uses a
small first-party token registry rather than implementing OAuth.

⚑ **The lease is the task-mutation capability.** Authentication says who is calling; the fenced
lease says which claimed task that subject may mutate now. Every mutation of in-progress or
actively leased work therefore needs both a valid credential and the current fencing token.

⚑ **Compatibility must be conspicuous.** The legacy `X-Write-Token` can remain an opt-out migration
bridge, but its events, responses, and leases must say `shared-root-compat`. It must not inherit a
caller-supplied worker name as its authenticated subject.

## Design consequence

The Hub will keep a host-local credential registry under its durable Hub directory. Tokens are
returned only at issuance and stored only as SHA-256 digests; records freeze the subject and carry
explicit scopes, issuance/expiry timestamps, and revocation metadata. `X-Agent-Token` authenticates
normal workers. The write decorator binds an immutable auth context to the request, enforces the
endpoint scope, and supplies that context to canonical event appends. Task leases persist the same
subject and credential id. A task update, completion, heartbeat, or release succeeds only when the
request subject and fencing token match the lease.

The shared root token remains available by default for existing installations, can be disabled with
`HUB_SHARED_TOKEN_COMPAT=False`, and is recorded as a root-compatibility actor regardless of the
seat label supplied in an old request body.

