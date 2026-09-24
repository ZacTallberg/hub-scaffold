# App slots — allocate, stage, cut over, retire, re-adopt

An operator who lets agents ship apps on shared infrastructure ends up needing a **slot
registry**: one record per app naming its slug, its port, its service name, the repository it
deploys from, and whether the front door routes to it. This pattern is the state machine that
registry must obey. It is deliberately a write-up, not a module: the registry's storage, the host
that runs the services, and the proxy that renders routes are all environment-specific. The
rules below are not.

## The record

| Field | Rule |
|---|---|
| `slug` | The URL segment and the key. Validated shape (`^[a-z0-9][a-z0-9-]{2,62}$` or yours). |
| `port` | Allocated, never typed: a deterministic candidate (for example `BASE + sha256(slug) % SPAN`) probed forward against the registry, every checkout's declared service manifest on the host, and a live bind test. Collisions become impossible instead of a failed bootstrap. Check the port against the band the **proxy** will actually render at the moment it enters — a route the proxy refuses is a route nobody gets. |
| `service` | The host's process name. Owned by exactly one slot. |
| `project` | The repository path this app deploys from, **recorded on the slot**. Never re-derived from the slug: two repositories can share a slug-shaped name (an app and the data pipeline that feeds it), and a diagnosis that resolves the wrong one reports "ok" about somebody else's code. Changing it is refused (`409`) so nothing quietly starts diagnosing a different repository. |
| `status` | `active` (routed), `staged` (allocated, route withheld), `retired`. |
| `inherited` | The slot kept an identity (service name, port) that existed before it — see takeover. |

The route registry the proxy reads projects **active slots only**.

## Transitions

```
provision <slug>                 -> active     fresh slug: allocate port + service, publish route
provision <slug> --takeover      -> staged     the slug is ALREADY served by something else
provision <slug> --cutover       staged -> active   only when the new service is proven up
provision-retire <slug>          active|staged -> retired   route withdrawn; nothing deleted
provision <slug>  (own retired)  retired -> active|staged   re-adopted in place
```

**Refuse a fresh slug the proxy already answers for.** A generated route that lands ahead of an
existing catch-all hijacks a live app within one reconcile cycle. That guard is right, and it
also makes moving an app out of a shared host impossible under its existing URL — hence:

**Takeover is a separate, privileged lane.** It allocates exactly as a fresh app would (port,
service name, the host-approval request) but in status `staged`, with the route **withheld**, so
the legacy route keeps serving while the new service bootstraps and is checked on its own port.
Gate it behind a higher privilege than an ordinary provision: it is precisely the thing the guard
exists to stop.

**Cutover refuses unless the new service is really there.** For a fresh identity, "the slot's
port is listening on the app host" (a bind test from the host itself; an off-host HTTP probe can
false-negative, and a route to a dead port is a live app going 502). For an **inherited**
identity the port was listening all along under the old owner, so "listening" proves nothing:
gate instead on something only the new deployment makes true (the service's registered command
line naming the new checkout). An unreadable check never cuts over.

**Inheritance is a decision, not an accident.** A takeover may keep the old app's service name
and port when the app is the same app leaving a shared host (its approvals, firewall rules and
runbooks stay valid). Accept a requested port only for a takeover, and refuse it when another
active or staged slot holds it or a host manifest names a *different* service on it. The old
owner must release its own manifest claim before cutover, so two checkouts never declare one
service.

**Retire withdraws the route and releases the slot — never the service, the checkout, or the
data.** It is a rollback, and it must be recoverable:

**A slug may re-adopt its own retired slot.** Retire releases the service *name* while the process
keeps running; a naive re-provision then meets "this service exists on the host and no slot owns
it" and refuses with no way back. That refusal exists for a name *nobody* owns. When the retiring
slug is the one asking, re-stage the slot **in place** — same port, same service, no second
approval. A takeover comes back `staged` (route withheld until cutover); a slug that served its
own route comes back `active`, because withholding it would leave the running service unreachable,
which is the state being recovered from. An explicit request for a *different* service name is
not a re-adoption and still meets the collision checks.

## Diagnosis must tell one story

A diagnostic ("why is this app not live?") reads the slot and the pipeline. Two rules keep it
honest:

- **A staged takeover is one state, not a pile of blockers.** "Service created, route withheld by
  design, next step is the owner's cutover" — not "approval missing" plus "deploy record missing".
  Read approvals from the host outcome (the service exists) rather than a flag written once at
  provision time by a process that could not see the grant.
- **A verification that probes the public URL cannot pass before cutover**, because that URL
  still belongs to the old owner. On a staged slot, when something deploy-shaped built green and
  every failed job is a verification job, report it as *expected before cutover* — and nothing
  broader: a deploy that genuinely failed is still a blocker. Give "waiting" its own mark in the
  renderer and print its remedy; a waiting finding whose next step is invisible reads as unknown.

## What stays with the adopter

The registry store, the host-approval gate (keep it human-maintained if your host is shared —
the one deliberate approval), the process manager, and the proxy reconciler. Record each slot
transition on the Hub (a `cap` or `note`, or your own entity type via `campaigns/augment-hub.md`)
so "who moved this app, and when" has an answer.
