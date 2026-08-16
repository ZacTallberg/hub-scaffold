"""Hub machine READ API. Every surface renders FROM the event-log snapshot (single source of
truth); the HTML embeds the same payload. Doctrine sections 1-2 + the hub API contract.

The LIVE endpoint emits only an authenticated cursor and a safe event envelope; the browser then
reconciles from the same canonical snapshot the HTML and the JSON API serve. Animation is never
allowed to become a second source of truth — an event says *something moved*, and the board is
re-read to find out what.
"""
import json
import time

from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from hub_core import adherence, cost, dag, failure_taxonomy, project, projections, telemetry, wip
from hub_core.canonical import content_hash

from . import delivery, hub_app

_COLLECTION = {"task": "tasks", "adr": "adrs", "feat": "feats", "gap": "gaps", "cap": "caps",
               "deploy": "deploys", "note": "notes"}

# How long a live lease may go without finishing before the board calls the worker stuck.
STALL_S = 900


@require_GET
def cursor_json(request):
    """Token-free liveness cursor: {seq, hash, ts} only — never event payloads or task titles.
    A canary or supervisor can verify the board is advancing without being handed its contents."""
    s = hub_app.store()
    try:
        return JsonResponse(s.latest_cursor())
    finally:
        s.close()


def _fmt_age(s):
    if s is None:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def _activity(events, state, limit=36):
    """Recent canonical events as a payload-safe activity rail."""
    rows = []
    entities = state.get("entities", {})
    for event in reversed(events):
        aggregate = event.get("aggregate") or ""
        entity = entities.get(aggregate, {})
        event_type = event.get("type") or ""
        if not event_type.startswith(("task.", "adr.", "gap.", "feat.", "cap.", "deploy.",
                                      "note.", "decision.")):
            continue
        row = {
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "event": event_type,
            "aggregate": aggregate,
            "entity_type": aggregate.split(":")[1] if aggregate.count(":") >= 2 else None,
            "title": entity.get("title") or entity.get("name") or aggregate.rsplit(":", 1)[-1],
            "status": entity.get("status") or entity.get("maturity"),
            "agent": event.get("agent_id"),
            "version": event.get("result_version"),
        }
        # Surface the completion PROOF: a done task carries the exit-0 receipt that granted it, so
        # a watcher sees WHAT verified the work, not merely that a row turned green.
        if entity.get("status") == "done":
            runs = entity.get("verification_run") or []
            if runs:
                r = runs[-1] if isinstance(runs, list) else runs
                row["receipt"] = {"command": r.get("command"), "exit_code": r.get("exit_code"),
                                  "ran_by": r.get("ran_by")}
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _inflight(state, stall_s=STALL_S):
    """The live fleet: open tasks currently held by a worker's lease.

    Under the receipt gate a task can go todo->done directly, so the LEASE — not a status word —
    is the true in-flight signal, and this reads the claims dir. An expired lease is not in
    flight (the task is already free to reclaim); a live lease older than stall_s is flagged so a
    stuck worker is visible rather than merely absent from the completions feed."""
    now = time.time()
    rows = []
    cdir = getattr(hub_app, "CLAIMS", None)
    if not cdir or not cdir.exists():
        return rows
    ents = state.get("entities", {})
    for p in cdir.glob("*.json"):
        try:
            lease = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # absorbs: torn/vanished lease mid-walk — the claim CAS rewrites it whole
        task = lease.get("task")
        if not task:
            continue
        ent = ents.get(task, {})
        if ent.get("status") in ("done", "dropped"):
            continue
        if lease.get("expires", 0) <= now:
            continue
        claimed = lease.get("claimed", 0)
        age = int(now - claimed) if claimed else None
        rows.append({
            "task": task, "agent": lease.get("agent"),
            "title": ent.get("title") or task.rsplit(":", 1)[-1],
            "status": ent.get("status"), "age_s": age,
            "stalled": bool(age is not None and age > stall_s),
            **_plan_progress(ent),
        })
    rows.sort(key=lambda r: (r.get("age_s") or 0), reverse=True)
    return rows


