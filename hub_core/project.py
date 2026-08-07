"""The pure projection fold: events -> entities -> derived hub state.

Stack-neutral, no I/O. `fold()` replays the event log into per-aggregate entities;
`derive()` computes the dependency DAG (blocked), the cross-link graph, dangling-idref
detection, and counts/phases/coverage. Everything the hub renders (state.json, .md docs,
/hub.json, the audit) is built from `state()`.
"""

import datetime as _dt
import re

from . import upcast as _upcast

# Entity types that fold into the projected state (everything else, e.g. decision/claim, is a
# log-only event kept in the event store but not materialized as an entity).
_KNOWN = {"task", "adr", "feat", "gap", "cap", "contract", "deploy", "commit", "note",
          # Ported/redesigned for this project (see ADR-9): finding + lesson are generic;
          # method is redesigned for CHARTER §5 (a method here is governance AND public content);
          # review replaces a domain-specific "surfacing" step with the §4.3/§10.4 human gate.
          "finding", "lesson", "method", "review",
          # telemetry = one worker SESSION (hub.worker-telemetry-event-and-cockpit): the fleet's
          # own health folds like every other entity instead of hiding in note bodies.
          "telemetry"}


def register_types(names):
    """Admit instance-local entity types to the fold.

    The Django adapter calls this with every name PROJECT/schema/*.schema.json declares, so a
    domain-specific instance (an older generation's own nouns) keeps
    folding its whole history without an engine edit — dropping events an older generation of
    this engine recorded is a capture failure, not a migration."""
    _KNOWN.update(n for n in names if n)
# Statuses that count as "satisfied" for dependency purposes.
_DONE = {"done", "closed", "shipped", "accepted", "extracted", "reusable", "proven"}
# Dependency SATISFACTION additionally treats a dropped dep as terminally resolved, matching the
# deps-before-done write gate (done, dropped) — a dropped dep blocks nothing forever. Without this
# a todo task whose only unmet-status dep was dropped is never unblocked, never listed by
# /hub/next.json, and not on the dangling rail: it appears NOWHERE (task 0003).
_DEP_SATISFIED = _DONE | {"dropped"}
_ACTIVE_TASK = {"active"}
_TASKBED_PLANNING = {"active", "queued"}


def _snoozed_until(task, now):
    """The task's not_before if it is still in the FUTURE, else None — durable timers for free.

    Backoff and deferral had nowhere to live: a task the reaper wanted to retry in ten minutes was
    either re-served immediately into the same failure or excluded permanently by hand. One ISO
    timestamp in the ledger gives a timer that survives every crash and restart, because the
    ledger does — no scheduler to keep alive, no in-memory deadline to lose. An unparseable value
    snoozes NOTHING: a malformed timestamp must not be able to hide work forever."""
    raw = str(task.get("not_before") or "").strip()
    if not raw:
        return None
    try:
        when = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return raw if when > now else None

# Which fields on each type are idref edges, and the relation name emitted to the graph.
_EDGES = {
    "task": {
        "deps": "depends_on",
        "implements": "implements",
        "decided_by": "decided_by",
        "surfaced_by": "surfaced_by",
        "canonical_task": "canonical_task",
    },
    "adr": {"supersedes": "supersedes", "superseded_by": "superseded_by"},
    "feat": {
        "tasks": "has_task",
        "adrs": "has_adr",
        "capability": "realizes",
        "depends_on": "depends_on_feature",
    },
    "gap": {"addressed_by": "addressed_by"},
    "cap": {"realized_by": "realized_by", "depends_on": "depends_on", "consumed_by": "consumed_by"},
    "deploy": {"tasks_closed": "closed_task", "reverts": "reverts", "touches": "touches"},
    "note": {"relates_to": "relates_to"},
}


def _type_of(entity_id: str) -> str:
    parts = entity_id.split(":")
    return parts[1] if len(parts) >= 2 else "?"


