# FAILURE MODES — defect-class taxonomy → repair routing map

> canonical · owner: leader (any seat proposes rows) · update: when a repeated or novel failure class improves repair routing

**The doctrine:** an observed real failure becomes a fresh repair task and an `INCIDENTS.md` row.
Classify it here when doing so improves routing or reveals a repeated cause; do not delay the fix to
invent taxonomy. The successful retry of the failed operation is the default proof. No incident
creates a permanent test, fixture, scanner, or workflow. A rare critical boundary may use a
one-shot temporary diagnostic probe, whose receipt is retained after the probe is deleted before
commit. Repeated failures may be routed to a dedicated repair agent so delivery work keeps moving.

Row id = `FM-<group letter><n>`. Suggested starting groups (rename/extend to fit the domain):

## A — Identity / duplication
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## B — World drift (reality changed, we didn't)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## C — Pipeline / ingest
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## D — Model judgment (agent/SLM errors)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## E — Derivation / display (asserted ≠ derived)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## F — Boundary / scope
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## G — Security / abuse
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## H — Process / governance (false-green, done≠live, ledger drift)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|