def _plan_progress(ent):
    """Per-task sub-progress from the worker's own plan checklist — so a task visibly climbs
    0->100 as the worker steps through it instead of flipping binary at done. plan_pct is None
    when the task carries no plan (nothing to show yet, which is not the same as no progress)."""
    plan = ent.get("plan") or []
    total = len(plan)
    done = sum(1 for s in plan if isinstance(s, dict) and s.get("done"))
    step = next((s.get("step") for s in plan if isinstance(s, dict) and not s.get("done")), None)
    return {"plan_done": done, "plan_total": total, "step": (str(step)[:70] if step else None),
            "plan_pct": (round(done * 100 / total) if total else None)}


def _readiness(state):
    """Pipeline health: which unblocked todos are READY to complete (they carry a
    verification_command a worker can run to a receipt) vs which NEED SPEC first. A worker that
    pulls a needs-spec task stalls trying to write one, so the queue must distinguish them."""
    flags = state.get("flags", {})
    ready, needs_spec, snoozed = [], [], []
    for t in state.get("by_type", {}).get("task", []):
        if t.get("status") != "todo":
            continue
        f = flags.get(t["id"], {})
        item = {"id": t["id"], "title": t.get("title"), "priority": t.get("priority")}
        # A task whose durable timer has not fired is not READY and not BROKEN — it is waiting,
        # and it says so with its wake time. Excluded-and-silent is how a snoozed task becomes a
        # task that appears NOWHERE, which is the failure the dangling-dep rail exists to prevent.
        if f.get("snoozed_until"):
            snoozed.append(dict(item, not_before=f["snoozed_until"]))
            continue
        if not f.get("unblocked"):
            continue
        (ready if (t.get("verification_command") or "").strip() else needs_spec).append(item)
    return {"ready": len(ready), "needs_spec": len(needs_spec), "snoozed": len(snoozed),
            "ready_top": ready[:5], "needs_spec_top": needs_spec[:5], "snoozed_top": snoozed[:5]}


# Governance amber that needs a human RULING, not code — surfaced on the attention rail so a
# silent revert, a bypassed scope edit, or an out-of-order completion is something the operator
# SEES, rather than something only the audit JSON carries.
_ATTENTION_AMBER = ("scope:changed", "task:reverted", "deps:unmet")


