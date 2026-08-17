# RESEARCH HISTORY — the chronicle of WHY

> canonical · owner: leader (or principal agent) · update: same session as any research/decision lands

This is the running answer to "why is the architecture what it is". Newest first. Every entry:
what question was open, what was found (keystones ⚑), and what it decided (→ ADR ids). Full detail
lives in the per-effort files in this folder; this file is the index a cold agent can actually read.

<!-- Entry format:
## YYYY-MM-DD — <question / effort title>
**Source:** <file in research/ | chat-mined | inline> · **Fed:** ADR-NNNN, gap ids
⚑ <keystone finding, one line each>
<2–5 sentences of what was learned and what it changed.>
-->

## 2026-08-16 — Capability-aware atomic pull routing
**Source:** `2026-08-16-capability-aware-pull-routing.md` · **Fed:** ADR-0003, `example:task:0015`
⚑ Feasibility filtering precedes preference scoring; neither may replace the dependency-derived ready frontier.
⚑ Unknown worker attributes cannot satisfy explicit task constraints, while unconstrained tasks preserve zero-profile compatibility.
⚑ Observed quality, latency, and cost rank compatible work only inside the existing pull-order cohort.

The design adopts a typed task routing contract and a bounded per-pull worker profile. It keeps
readiness, priority, critical-path rank, aging, touch collision, and WIP authoritative, exposes
structured exclusion reasons, and leaves durable worker identity to the scoped-credential plane.

## 2026-08-16 — authored identity without cockpit forks
**Source:** `2026-08-16-authored-hub-identity.md` · **Fed:** example:task:0016
⚑ Project character can be expressed by a small validated token set while structure, semantic
status, accessibility, and state-derived motion remain canonical.
The visual brief places mark, accent pairing, display voice, surface character, and motif in the
portable identity file. The renderer consumes bounded values only, and initialization produces a
distinct overridable starter rather than another identical blue cube.

## 2026-08-16 — scoped autonomous-agent authority
**Source:** `2026-08-16-scoped-agent-authority.md` · **Fed:** ADR-0002, `example:task:0012`
⚑ Authentication subject and worker-seat label must be separate canonical facts.
⚑ Every claimed-task mutation needs both scoped authentication and the current fenced lease.
⚑ Shared-token compatibility is safe only when it is visibly recorded as root compatibility.
The resulting design uses short-lived, revocable, scope-bearing agent credentials; binds their
immutable subjects into leases and ledger provenance; and keeps the legacy root token as a
disable-able migration bridge rather than allowing caller-authored identity to masquerade as auth.
