# Read access from the ledger's own grants

**Opt-in pattern.** Nothing here runs until you add the entity type and the middleware below.
It extends the private-board recipe in `SECURITY.md` → *Unauthenticated reads*.

## The failure it removes

A private board admits viewers from a static allowlist — an environment variable, a settings
list — while the same Hub already records who may use which application as reviewable,
revocable board entities. The board then ignores its OWN grants: adding one viewer means
rewriting a deploy variable and redeploying, revoking one means the same, and the answer to "who
can see this board?" lives in two places that drift apart.

## The rule

The set of people admitted to the board is **the static allowlist ∪ the ACTIVE access grants the
ledger holds for this board**. The ledger half has four properties, and each is load-bearing:

1. **Only grants for THIS board count.** A grant carries the slug of the application it is for;
   a grant for another application admits nobody here.
2. **Only `active` grants count.** Revocation is a status change on the grant (append-only, so
   who granted and who revoked stays on the record), effective within one cache interval.
3. **Bounded reads.** The fold is read at most once per `GRANT_TTL_S` (30 s) per process. A sign-in
   must never wait on a ledger fold.
4. **Fail-soft, never wider.** If the ledger cannot be read, keep the LAST KNOWN grant set and do
   not retry on every request. A ledger failure may keep someone out who was just granted; it can
   never admit anyone the last good read did not. Log the failure — it is an operational error,
   not a silent fallback.

Service identities (a CI job, a scheduler, a bot account) are never viewers and never owners of
anything addressed to a person; exclude them by a naming convention you enforce at grant time.

## Wiring it

1. **Add an `access` entity type** through `campaigns/augment-hub.md` (schema, write path, tab,
   audit, seed, docs), with at least: `slug` (the application), `subject` (the identifier your
   authentication resolves a person to — username, email, directory principal), `role`
   (`viewer`, `operator`, …) and `status` (`active` | `revoked`). Gate its writer behind its own
   narrow scope; granting access is an authorization change.
2. **Resolve the admitted set** from the fold:

   ```python
   import logging, os, time

   GRANT_TTL_S = 30
   _GRANTS = {"at": 0.0, "ids": frozenset()}
   log = logging.getLogger(__name__)

   def granted_identifiers(slug, now=None):
       """Identifiers with an ACTIVE access grant for `slug`; bounded and fail-soft."""
       now = now or time.time()
       if now - _GRANTS["at"] < GRANT_TTL_S:
           return _GRANTS["ids"]
       try:
           from hub import hub_app
           entities = hub_app.current_state()["entities"]
           ids = frozenset(
               str(e.get("subject") or "").strip().casefold()
               for e in entities.values()
               if isinstance(e, dict) and e.get("type") == "access"
               and (e.get("status") or "active") == "active"
               and str(e.get("slug") or "").strip().lower() == slug
               and str(e.get("subject") or "").strip())
           _GRANTS.update(at=now, ids=ids)
       except Exception:                       # never block, never widen
           log.warning("access grants unreadable; keeping the last known set", exc_info=True)
           _GRANTS["at"] = now                 # do not retry on every request
       return _GRANTS["ids"]

   def admitted(identifier, slug):
       static = {v.strip().casefold()
                 for v in os.environ.get("HUB_READ_ALLOWLIST", "").split(",") if v.strip()}
       who = (identifier or "").strip().casefold()
       return bool(who) and (who in static or who in granted_identifiers(slug))
   ```

3. **Enforce it** in the private-board middleware from `SECURITY.md`, after Django's
   authentication middleware: an authenticated user whose identifier is not `admitted(...)`
   receives the same opaque response as an anonymous request. Keep the routing-only agent-token
   exception exactly as `SECURITY.md` describes it; grants are for people.

## Proving it

This is an authorization boundary, so it earns one transient probe (`docs/TESTING.md`): with a
disposable ledger, record a grant for a test identifier, confirm the identifier is admitted, revoke
it and confirm it is refused after the cache interval, then make the ledger unreadable and confirm
the admitted set did not grow. Record the receipts and delete the probe.