def _attention(state, audit, inflight, adher=None, deliv=None):
    """The consolidated 'Needs the operator' rail: every signal a human (or a spec pass) must act
    on, unioned from sources otherwise scattered across tabs and the audit JSON — a poison-blocked
    task, a stuck worker, a dep that can never be satisfied, governance amber, blocked work,
    unspecced todos, a board that has drained, and the weakest adherence dimension.

    Ranked most-urgent first, and every row can be OPENED: a row whose subject is a violation
    rather than an entity carries a `route`, because a rail that says "someone must rule on this"
    and then refuses to show what is worse than no rail."""
    entities = state.get("entities", {})
    flags = state.get("flags", {})
    tasks = state.get("by_type", {}).get("task", [])
    items = []

    def add(rank, kind, reason, tid=None, title=None, route=None):
        items.append({"rank": rank, "kind": kind, "reason": reason, "id": tid, "route": route,
                      "title": title or (tid.rsplit(":", 1)[-1] if tid else None)})

    for t in tasks:
        if t.get("poison_blocked"):
            add(1, "circuit-open",
                t.get("poison_reason") or "verification keeps failing — the circuit breaker is open",
                t["id"], t.get("title"))

    for r in (inflight or []):
        if r.get("stalled"):
            add(1, "stalled-lease",
                f"{r.get('agent')} has held the lease {_fmt_age(r.get('age_s'))} without finishing",
                r.get("task"), r.get("title"))

    # DELIVERY: a done task master never received is a worker's finished work sitting outside the
    # integration branch — the same class of loss as a stuck seat, and invisible to every
    # status-based check.
    for tid in ((deliv or {}).get("unlanded") or []):
        rec = (deliv.get("records") or {}).get(tid) or {}
        add(1, "unlanded", rec.get("state_reason") or "done on the board, not on the branch",
            tid, (entities.get(tid) or {}).get("title"))
    # An UNMEASURABLE leg is not a passing one. Without these rows a gitless container shows a
    # silent rail beside a hero that just implied every done task landed.
    for leg in ("landing", "release", "live"):
        if deliv and deliv["measured"].get(leg) is False and deliv.get("records"):
            add(3, "delivery-unmeasured-" + leg,
                "%s — %d done task(s) carry no checked %s"
                % (deliv["notes"].get(leg) or "no measurement", len(deliv["records"]), leg),
                None, "%s unmeasured here" % leg, route={"view": "overview", "focus": "delivery"})

    for t in tasks:
        if t.get("status") not in ("todo", "blocked"):
            continue
        f = flags.get(t["id"], {})
        dangling = [d for d in (f.get("deps_unmet") or []) if d not in entities]
        if dangling and not f.get("unblocked"):
            add(2, "dangling-dep", "dep(s) reference no entity: " + ", ".join(sorted(dangling)),
                t["id"], t.get("title"))

    for v in (audit.get("violations") or []):
        if str(v.get("id", "")).startswith(_ATTENTION_AMBER):
            add(3, "governance-amber", f"{v.get('id')}: {v.get('observed')}", None, v.get("id"),
                route={"view": "audit", "violation": v.get("id")})

    # ADHERENCE DRIFT is a first-class operator signal: the board can be fully green on status and
    # still have stopped describing the work. The weakest measured dimension rides the rail with
    # what it actually costs, so "82% adherent" is never the whole story the operator gets.
    if adher and adher.get("weakest") and (adher["dimensions"][adher["weakest"]]["pct"] or 100) < 90:
        d = adher["dimensions"][adher["weakest"]]
        add(3, "adherence-drift",
            f"{adher['weakest']}: {d['ok']}/{d['total']} ({d['pct']}%) — {adher['weakest_meaning']}",
            None, "board adherence: " + adher["weakest"],
            route={"view": "overview", "focus": "adherence"})

    for t in tasks:
        if t.get("status") != "blocked":
            continue
        unmet = flags.get(t["id"], {}).get("deps_unmet") or []
        add(4, "blocked", "blocked on: " + (", ".join(unmet) if unmet else "unmet deps"),
            t["id"], t.get("title"))

    for t in tasks:
        if t.get("status") != "todo" or not flags.get(t["id"], {}).get("unblocked"):
            continue
        if not (t.get("verification_command") or "").strip():
            add(5, "needs-spec",
                "unblocked but no verification_command — spec it before a worker can pull it",
                t["id"], t.get("title"))

    if _readiness(state).get("ready", 0) == 0 and not (inflight or []):
        add(0, "board-drained",
            "no ready work and no worker in flight — spec a needs-spec item or file new work")

    items.sort(key=lambda i: (i["rank"], str(i.get("id") or "")))
    return items[:32]


