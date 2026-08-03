"""Hub machine READ API. Every surface renders FROM the event-log snapshot (single source
of truth); the HTML embeds the same payload. Doctrine sections 1-2 + the hub API contract.
"""
import time

from django.http import Http404, HttpResponse, JsonResponse

from hub_core import projections

from . import hub_app

_COLLECTION = {"task": "tasks", "adr": "adrs", "feat": "feats", "gap": "gaps", "cap": "caps",
               "deploy": "deploys", "note": "notes"}


def _snapshot(served=None):
    s = hub_app.store()
    state = hub_app.current_state(s)
    audit = hub_app.run_audit(s, served=served)
    build = hub_app.build_meta(served)
    snap = projections.hub_snapshot(state, build=build, audit=audit)
    if hub_app.worker_launch_enabled():
        from django.urls import reverse

        snap["worker_launch"] = {
            "enabled": True,
            "protocol": hub_app.worker_protocol(),
            "grant_endpoint": reverse("hub:launch-grant"),
        }
    else:
        snap["worker_launch"] = {"enabled": False}
    return state, snap


def hub_json(request):
    _, snap = _snapshot(request.GET.get("served"))
    return JsonResponse(snap)


def type_json(request, type):
    if type not in _COLLECTION:
        raise Http404("unknown type")
    _, snap = _snapshot()
    data = snap[_COLLECTION[type]]
    return JsonResponse({"data": data, "metadata": {"type": type, "count": len(data)}})


def entity_json(request, type, local):
    if type not in _COLLECTION:
        raise Http404("unknown type")
    eid = f"{hub_app.PROJECT_KEY}:{type}:{local}"
    state, _ = _snapshot()
    ent = state["entities"].get(eid)
    if not ent:
        raise Http404("no entity %s" % eid)
    flags = state.get("flags", {}).get(eid, {})
    return JsonResponse({"data": {**ent, **flags}})


def graph_json(request):
    state, _ = _snapshot()
    return JsonResponse({"data": state["graph"], "dangling": state["dangling"],
                         "metadata": {"edges": len(state["graph"]), "dangling": len(state["dangling"])}})


def audit_json(request):
    return JsonResponse(hub_app.run_audit())


def schema_json(request, type):
    p = hub_app.SCHEMA_DIR / f"{type}.schema.json"
    if not p.exists():
        raise Http404("no schema for %s" % type)
    return HttpResponse(p.read_text(encoding="utf-8"), content_type="application/json")


def next_json(request):
    """DISCOVER: ranked unblocked tasks without a live lease, including stale reclaims."""
    state, _ = _snapshot()
    flags = state.get("flags", {})
    blockers = {}
    for e in state["graph"]:
        if e["rel"] == "depends_on":
            blockers[e["to"]] = blockers.get(e["to"], 0) + 1

    def urgency(t):
        pri = {"P0": 40, "P1": 20, "P2": 10, "P3": 5}.get(t.get("priority"), 8)
        return pri + blockers.get(t["id"], 0) * 8

    now = time.time()

    def available(t):
        task_flags = flags.get(t["id"], {})
        lease = hub_app._read_lease(t["id"])
        held = bool(lease and lease.get("expires", 0) > now)
        # A crashed worker can leave projected status=in_progress after its lease expires. Offer
        # that task again once its dependencies are satisfied; a live lease always removes it.
        return (t.get("status") in ("todo", "in_progress") and
                not task_flags.get("deps_unmet") and not held)

    cand = [t for t in state["by_type"].get("task", []) if available(t)]
    cand.sort(key=urgency, reverse=True)
    try:
        n = max(1, min(int(request.GET.get("n", "1")), 50))
    except ValueError:
        n = 1
    rows = [dict(t, available=True, stale_reclaim=t.get("status") == "in_progress")
            for t in cand[:n]]
    # Keep the historical `unblocked` count as a compatibility alias for existing consumers.
    return JsonResponse({"data": rows, "metadata": {"available": len(cand), "unblocked": len(cand)}})
