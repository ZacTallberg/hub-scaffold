"""The pure projection fold: events -> entities -> derived hub state.

Stack-neutral, no I/O. `fold()` replays the event log into per-aggregate entities;
`derive()` computes the dependency DAG (blocked), the cross-link graph, dangling-idref
detection, and counts/phases/coverage. Everything the hub renders (state.json, .md docs,
/hub.json, the audit) is built from `state()`.
"""

# Entity types that fold into the projected state (everything else, e.g. decision/claim, is a
# log-only event kept in the event store but not materialized as an entity).
_KNOWN = {"task", "adr", "feat", "gap", "cap", "deploy", "note"}
# Statuses that count as "satisfied" for dependency purposes.
_DONE = {"done", "closed", "shipped", "accepted", "extracted", "reusable", "proven"}
_ACTIVE_TASK = {"todo", "in_progress", "blocked"}

# Which fields on each type are idref edges, and the relation name emitted to the graph.
_EDGES = {
    "task": {"deps": "depends_on", "implements": "implements", "decided_by": "decided_by", "surfaced_by": "surfaced_by"},
    "adr": {"supersedes": "supersedes", "superseded_by": "superseded_by"},
    "feat": {"tasks": "has_task", "adrs": "has_adr", "capability": "realizes"},
    "gap": {"addressed_by": "addressed_by"},
    "cap": {"realized_by": "realized_by", "depends_on": "depends_on", "consumed_by": "consumed_by"},
    "deploy": {"tasks_closed": "closed_task"},
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
        payload = ev.get("payload") or {}
        ent = entities.get(agg, {"id": agg, "type": _type_of(agg)})
        for k, v in payload.items():
            ent[k] = v
        ent["version"] = ev.get("result_version", ent.get("version", 0))
        prov = ent.get("provenance") or {}
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


def derive(entities: dict) -> dict:
    """Compute the DAG (blocked), the cross-link graph, dangling idrefs, counts/phases/coverage."""
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

    # downstream impact: how many tasks each task BLOCKS (it appears in their deps).
    blocks = {}
    for t in by_type.get("task", []):
        for d in (t.get("deps") or []):
            blocks[d] = blocks.get(d, 0) + 1

    # task dependency DAG -> computed into a SEPARATE flags map so entities stay schema-pure.
    # urgency (the shared DISCOVER selector, importable by every stack): priority weight + 8x the
    # number of tasks this one unblocks; 0 unless it is actionable now (todo + deps met).
    _PRI = {"P0": 100, "P1": 60, "P2": 30, "P3": 10}
    flags = {}
    for t in by_type.get("task", []):
        deps = t.get("deps") or []
        unmet = [d for d in deps if entities.get(d, {}).get("status") not in _DONE]
        unblocked = (t.get("status") == "todo" and not unmet)
        bc = blocks.get(t["id"], 0)
        flags[t["id"]] = {"deps_unmet": unmet, "deps_blocked": bool(unmet), "unblocked": unblocked,
                          "blocks_count": bc,
                          "urgency": (_PRI.get((t.get("priority") or "").upper(), 20) + bc * 8) if unblocked else 0}

    # counts
    tasks = by_type.get("task", [])
    counts = {}
    for st in ("todo", "in_progress", "blocked", "done", "dropped", "shadow"):
        counts[st] = sum(1 for t in tasks if t.get("status") == st)
    total = len([t for t in tasks if t.get("status") != "dropped"]) or 0
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
    }


def state(events) -> dict:
    """Full derived state: entities + derivations. The single thing projections/audit consume."""
    entities = fold(events)
    d = derive(entities)
    d["entities"] = entities
    return d