def _progress(events, state, deliv=None):
    """Progress the operator can watch CLIMB.

    done/total alone does not climb: a working fleet discovers new work, so the denominator grows
    as fast as completions and a busy hour can render as a flat bar. So the block also carries
    MONOTONIC signals — completed_total (every done-transition ever, which only rises) and a
    completion RATE — plus a per-bucket sparkline, so "work is happening" stays visible even when
    the ratio holds still."""
    import datetime

    # done/total/pct are READ from the fold's counts, never recomputed here: the hero and the
    # overview donut must render ONE denominator, and a second formula in this layer is exactly
    # how a page grows two numbers that disagree about the same board.
    counts = state.get("counts") or {}

    def _parse(ts):
        try:
            return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None  # absorbs: absent/malformed ts on a legacy event — skipped, never counted

    comps = sorted(t for t in (
        _parse(e.get("ts")) for e in events
        if e.get("type") == "task.transitioned" and (e.get("payload") or {}).get("status") == "done"
    ) if t is not None)
    now = datetime.datetime.now(datetime.timezone.utc)
    last_1h = sum(1 for t in comps if (now - t).total_seconds() <= 3600)
    last_24h = sum(1 for t in comps if (now - t).total_seconds() <= 86400)
    span, nb = 300, 12                       # completions per 5-minute bucket over the last hour
    spark = [0] * nb
    for t in comps:
        age = (now - t).total_seconds()
        if 0 <= age < span * nb:
            spark[nb - 1 - int(age // span)] += 1
    out = {"done": counts.get("done", 0), "total": counts.get("total", 0),
           "pct": counts.get("pct", 0), "completed_total": len(comps),
           "last_1h": last_1h, "last_24h": last_24h, "spark": spark,
           "spark_bucket_s": span}
    if deliv is not None:
        # A LANDED COUNT IS A MEASUREMENT, NOT A SUBTRACTION. Where the ancestry probe cannot run,
        # done-minus-zero-unlanded would assert that every done task is on the branch — a positive
        # claim from a question nobody asked. So the numerator is what measured TRUE, and the
        # never-measured remainder is its own number rather than hiding inside the claim.
        dc = deliv["counts"]
        measured = deliv["measured"]
        out["landing_measured"] = bool(measured.get("landing"))
        out["landing_note"] = deliv["notes"].get("landing")
        out["landed"] = dc["landed"] if measured.get("landing") else None
        out["unlanded"] = dc["unlanded"] if measured.get("landing") else None
        out["landed_unmeasured"] = dc["landed_unmeasured"] if measured.get("landing") else None
        out["deployed"] = dc["deployed"] if measured.get("release") else None
        out["live"] = dc["live"] if measured.get("live") else None
        out["live_attested"] = bool(deliv.get("live_attested"))
    return out


def _failure_modes(events, top=6):
    """What KIND of failure this fleet is having, in one vocabulary across every gate
    (hub_core.failure_taxonomy). Ranked, with the category rollup and the unclassified count
    beside it — a histogram that hides what it could not name reads as complete when it is
    merely confident."""
    hist = failure_taxonomy.histogram(events)
    ranked = sorted(hist["modes"].items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "total": hist["total"],
        "unclassified": len(hist["unclassified"]),
        "categories": dict(sorted(hist["categories"].items(), key=lambda kv: -kv[1])),
        "modes": [{"mode": m, "category": failure_taxonomy.CATEGORY[m], "count": n}
                  for m, n in ranked[:top]],
    }


def _worker_health(state, events, inflight, window=25):
    """The fleet's own health, folded from what this board can actually see: completions per
    seat, and the receipt outcomes behind them.

    Rates carry their DENOMINATOR, never a bare percentage — a fleet with two recorded receipts
    is not a 50%-failure fleet, and a board with none is not a healthy one. With nothing recorded
    the block says so rather than rendering 0%, which reads as a clean fleet instead of an
    unmeasured one."""
    tasks_by_worker = {}
    for t in state.get("by_type", {}).get("task", []):
        if t.get("status") == "done" and (t.get("provenance") or {}).get("agent"):
            agent = t["provenance"]["agent"]
            tasks_by_worker[agent] = tasks_by_worker.get(agent, 0) + 1

    recent = [ev for ev in events if wip._codes(ev)][-window:]
    codes = [c for ev in recent for c in wip._codes(ev)]
    failed = sum(1 for c in codes if c != 0)

    return {
        "window": window,
        "receipts": len(codes),
        "failed": failed,
        "fail_rate_pct": (round(100.0 * failed / len(codes), 1) if codes else None),
        "stalled_now": sum(1 for r in (inflight or []) if r.get("stalled")),
        "working_now": len(inflight or []),
        "tasks_per_worker": sorted(
            ({"worker": w, "done": c} for w, c in tasks_by_worker.items()),
            key=lambda r: (-r["done"], r["worker"]))[:8],
        "seats_with_done_work": len(tasks_by_worker),
    }


def _fleet(events, state, inflight):
    """What each worker is ACTIVELY doing — so the operator watches the FLEET, not terminals.

    Per agent: its current lease (task + age + stall), its live plan progress and current step,
    and a short trail of its recent canonical actions. Only agents holding a live lease or active
    in the last 30 minutes appear, so the view is the fleet NOW rather than everyone who ever
    touched the board."""
    import datetime

    def _parse(ts):
        try:
            return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None  # absorbs: absent/malformed ts — this row contributes no idle age

    now = datetime.datetime.now(datetime.timezone.utc)
    ents = state.get("entities", {})
    lease_by_agent = {}
    for r in (inflight or []):
        lease_by_agent.setdefault(r["agent"], r)

    IGNORE = {"agent", "unresolved-historical", None, ""}
    LABEL = {"task.created": "opened", "task.updated": "updated", "deploy.created": "deployed",
             "adr.upserted": "decided", "gap.created": "surfaced"}
    trails, last_ts = {}, {}
    for e in reversed(events):
        ag = e.get("agent_id")
        if ag in IGNORE:
            continue
        et = e.get("type", "")
        if not et.startswith(("task.", "adr.", "gap.", "deploy.")):
            continue
        ent = ents.get(e.get("aggregate", ""), {})
        if et == "task.transitioned":
            action = "completed" if ent.get("status") == "done" else "moved"
        else:
            action = LABEL.get(et, et)
        title = ent.get("title") or ent.get("name") or (e.get("aggregate", "").rsplit(":", 1)[-1])
        last_ts.setdefault(ag, e.get("ts"))
        tr = trails.setdefault(ag, [])
        if len(tr) < 5:
            tr.append({"action": action, "title": str(title)[:64], "ts": e.get("ts"),
                       "seq": e.get("seq")})

    cards = []
    for ag in set(lease_by_agent) | set(trails):
        lt = _parse(last_ts.get(ag))
        idle_s = int((now - lt).total_seconds()) if lt else None
        lease = lease_by_agent.get(ag)
        if not lease and (idle_s is None or idle_s > 1800):
            continue
        status = ("stalled" if (lease and lease.get("stalled")) else "working" if lease
                  else "recent" if (idle_s is not None and idle_s < 300) else "idle")
        cards.append({
            "agent": ag, "status": status,
            "task": lease.get("title") if lease else None,
            "task_id": lease.get("task") if lease else None,
            "age_s": lease.get("age_s") if lease else None,
            "idle_s": idle_s, "trail": trails.get(ag, []),
            "done_total": sum(1 for t in state.get("by_type", {}).get("task", [])
                              if t.get("status") == "done"
                              and (t.get("provenance") or {}).get("agent") == ag),
            **_plan_progress(ents.get(lease.get("task"), {}) if lease else {}),
        })
    rank = {"working": 0, "stalled": 0, "recent": 1, "idle": 2}
    cards.sort(key=lambda c: (rank.get(c["status"], 3), c.get("idle_s") or 0))
    return cards


def _dag_block(state, workers):
    """DAG metrics plus the min-makespan ETA for the fleet on the board right now. A worker count
    of zero still reports the floor (what ONE worker would take), so the number never reads as
    'instant' merely because nobody is currently pulling."""
    m = dag.metrics(state)
    m["workers"] = int(workers or 0)
    m["eta_tasks"] = dag.eta_tasks(state, max(1, int(workers or 1)))
    m["fleet_wide_enough"] = bool(workers and workers >= m["max_frontier_width"])
    return m


# Process-level snapshot memo keyed on the append-only head cursor (seq, hash) + the served probe
# + a fingerprint of the CLAIMS dir. The ledger is append-only, so an unchanged head means an
# unchanged fold and audit — without this, idle polls (the board's fallback, canaries, supervisors)
# re-fold and re-verify the whole ledger to recompute a result that could not have changed.
#
# The snapshot is NOT a pure function of the head, though: the inflight/fleet blocks read lease
# FILES, which change with no ledger event at all (claim, heartbeat, reap) — so the key carries a
# stat fingerprint of claims/, or a fresh claim would stay invisible until the next append.
# `served` is in the key because it feeds the build/coherence block; one probe must never be
# handed another probe's cached verdict. Races just recompute, which is benign.
_SNAP_CACHE = {"key": None, "value": None}


def _leases_fp():
    """A cheap deterministic fingerprint of the claims dir: any create/rewrite/delete of a lease
    changes a (name, mtime_ns, size) triple. One scandir — trivial next to a ledger fold."""
    import os as _os
    try:
        return tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                            for e in _os.scandir(hub_app.CLAIMS) if e.name.endswith(".json")))
    except OSError:
        return None  # absorbs: claims dir absent/entry vanished mid-scan — unfingerprintable just
                     # skips the memo and the fold recomputes


