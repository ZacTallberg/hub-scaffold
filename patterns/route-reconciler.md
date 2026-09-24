# Pattern: a route reconciler that reports every pass and refuses per entry

**When you need it.** Your Hub (or any registry) owns a list of apps and ports, and a separate
process on the edge host renders that list into reverse-proxy configuration (an nginx include, a
Caddyfile, an ingress manifest) and reloads the proxy. The registry is the source of truth; the
reconciler is the only thing with rights on the proxy. This pattern is the generic core of such a
reconciler. It is **not shipped as a running component** — the render target, the reload command
and the host's privileges are yours. Wire it, then prove it with a real pass.

## The two failure shapes it exists to prevent

1. **A silent reconciler.** The registry says an app is routed; the proxy never got the line,
   because the reconciler failed, backed off, or ran an old build — and nothing on the board says
   so. Every "is my app live?" question is then answered by guesswork.
2. **One bad entry freezing everyone.** A strict validator raises on a single malformed entry, the
   pass "keeps the last good state" for the WHOLE registry, and every app's route is frozen for as
   long as that one entry stays bad — while the refused app itself may be up and healthy. The
   refusal was right; its blast radius was not. A detector's failure must cost the thing it
   refuses, never everything else.

## The contract

**Validate entry by entry.** Split the registry into `valid` and `refused` (`slug`, reason):
malformed slug, a host that is not what your proxy may reach, a port outside the band **both
sides pin** (the registry's allocator and the reconciler must share one band — a band widened on
one side only is exactly how a new app gets refused). The refused entry loses its own route and
nobody else's.

**Keep a mass-refusal guard, ahead of every other guard.** Per-entry refusal must not become a
straight loosening: a registry this build cannot parse at all would otherwise render an EMPTY
configuration and unroute the fleet — worse than the freeze it replaced. Every entry refused, or
a majority of currently-live routes refused, keeps the last good state and says why. Likewise a
confident zero (the registry answered with no routes while routes are live) and a majority drop
are refused unless an independent count corroborates them.

**Report every pass to the Hub** (`POST` a small record: `ok`, `error`, the etag it rendered, the
etag the proxy actually holds, `routes`, `skipped`, `refused`, `reloaded`, `backed_off`). The
report must survive whichever exit the pass took — keep the pass's facts in one record that every
exit path fills in. Authenticate it with its own narrow credential (a reconciler identity with only
the report scope), and accept either binding of that token during a rotation so the report is not
lost exactly when you change it.

**Read the report back as a verdict, per app.** A doctor/readiness view should say one of:

| verdict | meaning |
|---|---|
| `LIVE` | the last reported pass applied an etag that contains this route |
| `PENDING` | the route changed after the last applied etag; the next pass should pick it up |
| `REFUSED` | the last pass refused this entry — show the reason |
| `SKIPPED` | the reconciler deliberately did not render it (disabled, reserved) |
| `STALE` | the last report is older than a few reconcile intervals — the reconciler itself is suspect |
| `UNREPORTED` | no pass has ever reported — the reconciler is not wired, whatever the registry says |

A change in the refused set is an audited transition (a warning row naming the entry), not a
heartbeat: per-entry refusal invents a new state — `ok: true`, etag applied, one route missing —
and left unreported it would be a new silent failure.

## Proving it

The real operation is the proof: register an app with a deliberately out-of-band port and watch
one pass refuse only that entry (the report names it; every other route still renders and the
proxy reloads), then fix the entry and watch the next pass serve it. A transient probe is justified
only for the mass-refusal guard (a data-integrity boundary): feed one pass a registry where every
entry is refused, confirm the configuration on disk is unchanged, record the receipt, delete the
probe.