def fold(events) -> dict:
    """Replay events (seq-ordered) into {id: entity}. Payloads merge last-write-wins per key."""
    entities = {}
    for ev in events:
        agg = ev.get("aggregate")
        if not agg or _type_of(agg) not in _KNOWN:
            continue  # log-only events (decision/claim/...) stay in the store, not materialized
        # Upcast BEFORE folding (read-side ledger evolution): stored bytes stay immutable, the
        # projection always sees the current payload shape (hub_core.upcast).
        payload = _upcast.apply(ev.get("type", ""), ev.get("payload") or {})
        ent = entities.get(agg, {"id": agg, "type": _type_of(agg)})
        for k, v in payload.items():
            ent[k] = v
        ent["version"] = ev.get("result_version", ent.get("version", 0))
        # COPY before stamping: a payload that carries its own provenance dict arrives ALIASED
        # into ent (the k/v merge is by reference), and stamping through the alias mutates the
        # in-memory EVENT — the served-index cross-check then reports the audit's own write-through
        # as a critical db/file divergence (observed live: index:divergence:seq1640).
        # Gen-1 ledgers carried provenance as PROSE ("shipped <sha> ..."); folding an older
        # instance's board must preserve that record, never crash on it.
        legacy_prov = ent.get("provenance")
        if isinstance(legacy_prov, dict):
            prov = dict(legacy_prov)
        elif legacy_prov:
            prov = {"note": str(legacy_prov)}
        else:
            prov = {}
        prov.setdefault("created_at", ev.get("ts"))
        prov["updated_at"] = ev.get("ts")
        if ev.get("agent_id"):
            prov["agent"] = ev["agent_id"]
        if ev.get("git_sha"):
            commits = list(prov.get("commits") or [])
            if ev["git_sha"] not in commits:
                commits.append(ev["git_sha"])
            prov["commits"] = commits
        ent["provenance"] = prov
        ent["id"] = agg
        ent.setdefault("type", _type_of(agg))
        entities[agg] = ent
    return entities


def _iter_refs(ent):
    """Yield (rel, target_id) idref edges for an entity."""
    edefs = _EDGES.get(ent.get("type"), {})
    for field, rel in edefs.items():
        val = ent.get(field)
        if isinstance(val, str):
            yield rel, val
        elif isinstance(val, list):
            for t in val:
                if isinstance(t, str):
                    yield rel, t