def _telemetry_fp():
    """Worker runs append OTLP lines with no ledger event, so the memo key carries the file's
    fingerprint — same reasoning as the claims fingerprint above."""
    try:
        st = (hub_app.HUB_DIR / "telemetry" / "otlp.jsonl").stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None  # absorbs: no telemetry file yet — nothing to fingerprint, memo skipped


def _snapshot(served=None):
    s = hub_app.store()
    try:
        cur = s.latest_cursor()
        # git head rides in the key because the delivery legs (rail + hero) depend on what the
        # integration branch carries, which changes with no board event at all — a landing.
        key = (cur["seq"], cur["hash"], served, _leases_fp(), _telemetry_fp(), hub_app._git_head())
        if _SNAP_CACHE["key"] == key:
            return _SNAP_CACHE["value"]
        events = s.events()
        state = project.state(events)
        audit = hub_app.run_audit(s, served=served)
        build = hub_app.build_meta(served)
        last = events[-1] if events else {}
        inflight = _inflight(state)
        adher = adherence.score(events, state, leases=inflight)
        # WHERE THE WORK ACTUALLY IS, per task, from recorded evidence. Computed ONCE and handed to
        # both the hero and the rail, so the cockpit cannot grow two answers to "is this landed"
        # — and so an unmeasurable leg reaches the operator as an honest unknown, not silent green.
        deliv = delivery.block(state, served=served)
        hub_dir = hub_app.HUB_DIR
        live = {
            "transport": "event-stream",
            "cursor": {"seq": last.get("seq", 0), "hash": last.get("hash", ""),
                       "ts": last.get("ts")},
            "activity": _activity(events, state),
            "inflight": inflight,
            "readiness": _readiness(state),
            "progress": _progress(events, state, deliv),
            # Per-task delivery: done / landed / deployed / live are four different questions, and
            # each leg reports UNMEASURED rather than false where it could not be asked.
            "delivery": deliv,
            # Is the board still being FOLLOWED and kept current — six dimensions, each with its
            # denominator (hub_core.adherence).
            "adherence": adher,
            # The dep DAG's two schedulable numbers, its SHAPE (frontier layers + the critical
            # path itself), and the honest ETA for the fleet actually present: no concurrency
            # finishes the board faster than its longest chain, and workers past the widest layer
            # have nothing to pull.
            "dag": _dag_block(state, len(inflight)),
            "fleet": _fleet(events, state, inflight),
            "worker_health": _worker_health(state, events, inflight),
            "failure_modes": _failure_modes(events),
            "attention": _attention(state, audit, inflight, adher, deliv),
            # Cost/latency aggregated FROM the OTLP GenAI lines workers emit — the standard's
            # aggregate, never a bespoke side-channel field.
            "telemetry": telemetry.read_aggregate(hub_dir),
            "cost": cost.cost_block(hub_dir, state),
            # The adaptive WIP ceiling the claim seam enforces, so the cockpit shows the fleet's
            # own concurrency budget rather than the operator guessing at it.
            "wip": wip.status(events, len(inflight)),
            "fallback_poll_ms": 2500,
        }
        snap = projections.hub_snapshot(state, build=build, audit=audit, live=live)
        if hub_app.worker_launch_enabled():
            from django.urls import reverse

            snap["worker_launch"] = {
                "enabled": True,
                "protocol": hub_app.worker_protocol(),
                "grant_endpoint": reverse("hub:launch-grant"),
            }
        else:
            snap["worker_launch"] = {"enabled": False}
        _SNAP_CACHE["value"] = (state, snap)
        _SNAP_CACHE["key"] = key
        return _SNAP_CACHE["value"]
    finally:
        s.close()


