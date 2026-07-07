# CHARTER — <project name>

> template → becomes canonical once filled · owner: leader · update: by ADR only (scope changes are decisions)

The charter is the contract for WHAT this project is and WHEN it is done. It changes rarely and
only by ADR. If work in flight contradicts the charter, the work is wrong or the charter needs an
ADR — never both silently.

## 1. Mission
One paragraph: what this delivers, for whom, and the single sentence a stranger repeats back.

## 2. Users & surfaces
Who touches it (operator, public, agents) and through what (web root, API, feeds, /hub).

## 3. Scope
The capabilities this project promises. Each maps to hub `feat` entities once building starts.

## 4. Non-goals
Explicitly out of scope, with the reason. A non-goal may only move into scope via ADR.

## 5. Quality bar
- **Born-safe:** prod settings hardened at birth (no DEBUG default, no committed secrets, token-gated writes).
- **Truth-first:** every rendered assertion derives from gathered evidence (`DOCTRINE.md` §2) —
  the truth matrix (`registers/TRUTH-MATRIX.md`) is the acceptance checklist for any new surface.
- **Gate-green:** `hubaudit` PASS + project invariant gates green are ship preconditions, fail-closed.
- **Best-of-breed:** research before build (`research/README.md`); re-architect rather than polish a failing approach.

## 6. Definition of done (project-level)
Machine-binary first, judgment second:
1. All promised capabilities exist, are WIRED (reachable in the served artifact), and are hub `feat: shipped` with linked tasks.
2. `hubaudit` exit 0; invariant gates green; deploy coherence proven (served SHA == blessed SHA).
3. Live front-door verification passed (patient canary, real UA, asserts the app's own body).
4. Zero open P0/P1 gaps; every open `DP-` decision either resolved or explicitly deferred by the operator.
5. `HANDOFF.md` current; audit history complete (every deploy/run/incident recorded).

## 7. Run model & cost ceiling
Compute tiers this project may use, hard cost ceiling, and what happens at the ceiling (stop, not degrade silently).

## 8. Data & legal posture
Where data comes from, what may be stored/published, rate-limit/courtesy rules, takedown stance.
