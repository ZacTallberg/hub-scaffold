# Capability-aware pull routing

> 2026-08-16 · task `example:task:0015` · decision input for ADR-0003

## Question

How should an atomic Hub pull choose work for a particular worker without allowing model-fit,
cost, or latency preferences to falsify the dependency-derived ready frontier?

## Primary sources

- [Kubernetes Scheduling Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
  separates feasibility filtering from weighted scoring: infeasible nodes never reach Score, while
  only feasible nodes are ranked.
- [Kubernetes Scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/)
  treats resource requirements, policy constraints, data locality, and interference as placement
  inputs rather than queue truth.
- [The Kanban Guide, May 2025](https://kanbanguides.org/the-kanban-guide/2025.5/pdf/kanban-guide.v2025.5.en.pdf)
  keeps selection subordinate to an explicit workflow and work-in-progress control.
- [OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
  distinguishes deterministic code routing from model-selected delegation and recommends focused
  specialists for specialized work.

## Findings

⚑ **Eligibility and preference are different decisions.** Hard requirements must filter before
any quality/latency/cost score; a weighted score must never make an incompatible worker eligible.

⚑ **Readiness remains board truth.** Dependencies, status, timer/circuit state, leases, and WIP
produce the ready frontier first. Worker fit only chooses within that frontier; it cannot revive
blocked work or reorder work outside the existing priority/critical-path discipline.

⚑ **Unknown is not compatible.** A task with an explicit capability, risk, resource, locality, or
outcome requirement cannot be assigned on an undeclared worker attribute. Tasks with no routing
requirements preserve the existing zero-profile pull path.

⚑ **Observed outcomes are soft evidence unless a task makes them constraints.** Historical
quality, latency, and cost can rank compatible work. Explicit minimum/maximum outcome constraints
are hard filters. Missing observations remain unmeasured and therefore cannot satisfy a declared
hard constraint.

## Chosen design

Add a task `routing` contract containing required capabilities, risk, resource budget, locality,
hard outcome constraints, and optional outcome weights. An atomic pull accepts a bounded worker
profile containing actual capabilities, risk clearance, current availability, locality, and
observed outcomes. The scheduler executes three composable stages:

1. derive the canonical ready frontier exactly as today;
2. reject incompatible tasks with structured reasons;
3. retain the existing pull order as the primary key, using normalized observed-outcome fit only
   inside an equal ready-order cohort.

The response reports the worker profile and excluded-reason counts so an empty compatible frontier
is distinguishable from an empty board. The profile travels through the HTTP and MCP atomic-pull
surfaces but is not treated as durable worker identity; scoped credentials may supply it later.

## Rejected alternatives

- **Global quality/cost optimization across all tasks:** rejected because it can skip dependency
  heads and turn preference into a second, hidden backlog.
- **Capability-only matching:** rejected because risk, locality, resource headroom, and explicit
  outcome limits are feasibility constraints too.
- **Treat missing profile data as a wildcard:** rejected because it silently assigns constrained
  work to an unknown seat.
- **Persist worker profiles as capability entities:** rejected because `cap` describes reusable
  system capabilities, not a mutable worker-presence record; credential/run work can add durable
  worker state without overloading the capability graph.