def _etag(snap):
    """A validator for the WHOLE representation, including lease-only and telemetry changes.

    The ledger head alone is insufficient: heartbeat rewrites, lease expiry, and OTLP appends all
    change the live cockpit without appending a board event. Hashing the already-built snapshot is
    bounded by the response size and makes a 304 a truthful byte-representation claim.
    """
    return '"%s"' % content_hash(snap)


def hub_json(request):
    _, snap = _snapshot(request.GET.get("served"))
    etag = _etag(snap)
    # 304 on a matching If-None-Match: an idle poll or a re-grounding pull gets an empty body
    # when nothing changed, instead of the full snapshot every time.
    resp = HttpResponse(status=304) if request.headers.get("If-None-Match") == etag \
        else JsonResponse(snap)
    resp["ETag"] = etag
    return resp


def delta_json(request):
    """The incremental read: everything that CHANGED since the client's cursor, not the whole
    board. A client holding a base snapshot patches the changed entities in place and advances its
    cursor, so a live board moves KBs per event instead of re-sending the entire fold.
    since >= head yields an empty changed set."""
    state, snap = _snapshot(request.GET.get("served"))
    target = ((snap.get("live") or {}).get("cursor") or {"seq": 0, "hash": ""})
    s = hub_app.store()
    try:
        try:
            since = max(0, int(request.GET.get("since", "0")))
        except (TypeError, ValueError):
            since = 0
        changed_aggs, seen = [], set()
        # EventStore deliberately caps one events_after query at 500. Page to the exact cursor
        # whose state `_snapshot` folded; never advance to a newer head whose entity is absent from
        # this response, and never omit event 501 while claiming the final cursor.
        page_cursor = since
        while page_cursor < target["seq"]:
            batch = [ev for ev in s.events_after(page_cursor, limit=500)
                     if ev.get("seq", 0) <= target["seq"]]
            if not batch:
                break
            for ev in batch:
                agg = ev.get("aggregate")
                if agg and agg not in seen:
                    seen.add(agg)
                    changed_aggs.append(agg)
            page_cursor = batch[-1]["seq"]
    finally:
        s.close()
    entities = state.get("entities", {})
    flags = state.get("flags", {})
    changed = [{**entities[a], **flags.get(a, {})} for a in changed_aggs if a in entities]
    audit = snap.get("audit") or {}
    live = snap.get("live") or {}
    return JsonResponse({
        "changed": changed, "removed": [],
        "cursor": {"seq": target["seq"], "hash": target["hash"]},
        "audit": {"ok": audit.get("ok")},
        # The cockpit blocks ride the delta too. Without them a live board patched entity rows
        # while its hero, fleet and attention rail stayed frozen at page-load values until the
        # next full sync — the most-watched part of the page was the last to move.
        "live": {k: live.get(k) for k in ("cursor", "progress", "adherence", "fleet", "inflight",
                                          "attention", "readiness", "activity", "dag",
                                          "worker_health", "wip", "delivery")},
        "metadata": {"since": since, "changed": len(changed)},
    })


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
                         "metadata": {"edges": len(state["graph"]),
                                      "dangling": len(state["dangling"])}})