def derive(entities: dict, now=None) -> dict:
    """Compute the DAG (blocked), the cross-link graph, dangling idrefs, counts/phases/coverage.

    `now` is injectable so a test can prove a not_before timer both before and after it passes
    without sleeping — a durable timer asserted by a real wait is a slow test that flakes."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    by_type = {}
    for e in entities.values():
        by_type.setdefault(e.get("type", "?"), []).append(e)

    graph = []
    dangling = []
    backrefs = {}   # bidirectional cross-links (doctrine sec1/3): target -> who references it
    for e in entities.values():
        for rel, target in _iter_refs(e):
            graph.append({"from": e["id"], "rel": rel, "to": target})
            if target not in entities:
                dangling.append({"from": e["id"], "rel": rel, "to": target})
            else:
                backrefs.setdefault(target, []).append({"from": e["id"], "rel": rel})

    # The operative task bed is a projection over the immutable event history. Legacy expansions,
    # content records, duplicates, and review-only rows remain addressable without flooding DISCOVER
    # or the human Tasks tab. Missing planning_state retains legacy behaviour until a migration
    # explicitly classifies the row.
    def in_taskbed(t):
        return t.get("planning_state", "active") in _TASKBED_PLANNING

    task_bed = [t for t in by_type.get("task", []) if in_taskbed(t)]
    task_archive = [t for t in by_type.get("task", []) if not in_taskbed(t)]

    # Downstream impact is an operative planning signal, so archived dependencies must not inflate
    # urgency in DISCOVER. Historical dependency edges remain available in the full graph.
    blocks = {}
    for t in task_bed:
        for d in (t.get("deps") or []):
            blocks[d] = blocks.get(d, 0) + 1

    # task dependency DAG -> computed into a SEPARATE flags map so entities stay schema-pure.
    # urgency (the shared DISCOVER selector, importable by every stack): priority weight + 8x the
    # number of tasks this one unblocks; 0 unless it is actionable now (todo + deps met).
    _PRI = {"P0": 100, "P1": 60, "P2": 30, "P3": 10}

    # CRITICAL-PATH (HEFT upward-rank) over the OPEN dependency DAG: rank_u(t) = 1 + the longest
    # chain of open tasks whose completion transitively waits on t (unit weights — the ledger
    # carries no duration estimates). Completing the head of the longest chain first minimizes
    # makespan under parallel workers, which direct-dependent counting cannot see. Pure fold:
    # DAG edges only; a cycle (guarded elsewhere) contributes its node weight and stops.
    open_ids = {t["id"] for t in task_bed if t.get("status") not in ("done", "dropped")}
    dependents = {}
    for t in task_bed:
        if t["id"] not in open_ids:
            continue
        for d in (t.get("deps") or []):
            if d in open_ids:
                dependents.setdefault(d, []).append(t["id"])
    _rank_u = {}

    def rank_u(tid, _walking=frozenset()):
        if tid in _rank_u:
            return _rank_u[tid]
        if tid in _walking:
            return 1   # cycle: stop the walk; the dep-cycle guard owns reporting it
        kids = dependents.get(tid, ())
        r = 1 + (max((rank_u(k, _walking | {tid}) for k in kids), default=0) if kids else 0)
        _rank_u[tid] = r
        return r

    flags = {}
    for t in by_type.get("task", []):
        deps = t.get("deps") or []
        unmet = [d for d in deps if entities.get(d, {}).get("status") not in _DEP_SATISFIED]
        # A poison-blocked task (the circuit breaker opened after repeated verification fails) is
        # never unblocked — next must not re-serve it into a retry storm until an exit-0 clears it.
        snoozed = _snoozed_until(t, now)
        unblocked = (in_taskbed(t) and t.get("status") == "todo" and not unmet
                     and not t.get("poison_blocked") and not snoozed)
        bc = blocks.get(t["id"], 0)
        flags[t["id"]] = {"deps_unmet": unmet, "deps_blocked": bool(unmet), "unblocked": unblocked,
                          "blocks_count": bc, "snoozed_until": snoozed,
                          "rank_u": rank_u(t["id"]) if t["id"] in open_ids else 0,
                          "urgency": (_PRI.get((t.get("priority") or "").upper(), 20) + bc * 8) if unblocked else 0}

    # THE PLAYABLE SLICE, taken from the board's own typed edges (ADR-0008: a seat picks the task
    # that most unblocks a playable slice ahead of an equal-or-lower-priority instrument repair).
    # The slice is the EARLIEST milestone feature — the one no other feature precedes in the feat
    # depends_on chain — and the path to it is that feature's tasks (from either end of the
    # has_task/implements pairing) plus everything they transitively depend on.
    #
    # Derived, never named: hardcoding the release gate's task id would bake one instance's board
    # into the shared engine. Derived also keeps it SELECTIVE, which is the whole value — measured
    # on the live board, the terminal release gate's dependency closure covers 401 of 520 task-bed
    # rows and would order almost nothing, while the slice closure is 13. A signal that says yes to
    # three quarters of the board is not a signal.
    #
    # A feat graph with no root (every feature preceded by another — a cycle) yields the EMPTY set
    # and the order is exactly what it was: a tie-break that cannot identify the slice must not
    # guess at one.
    def _slice_path():
        feats = [f for f in by_type.get("feat", []) if f.get("status") != "removed"]
        roots = sorted((f for f in feats if not (f.get("depends_on") or [])),
                       key=lambda f: f.get("id") or "")
        if not roots:
            return frozenset()
        seed = set()
        for f in roots:
            seed.update(tid for tid in (f.get("tasks") or []) if tid in entities)
            seed.update(t["id"] for t in by_type.get("task", [])
                        if f["id"] in (t.get("implements") or []))
        seen, stack = set(seed), list(seed)
        while stack:
            for d in (entities.get(stack.pop(), {}).get("deps") or []):
                if d not in seen and d in entities:
                    seen.add(d)
                    stack.append(d)
        return frozenset(seen)

    slice_path = _slice_path()
    for t in by_type.get("task", []):
        flags[t["id"]]["on_slice_path"] = t["id"] in slice_path

    # Make the worker's real DISCOVER order visible to every projection. Priority and downstream
    # impact establish urgency; the slice signal breaks what urgency leaves tied, and numbered
    # phase and stable id resolve the rest deterministically.
    #
    # The slice term sits AFTER urgency, so it never overrides priority: ADR-0008 puts game work
    # ahead of an equal-or-lower-priority instrument repair, which means a P0 repair still outranks
    # a P2 game task. What it changes is the case the old key decided by phase number and then by
    # id — arbitrary from the ruling's point of view — where two equally urgent tasks sit side by
    # side and exactly one of them advances a playable slice.
    #
    # BETWEEN urgency and the slice sits the upward rank (critical-path scheduling): among
    # equally urgent work the longest open chain's head pulls first — the HEFT insight that a
    # short independent task can always fill a later slot, while delaying the chain head delays
    # everything behind it. Age (created_at, event-ts) breaks what phase number leaves tied.
    def pickup_key(task):
        f = flags[task["id"]]
        phase = task.get("phase") or ""
        match = re.match(r"^\s*(\d+)", phase)
        return (
            -f["urgency"],
            -f["rank_u"],
            0 if f["on_slice_path"] else 1,
            int(match.group(1)) if match else 10**9,
            (task.get("provenance") or {}).get("created_at") or "9999",
            task["id"],
        )

    pickup_queue = sorted(
        (task for task in task_bed if flags[task["id"]]["unblocked"]),
        key=pickup_key,
    )
    for rank, task in enumerate(pickup_queue, 1):
        flags[task["id"]]["pickup_rank"] = rank

    # counts
    tasks = task_bed
    counts = {}
    for st in ("todo", "active", "in_progress", "blocked", "done", "dropped", "shadow"):
        counts[st] = sum(1 for t in tasks if t.get("status") == st)
    # A dropped task leaves the completion denominator ONLY when its descope was RULED — decided_by
    # resolving to an ADR. An undecided drop stays owed, so dropping a task can never RAISE pct
    # (the metric-gaming vector scope-integrity closes); a ruled descope re-baselines legitimately.
    def _ruled_drop(t):
        return any((entities.get(d) or {}).get("type") == "adr" for d in (t.get("decided_by") or []))

    total = len([t for t in tasks if t.get("status") != "dropped" or not _ruled_drop(t)]) or 0
    counts["total"] = total
    counts["pct"] = round(100 * counts["done"] / total) if total else 0

    # phases (group tasks by phase)
    phases = {}
    for t in tasks:
        if t.get("status") == "dropped":
            continue
        ph = t.get("phase") or "Unphased"
        p = phases.setdefault(ph, {"name": ph, "done": 0, "total": 0})
        p["total"] += 1
        if t.get("status") == "done":
            p["done"] += 1
    phase_list = []
    for p in phases.values():
        p["pct"] = round(100 * p["done"] / p["total"]) if p["total"] else 0
        phase_list.append(p)
    phase_list.sort(
        key=lambda phase: (
            int(match.group(1))
            if (match := re.match(r"^\s*(\d+)", phase["name"]))
            else 10**9,
            phase["name"].casefold(),
        )
    )

    # feature coverage: shipped/partial features that cite >=1 task or adr
    feats = by_type.get("feat", [])
    cov_total = sum(1 for f in feats if f.get("status") in ("shipped", "partial"))
    cov_linked = sum(1 for f in feats if f.get("status") in ("shipped", "partial") and (f.get("tasks") or f.get("adrs")))
    coverage = round(100 * cov_linked / cov_total) if cov_total else 100

    return {
        "by_type": by_type,
        "graph": graph,
        "dangling": dangling,
        "backrefs": backrefs,
        "counts": counts,
        "phases": phase_list,
        "coverage": coverage,
        "flags": flags,
        "task_bed": task_bed,
        "task_archive": task_archive,
    }


def state(events, now=None) -> dict:
    """Full derived state: entities + derivations. The single thing projections/audit consume."""
    entities = fold(events)
    d = derive(entities, now=now)
    d["entities"] = entities
    return d
