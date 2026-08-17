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
_KNOWN = {"task", "adr", "feat", "gap", "cap", "deploy", "note"}
# Statuses that count as "satisfied" for dependency purposes.
_DONE = {"done", "closed", "shipped", "accepted", "extracted", "reusable", "proven"}
# Dependency SATISFACTION additionally treats a dropped dep as terminally resolved, matching the
# deps-before-done write gate (done, dropped) — a dropped dep blocks nothing forever. Without this
# a todo task whose only unmet-status dep was dropped is never unblocked, never listed by
# /hub/next.json, and not on the dangling rail: it appears nowhere.
_DEP_SATISFIED = _DONE | {"dropped"}


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
        "repair_task": "repaired_by",
        "repair_for": "repairs",
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


def _advance(entities, events):
    """Apply only ``events`` onto an existing materialized entity map."""
    for ev in events:
        agg = ev.get("aggregate")
        if not agg or _type_of(agg) not in _KNOWN:
            continue  # log-only events (decision/claim/...) stay in the store, not materialized
        # Upcast BEFORE folding (read-side ledger evolution): stored bytes stay immutable, the
        # projection always sees the current payload shape (hub_core.upcast).
        payload = _upcast.apply(ev.get("type", ""), ev.get("payload") or {})
        # Copy the one touched aggregate. Incremental readers can therefore advance a cached
        # projection without mutating the version still visible to another request.
        ent = dict(entities.get(agg, {"id": agg, "type": _type_of(agg)}))
        for k, v in payload.items():
            ent[k] = v
        ent["version"] = ev.get("result_version", ent.get("version", 0))
        # COPY before stamping: a payload that carries its own provenance dict arrives ALIASED
        # into ent (the k/v merge is by reference), and stamping through the alias mutates the
        # in-memory EVENT — the served-index cross-check then reports the audit's own write-through
        # as a critical db/file divergence.
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


def fold(events) -> dict:
    """Replay events (seq-ordered) into {id: entity}. Payloads merge last-write-wins per key."""
    return _advance({}, events)


def advance(entities, events) -> dict:
    """Advance a materialized fold from only its new, contiguous events.

    The top-level map and each touched aggregate are copied; untouched entities are safely shared.
    This is the live read model's hot path, so an ordinary mutation never replays prior history.
    """
    return _advance(dict(entities or {}), events)


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

    task_bed = list(by_type.get("task", []))

    # Downstream impact is an operative planning signal. Terminal dependencies do not inflate
    # urgency in DISCOVER; their historical edges remain available in the full graph.
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
        unblocked = (t.get("status") == "todo" and not unmet
                     and not t.get("poison_blocked") and not snoozed)
        bc = blocks.get(t["id"], 0)
        flags[t["id"]] = {"deps_unmet": unmet, "deps_blocked": bool(unmet), "unblocked": unblocked,
                          "blocks_count": bc, "snoozed_until": snoozed,
                          "rank_u": rank_u(t["id"]) if t["id"] in open_ids else 0,
                          "urgency": (_PRI.get((t.get("priority") or "").upper(), 20) + bc * 8) if unblocked else 0}

    # Make the worker's real DISCOVER order visible to every projection. Priority and downstream
    # impact establish urgency. Between urgency and ordinary deterministic tie-breakers sits the
    # upward rank (critical-path scheduling): among
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
    for st in ("todo", "in_progress", "blocked", "done", "dropped", "shadow"):
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
    }


def state(events, now=None) -> dict:
    """Full derived state: entities + derivations. The single thing projections/audit consume."""
    entities = fold(events)
    d = derive(entities, now=now)
    d["entities"] = entities
    return d