@require_GET
def dag_graphml(request):
    """The open dependency DAG as GraphML, for any graph tool that reads the format."""
    s = hub_app.store()
    try:
        state = project.state(s.events())
    finally:
        s.close()
    return HttpResponse(dag.graphml(state), content_type="application/xml")


def audit_json(request):
    return JsonResponse(hub_app.run_audit())


def schema_json(request, type):
    p = hub_app.SCHEMA_DIR / f"{type}.schema.json"
    if not p.exists():
        raise Http404("no schema for %s" % type)
    return HttpResponse(p.read_text(encoding="utf-8"), content_type="application/json")


def _event_envelope(event):
    """Expose enough event identity to drive reconciliation, never raw payload content. The
    browser learns THAT something moved and re-reads the canonical board to learn what."""
    return {
        "seq": event.get("seq"),
        "ts": event.get("ts"),
        "event": event.get("type"),
        "aggregate": event.get("aggregate"),
        "version": event.get("result_version"),
        "agent": event.get("agent_id"),
    }


@require_GET
def live_events(request):
    """Server-Sent Events cursor with a bounded lifetime.

    One connection lasts under a minute and emits heartbeats to defeat proxy buffering, then lets
    EventSource reconnect with ``Last-Event-ID``. The browser always reconciles from a fresh
    canonical read after an event, which is what makes reconnects, missed deltas and out-of-order
    delivery safe rather than merely unlikely."""
    raw_since = request.headers.get("Last-Event-ID") or request.GET.get("since") or "0"
    try:
        requested = max(0, int(raw_since))
    except (TypeError, ValueError):
        requested = 0

    def stream():
        store = hub_app.store()
        try:
            head = store.latest_cursor()
            cursor = min(requested, head["seq"]) if requested else head["seq"]
            yield "retry: 1500\n\n"
            yield (f"id: {cursor}\nevent: ready\n"
                   f"data: {json.dumps({'seq': cursor, 'ts': head.get('ts')}, separators=(',', ':'))}\n\n")
            deadline = time.monotonic() + 52
            heartbeat_at = time.monotonic() + 8
            while time.monotonic() < deadline:
                pending = store.events_after(cursor, limit=100)
                if pending:
                    for event in pending:
                        cursor = event["seq"]
                        payload = json.dumps(_event_envelope(event), separators=(",", ":"))
                        yield f"id: {cursor}\nevent: hub\ndata: {payload}\n\n"
                    heartbeat_at = time.monotonic() + 8
                    continue
                if time.monotonic() >= heartbeat_at:
                    yield (f"id: {cursor}\nevent: heartbeat\n"
                           f"data: {json.dumps({'seq': cursor}, separators=(',', ':'))}\n\n")
                    heartbeat_at = time.monotonic() + 8
                time.sleep(0.45)
            yield f"id: {cursor}\nevent: reconnect\ndata: {{\"seq\":{cursor}}}\n\n"
        finally:
            store.close()

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["X-Accel-Buffering"] = "no"
    return response


