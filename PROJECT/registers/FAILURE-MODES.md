# FAILURE MODES — defect-class taxonomy → detector map

> canonical · owner: leader (any seat proposes rows) · update: BEFORE fixing any defect (classify first — DOCTRINE §3.1)

**The doctrine:** every operator-visible defect is classified here FIRST. If no row fits, the
taxonomy grows. The fix is always a class-wide detector (never a point patch), the detector ships
with a self-test that seeds a synthetic violation and proves it fires, and this table is the
checklist for opening any new surface, region, or data source. Instances go to `INCIDENTS.md`.

Row id = `FM-<group letter><n>`. Suggested starting groups (rename/extend to fit the domain):

## A — Identity / duplication
| # | Class | Seen? | Detector |
|---|---|---|---|

## B — World drift (reality changed, we didn't)
| # | Class | Seen? | Detector |
|---|---|---|---|

## C — Pipeline / ingest
| # | Class | Seen? | Detector |
|---|---|---|---|

## D — Model judgment (agent/SLM errors)
| # | Class | Seen? | Detector |
|---|---|---|---|

## E — Derivation / display (asserted ≠ derived)
| # | Class | Seen? | Detector |
|---|---|---|---|

## F — Boundary / scope
| # | Class | Seen? | Detector |
|---|---|---|---|

## G — Security / abuse
| # | Class | Seen? | Detector |
|---|---|---|---|

## H — Process / governance (false-green, done≠live, ledger drift)
| # | Class | Seen? | Detector |
|---|---|---|---|