def next_json(request):
    """DISCOVER: ranked unblocked tasks without a live lease, including stale reclaims.

    A worker's entrypoint — pull the top task, claim it, complete it with a receipt. The board
    sequences the fleet by readiness and priority; there is no dedicated leader. Non-ready todos
    are returned SEPARATELY with the reason, never silently withheld: a worker handed a stub
    stalls trying to write its own acceptance."""
    # A CLI worker is a fresh process for every pull, so the snapshot memo is cold every time.
    # DISCOVER needs only the folded task graph — building the audit, telemetry, cost and DAG
    # blocks here would put the whole cockpit on the critical path of every claim.
    s = hub_app.store()
    try:
        events = s.events()
    finally:
        s.close()
    state = project.state(events)
    wip_st = wip.status(events, len(_inflight(state)))
    if wip_st["saturated"]:
        # At saturation the rail answers 429 rather than handing out work the claim seam would
        # refuse to lease. A GET records nothing — the recorded signals come from the claim seam.
        return JsonResponse({"error": "board_saturated", **wip_st}, status=429)
    flags = state.get("flags", {})
    entities = state.get("entities", {})
    now = time.time()

    def available(t):
        lease = hub_app._read_lease(t["id"])
        held = bool(lease and lease.get("expires", 0) > now)
        # A crashed worker can leave projected status=in_progress after its lease expires. Offer
        # that task again once its dependencies are satisfied; a live lease always removes it.
        return (t.get("status") in ("todo", "in_progress")
                and not flags.get(t["id"], {}).get("deps_unmet") and not held)

    tasks = state["by_type"].get("task", [])
    cand = [t for t in tasks if available(t)]
    ready, needs_spec = [], []
    for t in cand:
        (ready if (t.get("verification_command") or "").strip() else needs_spec).append(t)
    from hub_core import schedule
    ready = schedule.order_ready(ready, flags)

    # BLOCKED-ON-DANGLING: an unmet dep referencing NO entity can never be satisfied by work
    # completing — it is a spec defect, not a wait. Without this rail such a task appears NOWHERE.
    for t in tasks:
        f = flags.get(t["id"], {})
        if f.get("unblocked") or t.get("status") not in ("todo", "blocked"):
            continue
        dangling = [d for d in (f.get("deps_unmet") or []) if d not in entities]
        if dangling:
            needs_spec.append({**t, "needs": "dangling dep(s): " + ", ".join(sorted(dangling))})

    snoozed = sorted(({**t, "not_before": flags.get(t["id"], {}).get("snoozed_until")}
                      for t in tasks
                      if t.get("status") == "todo" and flags.get(t["id"], {}).get("snoozed_until")),
                     key=lambda t: (t.get("not_before") or "", t["id"]))
    try:
        n = max(1, min(int(request.GET.get("n", "1")), 50))
    except ValueError:
        n = 1
    rows = [dict(t, available=True, stale_reclaim=t.get("status") == "in_progress")
            for t in ready[:n]]
    return JsonResponse({"data": rows, "needs_spec": needs_spec[:n], "snoozed": snoozed[:n],
                         "metadata": {"available": len(ready), "unblocked": len(ready),
                                      "ready": len(ready), "needs_spec": len(needs_spec),
                                      "snoozed": len(snoozed),
                                      "snoozed_next": snoozed[0]["not_before"] if snoozed else None,
                                      **wip_st}})
