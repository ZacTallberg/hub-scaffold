"""Hub machine READ API. Every surface renders FROM the event-log snapshot (single source of
truth); the HTML embeds the same payload. Doctrine sections 1-2 + the hub API contract.

The LIVE endpoint carries canonical cumulative patches directly from the same materialized event
projection the HTML and JSON API serve. Signals are wake-ups, never a second source of truth;
reconnect cursor reconciliation closes the only interval in which a client could miss one.
"""
import asyncio
import json
import os
import random
import re
import threading
import time

from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from hub_core import (adherence, cost, dag, errorlog, failure_taxonomy, flow,
                      inbox as inbox_core, project, projections, telemetry, upcast, wip)
from hub_core.canonical import content_hash

from . import delivery, hub_app, realtime

_COLLECTION = {"task": "tasks", "run": "runs", "adr": "adrs", "feat": "feats", "gap": "gaps", "cap": "caps",
               "deploy": "deploys", "note": "notes", "directive": "directives", "ack": "acks"}


def _epoch(value):
    """ISO-8601 -> epoch seconds, or None when it cannot be read as a plausible instant.

    RANGE-checked, not just format-checked, at this one choke point: external systems emit
    sentinel dates (year-0001 minimums, year-9999 maximums) as routine data, and a sentinel
    parses perfectly and then renders as an age of two thousand years — or overflows a
    platform time call and 500s the endpoint forever. Anything outside a plausible window
    maps to "absent", which every caller already handles."""
    import datetime as _dtm
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dtm.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dtm.timezone.utc)
    if not (1990 <= dt.year <= 2100):
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None

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
    """Recent canonical events rendered at event time, never rewritten by current state."""
    rows, event_entities = [], {}
    current = state.get("entities", {})
    for event in events:
        aggregate = event.get("aggregate") or ""
        event_type = event.get("type") or ""
        if not event_type.startswith(("task.", "adr.", "gap.", "feat.", "cap.", "deploy.",
                                      "note.", "decision.")):
            continue
        payload = upcast.apply(event_type, event.get("payload") or {})
        entity = dict(event_entities.get(aggregate, {}))
        entity.update(payload)
        event_entities[aggregate] = entity
        row = {
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "event": event_type,
            "aggregate": aggregate,
            "entity_type": aggregate.split(":")[1] if aggregate.count(":") >= 2 else None,
            "title": entity.get("title") or entity.get("name") or aggregate.rsplit(":", 1)[-1],
            "status": entity.get("status") or entity.get("maturity"),
            "current_status": (current.get(aggregate) or {}).get("status")
                              or (current.get(aggregate) or {}).get("maturity"),
            "agent": event.get("agent_id"),
            "version": event.get("result_version"),
        }
        # Surface an OPTIONAL critical-probe receipt when one exists. Ordinary done work stands on
        # the successful real operation and emits no synthetic receipt merely to turn the row green.
        if event_type == "task.transitioned" and payload.get("status") == "done":
            runs = payload.get("verification_run") or []
            if runs:
                r = runs[-1] if isinstance(runs, list) else runs
                row["receipt"] = {"command": r.get("command"), "exit_code": r.get("exit_code"),
                                  "ran_by": r.get("ran_by")}
        rows.append(row)
    return list(reversed(rows[-limit:]))


def _inflight(state, stall_s=STALL_S):
    """The live fleet: open tasks currently held by a worker's lease.

    Under the throughput-first model a task can go todo->done directly, so the LEASE — not a status word —
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
        heartbeat = lease.get("last_heartbeat", claimed)
        age = int(now - claimed) if claimed else None
        heartbeat_age = int(now - heartbeat) if heartbeat else None
        rows.append({
            "task": task, "agent": lease.get("agent"),
            "title": ent.get("title") or task.rsplit(":", 1)[-1],
            "status": ent.get("status"), "age_s": age,
            "heartbeat_age_s": heartbeat_age,
            "expires_in_s": max(0, int(lease.get("expires", 0) - now)),
            "last_heartbeat": heartbeat,
            "stalled": bool(heartbeat_age is not None and heartbeat_age > stall_s),
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
    # The last checkpoint note is the CONTEXT that turns "working on X" into "working on X,
    # last did Y" — the fact a peer needs to decide whether to coordinate, wait, or move on.
    noted = [s for s in plan if isinstance(s, dict) and s.get("note")]
    return {"plan_done": done, "plan_total": total, "step": (str(step)[:70] if step else None),
            "plan_pct": (round(done * 100 / total) if total else None),
            "last_note": (str(noted[-1].get("note"))[:90] if noted else None)}


# Governance amber that needs a human RULING, not code — surfaced on the attention rail so a
# silent revert, a bypassed scope edit, or an out-of-order completion is something the operator
# SEES, rather than something only the audit JSON carries.
def _readiness(state, lease_rows=None):
    """Pipeline health from the same classifier the claim seam enforces."""
    flags = state.get("flags", {})
    lease_map = {row.get("task"): row for row in
                 (hub_app.leases() if lease_rows is None else lease_rows)}
    groups = {name: [] for name in ("ready", "needs_spec", "snoozed", "blocked")}
    for task in state.get("by_type", {}).get("task", []):
        cls = flow.classify(task, flags.get(task["id"], {}), lease_map.get(task["id"]))
        item = {"id": task["id"], "title": task.get("title"),
                "priority": task.get("priority"), "flow_state": cls["state"],
                "reason": cls["reason"], "stale_reclaim": cls["stale_reclaim"]}
        if cls["available"]:
            groups["ready"].append(item)
        elif cls["state"] == "needs_spec":
            groups["needs_spec"].append(item)
        elif cls["state"] == "snoozed":
            item["not_before"] = flags.get(task["id"], {}).get("snoozed_until")
            groups["snoozed"].append(item)
        elif cls["state"] in ("blocked", "poison"):
            groups["blocked"].append(item)
    return {**{name: len(rows) for name, rows in groups.items()},
            **{name + "_top": rows[:5] for name, rows in groups.items()}}


_ATTENTION_AMBER = ("scope:changed", "task:reverted", "deps:unmet")


# The board's copy about its operational error stream promises only critical and high
# problems — warnings, foreign-scanner traffic and transient transport blips never reach it.
# The promise is enforced HERE, at read, in the one predicate every consumer shares: a bar
# kept only in the renderer lies to every machine reader. Applied at READ, never at write,
# so improving the predicate reclassifies the whole retained window retroactively.
_BLIP = re.compile(r"^(HTTP 5|HTTP 0|Failed to fetch|NetworkError|Load failed|"
                   r"Live stream unavailable)", re.I)


def _error_bar(row):
    """(on_bar, reason). One shared predicate for the board, the JSON API and the rail."""
    if row.get("external"):
        return False, "foreign client, not this system"
    sev = str(row.get("severity") or "error").lower()
    if sev not in ("critical", "error"):
        return False, "severity %s" % sev
    # A tab that briefly could not reach the hub is the board losing its connection, not a
    # defect anybody can be asked to fix.
    if str(row.get("source") or "").startswith("browser.") and _BLIP.match(str(row.get("message") or "")):
        return False, "transport blip that recovered"
    return True, ""


def _errors_block():
    """The operational error stream, bar-annotated, plus the SHAPE a reader actually needs:
    is it getting worse, which source is responsible, and is any of it even ours."""
    rows, metadata = errorlog.read(hub_app.HUB_DIR)
    now = time.time()
    buckets = [0] * 24
    severities = {"critical": 0, "error": 0, "warning": 0}
    sources, external, on_bar_n, unclaimed = {}, 0, 0, []
    for row in rows:
        ok, why = _error_bar(row)
        row["bar"] = "on" if ok else "deferred"
        if not ok:
            row["defer_reason"] = why
        else:
            on_bar_n += 1
            if not row.get("acked"):
                unclaimed.append(row)
        try:
            age_h = int((now - float(row.get("epoch") or 0)) // 3600)
        except (TypeError, ValueError):
            age_h = 999
        if 0 <= age_h < 24:
            buckets[23 - age_h] += 1
        sev = row.get("severity") or "error"
        if sev in severities:
            severities[sev] += 1
        if row.get("external"):
            external += 1
        src = str(row.get("source") or "unknown")
        entry = sources.setdefault(src, {"source": src, "count": 0,
                                         "external": bool(row.get("external")), "newest": ""})
        # occurrences_since_last is what the write-time throttle collapsed, so the true
        # weight of a repeating error is not the number of rows it left behind.
        # occurrences_folded counts what the throttle has folded SINCE the newest row was
        # written; the larger of the two is the honest weight (summing would double-count the
        # occurrence the row itself is).
        entry["count"] += max(1 + int(row.get("occurrences_since_last") or 0),
                              int(row.get("occurrences_folded") or 0))
        if str(row.get("ts") or "") > entry["newest"]:
            entry["newest"] = str(row.get("ts") or "")
    recent = sum(buckets[-6:])
    earlier = sum(buckets[:-6]) or 0
    if not rows:
        trend = "quiet"
    elif recent == 0:
        trend = "cooling"
    elif earlier and recent > (earlier / 3.0) * 1.6:
        trend = "rising"
    elif earlier and recent < (earlier / 3.0) * 0.5:
        trend = "cooling"
    else:
        trend = "steady"
    metadata.update({
        "hourly": buckets, "severities": severities,
        "top_sources": sorted(sources.values(), key=lambda s: -s["count"])[:6],
        "external_rows": external, "app_rows": max(0, len(rows) - external),
        "trend": trend, "last_24h": sum(buckets),
        "on_board": on_bar_n, "unclaimed": len(unclaimed),
        # "Is this everything?" is the one question a list of errors can never answer about
        # itself, and the one a reader must have answered before an empty card may be read
        # as good news.
        "coverage": errorlog.coverage(rows),
    })
    return rows, metadata, unclaimed


def _attention(state, audit, inflight, adher=None, deliv=None, asks=None, error_unclaimed=None):
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
        if t.get("poison_blocked") and t.get("operator_attention", True):
            add(1, "circuit-open",
                t.get("poison_reason") or "verification keeps failing — the circuit breaker is open",
                t["id"], t.get("title"))
        elif t.get("operator_attention") and t.get("last_failure"):
            add(1, "consequential-failure",
                (t.get("last_failure") or {}).get("note") or "failure requires operator authority",
                t["id"], t.get("title"))

    for r in (inflight or []):
        if r.get("stalled"):
            add(1, "stalled-lease",
                f"{r.get('agent')} has held the lease {_fmt_age(r.get('age_s'))} without finishing",
                r.get("task"), r.get("title"))

    # OPEN QUESTIONS are operator work: an ask nobody sees is a worker blocked on one fact,
    # and the cost of a question compounds for as long as it sits.
    for q in (asks or []):
        add(1, "open-question",
            f"{q.get('from')} asks: {str(q.get('title') or '')[:120]}", q.get("id"),
            q.get("title"), route={"view": "overview", "focus": "asks"})

    # OVERDUE directives: `deadline` is documented as "surfaced, never enforced" — this is
    # the surfacing. An active directive past its deadline with targets still unacked is an
    # instruction the fleet has NOT absorbed on the timeline its author declared.
    now_epoch = time.time()
    acked_by_dir = {}
    for e in entities.values():
        if e.get("type") == "ack":
            acked_by_dir.setdefault(e.get("directive"), set()).add(
                str(e.get("agent") or "").lower())
    for e in entities.values():
        if e.get("type") != "directive" or e.get("status") != "active":
            continue
        due = _epoch(e.get("deadline"))
        if not due or due >= now_epoch:
            continue
        targets = [str(t).lower() for t in (e.get("targets") or []) if str(t).strip()]
        missing = [t for t in targets if t != "all" and t not in acked_by_dir.get(e["id"], set())]
        add(2, "directive-overdue",
            "past its deadline" + ((" — unacked: " + ", ".join(sorted(missing))) if missing
                                   else " and 'all'-targeted (no closed roster to check off)"),
            e["id"], e.get("title"))

    # UNCLAIMED operational errors that clear the bar. The same set a human sees — the rail
    # must never hand a machine a longer list than the person who would be asked about it.
    for r in (error_unclaimed or [])[:5]:
        where = (r.get("context") or {}).get("app") or r.get("origin_app") or r.get("origin") or ""
        add(2, "error-unclaimed",
            (f"[{where}] " if where else "") + str(r.get("message") or "")[:140],
            None, str(r.get("source") or "error"),
            route={"view": "overview", "focus": "errors"})

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
        if t.get("work_kind") in ("product", "verification") and not str(
                t.get("acceptance") or "").strip():
            add(5, "needs-spec",
                "unblocked executable work has no concrete acceptance — spec it before pull",
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
    seat, plus outcomes for the rare critical probes workers explicitly recorded.

    Probe rates carry their DENOMINATOR, never a bare percentage. No receipt is a normal state when
    no critical probe was declared; it says nothing negative about ordinary completed work."""
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
        payload = upcast.apply(et, e.get("payload") or {})
        if et == "task.transitioned":
            action = "completed" if payload.get("status") == "done" else \
                     "started" if payload.get("status") == "in_progress" else "moved"
        else:
            action = LABEL.get(et, et)
        title = payload.get("title") or payload.get("name") or ent.get("title") \
                or ent.get("name") or (e.get("aggregate", "").rsplit(":", 1)[-1])
        last_ts.setdefault(ag, e.get("ts"))
        tr = trails.setdefault(ag, [])
        if len(tr) < 5:
            tr.append({"action": action, "title": str(title)[:64], "ts": e.get("ts"),
                       "seq": e.get("seq")})

    # Live consoles per agent, from observed presence: an agent working WITHOUT a formal
    # claim must not render as idle — that is the exact case that makes a busy fleet look
    # asleep. A console's focus becomes the card's "on" line when no lease exists, and the
    # per-console rows are the surface that stops two sessions from unknowingly working the
    # same thing.
    sessions_by_agent = {}
    try:
        for s in hub_app.live_sessions():
            sessions_by_agent.setdefault(s["agent"], []).append(s)
    except Exception:                                        # noqa: BLE001 - never 500 the board
        sessions_by_agent = {}

    cards = []
    for ag in set(lease_by_agent) | set(trails) | set(sessions_by_agent):
        lt = _parse(last_ts.get(ag))
        idle_s = int((now - lt).total_seconds()) if lt else None
        lease = lease_by_agent.get(ag)
        sessions = sessions_by_agent.get(ag, [])
        if not lease and not sessions and (idle_s is None or idle_s > 1800):
            continue
        newest_session_age = sessions[0].get("age_s") if sessions else None
        if lease and lease.get("stalled"):
            status = "stalled"
        elif lease:
            status = "working"
        elif newest_session_age is not None and newest_session_age < 300:
            # A live console, just not on a board task. The age check is an explicit
            # None-test: `(age or huge) < 300` sent a console that pinged ZERO seconds ago
            # — the most active possible — to idle, because 0 is falsy.
            status = "active"
        elif idle_s is not None and idle_s < 300:
            status = "recent"
        else:
            status = "idle"
        cards.append({
            "agent": ag, "status": status,
            "machine": (sessions[0].get("machine") if sessions else "") or "",
            "task": lease.get("title") if lease else None,
            "task_id": lease.get("task") if lease else None,
            "focus": (sessions[0].get("focus") if (not lease and sessions) else "") or "",
            "age_s": lease.get("age_s") if lease else None,
            "idle_s": idle_s, "trail": trails.get(ag, []),
            "sessions": sessions[:6],
            "done_total": sum(1 for e in events if e.get("agent_id") == ag
                              and e.get("type") == "task.transitioned"
                              and (e.get("payload") or {}).get("status") == "done"),
            **_plan_progress(ents.get(lease.get("task"), {}) if lease else {}),
        })
    rank = {"working": 0, "stalled": 0, "active": 1, "recent": 1, "idle": 2}
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
# unchanged fold and audit — without this, reconnect recovery and supervisors would re-fold and
# re-verify the whole ledger to recompute a result that could not have changed.
#
# The snapshot is NOT a pure function of the head, though: the inflight/fleet blocks read lease
# FILES, which change with no ledger event at all (claim, heartbeat, reap) — so the key carries a
# stat fingerprint of claims/, or a fresh claim would stay invisible until the next append.
# `served` is in the key because it feeds the build/coherence block; one probe must never be
# handed another probe's cached verdict. Races just recompute, which is benign.
_STATE_CACHE = {"seq": None, "hash": None, "events": None, "state": None}
_STATE_LOCK = threading.RLock()
_SNAP_CACHE = {"key": None, "value": None}
_AUDIT_CACHE = {"key": None, "value": None}
_DELIVERY_CACHE = {"values": {}, "building": set()}
_DELIVERY_LOCK = threading.RLock()


def _leases_fp():
    """A cheap deterministic fingerprint of the claims dir: any create/rewrite/delete of a lease
    changes a (name, mtime_ns, size) triple. One scandir — trivial next to a ledger fold."""
    import os as _os
    try:
        now = time.time()
        stats = {e.name: (e.stat().st_mtime_ns, e.stat().st_size)
                 for e in _os.scandir(hub_app.CLAIMS) if e.name.endswith(".json")}
        semantic = []
        for lease in hub_app.leases(now=now, include_expired=True):
            name = hub_app._claim_path(lease.get("task", "")).name
            heartbeat = lease.get("last_heartbeat", lease.get("claimed", 0))
            semantic.append((name, lease.get("expires", 0) > now,
                             bool(heartbeat and now - heartbeat > STALL_S), stats.get(name)))
        return tuple(sorted(semantic))
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


def _events_through(store, after, target):
    rows, cursor = [], after
    while cursor < target:
        batch = [event for event in store.events_after(cursor, limit=500)
                 if event.get("seq", 0) <= target]
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1]["seq"]
    return rows


def _projected(store, cursor):
    """Return ``(events, state)`` at cursor from the materialized append-only read model."""
    seq, head_hash = cursor.get("seq", 0), cursor.get("hash", "")
    with _STATE_LOCK:
        cached_seq = _STATE_CACHE["seq"]
        cached_hash = _STATE_CACHE["hash"]
        incremental = cached_seq is not None and seq > cached_seq
        pending = _events_through(store, cached_seq, seq) if incremental else []
        contiguous = bool(
            incremental and pending and pending[0].get("seq") == cached_seq + 1
            and pending[0].get("prev_hash", "") == cached_hash
            and pending[-1].get("seq") == seq and pending[-1].get("hash", "") == head_hash
        )

        if cached_seq is None or seq < cached_seq or (seq == cached_seq and head_hash != cached_hash) \
                or (incremental and not contiguous):
            events = _events_through(store, 0, seq)
            state = project.state(events)
        elif incremental:
            events = list(_STATE_CACHE["events"] or ()) + pending
            entities = project.advance(
                (_STATE_CACHE["state"] or {}).get("entities", {}), pending
            )
            state = project.derive(entities)
            state["entities"] = entities
        else:
            # Time may cross a not_before boundary without a write. Re-derive over held entities;
            # no event history or audit is touched.
            entities = dict((_STATE_CACHE["state"] or {}).get("entities", {}))
            state = project.derive(entities)
            state["entities"] = entities
            events = _STATE_CACHE["events"] or []

        _STATE_CACHE.update({"seq": seq, "hash": head_hash, "events": events, "state": state})
        return events, state


def _cache_delivery(key, value):
    with _DELIVERY_LOCK:
        _DELIVERY_CACHE["values"][key] = value
        if len(_DELIVERY_CACHE["values"]) > 12:
            _DELIVERY_CACHE["values"].pop(next(iter(_DELIVERY_CACHE["values"])), None)


def _delivery_fast(state, cursor, served):
    # The artifact stamp is production's direct running identity. Include it in the cache key so
    # a new image can never inherit a delivery projection materialized by an older one, even when
    # both point at the same durable ledger cursor.
    artifact_sha = hub_app._running_sha()
    identity_key = (served, artifact_sha)
    key = (cursor.get("seq", 0), cursor.get("hash", ""), *identity_key)
    with _DELIVERY_LOCK:
        exact = _DELIVERY_CACHE["values"].get(key)
        if exact is not None:
            return exact, True
        should_build = key not in _DELIVERY_CACHE["building"]
        if should_build:
            _DELIVERY_CACHE["building"].add(key)
    # Exact sha/served_sha/tasks_closed proof is pure entity projection and belongs on the direct
    # path. Git ancestry is legacy/source-checkout enrichment only.
    provisional = delivery.direct_block(state, served=served)
    if not delivery.repository_available():
        _cache_delivery(key, provisional)
        with _DELIVERY_LOCK:
            _DELIVERY_CACHE["building"].discard(key)
        return provisional, True
    if should_build:
        # Ancestry measurement is useful but can spawn many git commands. Keep it completely off
        # the delta/SSE response path; its own completion wakes the cockpit with a second patch.
        def materialize():
            try:
                measured = delivery.block(state, served=served)
                _cache_delivery(key, measured)
                realtime.publish(hub_app.HUB_DIR,
                                 {"kind": "projection.delivery", "cursor": cursor.get("seq", 0)},
                                 channel=hub_app.PROJECT_KEY)
            finally:
                with _DELIVERY_LOCK:
                    _DELIVERY_CACHE["building"].discard(key)

        threading.Thread(target=materialize, name="hub-delivery-projection", daemon=True).start()
    return provisional, False


def _live_side_blocks(state):
    """The addressed plane, the operational stream, and the live consoles — computed once
    per live payload so the attention rail, the cockpit cards, and the JSON endpoints all
    read ONE answer to "what is open" and "what is broken". Fail-soft: a sidecar problem
    must never take the board down."""
    asks = inbox_core.question_items(state)
    error_rows, error_meta, error_unclaimed = _errors_block()
    try:
        sessions_live = hub_app.live_sessions()
    except Exception:                                        # noqa: BLE001
        sessions_live = []
    return asks, error_rows, error_meta, error_unclaimed, sessions_live


def _live_blocks(events, state, audit, deliv, cursor):
    last = events[-1] if events else {}
    inflight = _inflight(state)
    # Re-arm semantic timers after a process restart. Timers wake exactly at stall/expiry; they do
    # not sample the claims directory on an interval.
    for lease in hub_app.leases():
        hub_app._schedule_lease_truth(lease)
    adher = adherence.score(events, state, leases=inflight)
    hub_dir = hub_app.HUB_DIR
    asks, error_rows, error_meta, error_unclaimed, sessions_live = _live_side_blocks(state)
    return {
        "transport": "event-stream",
        "realtime": hub_app.realtime_info(),
        "cursor": {"seq": cursor.get("seq", last.get("seq", 0)),
                   "hash": cursor.get("hash", last.get("hash", "")), "ts": last.get("ts")},
        "activity": _activity(events, state),
        "inflight": inflight,
        "readiness": _readiness(state, inflight),
        "progress": _progress(events, state, deliv),
        "delivery": deliv,
        "adherence": adher,
        "dag": _dag_block(state, len(inflight)),
        "fleet": _fleet(events, state, inflight),
        "worker_health": _worker_health(state, events, inflight),
        "failure_modes": _failure_modes(events),
        # OPEN QUESTIONS: who is blocked on one fact, and for how long — the whole ask →
        # deliver → answer → ack loop closes through the board, so the board shows it.
        "asks": asks[:12],
        "asks_open": len(asks),
        # THE OPERATIONAL ERROR STREAM, bar-annotated, with the shape a reader needs
        # (trend, sources, coverage) — the failures the ledger audit cannot see.
        "errors": error_rows[:40],
        "error_log": error_meta,
        # EVERY LIVE CONSOLE, flat: the surface that stops two sessions from unknowingly
        # working the same thing. The per-agent fleet cards roll these up.
        "sessions_live": sessions_live[:12],
        "attention": _attention(state, audit, inflight, adher, deliv,
                                asks=asks, error_unclaimed=error_unclaimed),
        "telemetry": telemetry.read_aggregate(hub_dir),
        "cost": cost.cost_block(hub_dir, state),
        "wip": hub_app.wip_status(len(inflight)),
    }


def _snapshot(served=None):
    s = hub_app.store()
    try:
        cur = s.latest_cursor()
        # git head rides in the key because the delivery legs (rail + hero) depend on what the
        # integration branch carries, which changes with no board event at all — a landing.
        # Several non-stream callers still cross time-only boundaries (not_before and rolling
        # throughput windows). The push lane uses exact semantic lease timers; this bucket only
        # prevents a standalone full-snapshot caller from retaining a stale time-derived view.
        git_head = hub_app._git_head()
        # Presence and the error sidecars change with NO ledger event; a memo blind to them
        # serves a live board whose fleet strip and error card are frozen at whatever the
        # last append happened to capture.
        key = (cur["seq"], cur["hash"], served, _leases_fp(), _telemetry_fp(),
               hub_app.presence_stamp(), errorlog.stamp(hub_app.HUB_DIR),
               git_head, int(time.time() // 5))
        if _SNAP_CACHE["key"] == key:
            return _SNAP_CACHE["value"]
        events, state = _projected(s, cur)
        # Realtime lease/telemetry refreshes must not repeatedly pay for a repository audit whose
        # inputs did not change. Structural audit truth changes with the ledger or build identity;
        # cache on exactly those inputs and keep the five-second live cockpit refresh lightweight.
        audit_key = (cur["seq"], cur["hash"], served, git_head)
        if _AUDIT_CACHE["key"] == audit_key:
            audit = _AUDIT_CACHE["value"]
        else:
            audit = hub_app.run_audit(s, served=served)
            _AUDIT_CACHE["key"], _AUDIT_CACHE["value"] = audit_key, audit
        build = hub_app.build_meta(served, state=state)
        last = events[-1] if events else {}
        inflight = _inflight(state)
        adher = adherence.score(events, state, leases=inflight)
        # Repository ancestry can require many Git calls. First paint uses the truthful cached or
        # provisional view; the completed materialization publishes its own live patch.
        # Until then, an unmeasured leg stays an honest unknown rather than silent green.
        deliv, _ = _delivery_fast(state, cur, served)
        hub_dir = hub_app.HUB_DIR
        side_asks, side_error_rows, side_error_meta, side_unclaimed, side_sessions = \
            _live_side_blocks(state)
        live = {
            "transport": "event-stream",
            "realtime": hub_app.realtime_info(),
            "cursor": {"seq": last.get("seq", 0), "hash": last.get("hash", ""),
                       "ts": last.get("ts")},
            "activity": _activity(events, state),
            "inflight": inflight,
            "readiness": _readiness(state, inflight),
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
            # The addressed plane, the operational stream, and the live consoles — the same
            # blocks the push patches carry (_live_blocks), so the first paint and every
            # later patch read one truth.
            "asks": side_asks[:12],
            "asks_open": len(side_asks),
            "errors": side_error_rows[:40],
            "error_log": side_error_meta,
            "sessions_live": side_sessions[:12],
            "attention": _attention(state, audit, inflight, adher, deliv,
                                    asks=side_asks, error_unclaimed=side_unclaimed),
            # Cost/latency aggregated FROM the OTLP GenAI lines workers emit — the standard's
            # aggregate, never a bespoke side-channel field.
            "telemetry": telemetry.read_aggregate(hub_dir),
            "cost": cost.cost_block(hub_dir, state),
            # The adaptive WIP ceiling the claim seam enforces, so the cockpit shows the fleet's
            # own concurrency budget rather than the operator guessing at it.
            "wip": hub_app.wip_status(len(inflight)),
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


# Clock-derived fields that move on every rebuild while nothing on the board changed. They are
# kept OUT of the validator (the client ticks ages itself between reads), and the tag carries a
# five-minute bucket instead, so a 304 can never pin an age more than one bucket stale.
_VOLATILE_KEYS = frozenset({"generated_at", "age_s", "idle_s"})
_ETAG_BUCKET_S = 300


def _without_volatile(value):
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def _etag(snap):
    """A WEAK validator for the served representation.

    Hashing the snapshot verbatim made the tag useless: `generated_at` and every age/idle counter
    change on each five-second rebuild, so two back-to-back reads of an unchanged board carried
    different tags and a conditional GET never answered 304 (measured on the example board: 746 ->
    754 in `age_s`, 377 -> 385 in `idle_s`, a new `generated_at`, a new tag). Those clock fields
    are stripped; everything that describes the board -- rows, leases, telemetry, audit, build --
    still feeds the hash, plus a coarse time bucket. Weak (`W/`) because a 304 claims semantic,
    not byte, equivalence: the body you already hold differs only in ages you are ticking locally.
    """
    return 'W/"%s"' % content_hash({"snap": _without_volatile(snap),
                                     "bucket": int(time.time() // _ETAG_BUCKET_S)})


def _sent_etag(request):
    """The caller's last tag, from whichever carrier survived the path to us.

    A reverse proxy or edge cache in front of an adopting host may drop or rewrite
    `If-None-Match` (the same compare answering 304 in-process and 200 through the proxy is the
    fingerprint). The board's own client therefore sends it three ways: the standard header, an
    `X-Hub-ETag` header, and an `?etag=` query parameter. Quotes and the weak prefix are
    normalised, so a tag the proxy weakened still matches."""
    raw = (request.headers.get("If-None-Match") or request.headers.get("X-Hub-ETag")
           or request.GET.get("etag") or "")
    return raw.strip().replace("W/", "").strip('"')


def _conditional(request, etag, build_body):
    """304 when the caller already holds `etag`; otherwise the JSON body. The tag rides both."""
    if _sent_etag(request) == etag.replace("W/", "").strip('"'):
        resp = HttpResponse(status=304)
    else:
        resp = JsonResponse(build_body())
    resp["ETag"] = etag
    resp["X-Hub-ETag"] = etag
    return resp


# First-paint scaling. A board's snapshot carries every row of every collection, and the page
# paints nothing until the browser has parsed it -- on a long-lived board that is megabytes
# before the first pixel. So a collection larger than HEAD_ROWS rides on the WIRE as a head: every
# row that is still live (open work, active directives, open questions and gaps) plus the newest
# of the rest up to HEAD_ROWS. `partial[<key>]` names each headed collection, `collection_counts`
# stays exact, and a tab fetches its whole list from GET /hub/<type>.json on first open.
#
# The head exists ONLY on the wire (`_wire_snapshot`). The cached snapshot every server-side
# consumer reads -- the attention rail, counts, search, the MCP tools -- stays whole, so no derived
# number can ever be computed over a head. 0 disables heading.
def _int_setting(name, default):
    """A Django setting, else the environment, else `default` -- where 0 is a real value."""
    value = hub_app._dj_setting(name, None)
    if value is None:
        value = os.environ.get(name)
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


HEAD_ROWS = _int_setting("HUB_SNAPSHOT_HEAD_ROWS", 60)
_HEADABLE = ("tasks", "adrs", "feats", "gaps", "caps", "deploys", "notes", "directives", "acks")


def _row_live(key, row):
    status = str(row.get("status") or "")
    if key == "tasks":
        return status not in ("done", "dropped")
    if key == "directives":
        return status == "active"
    if key == "gaps":
        return status in ("open", "investigating")
    if key == "notes":
        return "open" in [str(t).lower() for t in (row.get("tags") or [])]
    return False


def _row_updated(row):
    prov = row.get("provenance") or {}
    return str(prov.get("updated_at") or prov.get("created_at") or row.get("ts") or "")


def _head(key, rows):
    live_ids = {r.get("id") for r in rows if _row_live(key, r)}
    rest = sorted((r for r in rows if r.get("id") not in live_ids), key=_row_updated, reverse=True)
    keep = live_ids | {r.get("id") for r in rest[:max(0, HEAD_ROWS - len(live_ids))]}
    return [r for r in rows if r.get("id") in keep]      # the snapshot's own order, filtered


def _wire_snapshot(snap):
    """The snapshot as SERVED: exact collection counts, and heads for the large collections."""
    counts = {key: len(snap.get(key) or []) for key in _COLLECTION.values()}
    wire = dict(snap)
    wire["collection_counts"] = counts
    partial = {}
    if HEAD_ROWS > 0:
        for key in _HEADABLE:
            rows = snap.get(key) or []
            if len(rows) > HEAD_ROWS:
                head = _head(key, rows)
                if len(head) < len(rows):
                    wire[key] = head
                    partial[key] = True
    wire["partial"] = partial
    return wire


def hub_json(request):
    _, snap = _snapshot(request.GET.get("served"))
    wire = _wire_snapshot(snap)
    # 304 on the caller's last tag: a reconnect re-ground or supervisor read gets an empty body
    # when nothing changed, instead of the full snapshot every time.
    return _conditional(request, _etag(wire), lambda: wire)


def _delta_payload(since, served=None):
    """Build the exact canonical push patch without audit or repository work."""
    store = hub_app.store()
    try:
        target = store.latest_cursor()
        events, state = _projected(store, target)
        changed_aggs, seen = [], set()
        for event in _events_through(store, since, target["seq"]):
            aggregate = event.get("aggregate")
            if aggregate and aggregate not in seen:
                seen.add(aggregate)
                changed_aggs.append(aggregate)
    finally:
        store.close()
    entities, flags = state.get("entities", {}), state.get("flags", {})
    changed = [{**entities[aggregate], **flags.get(aggregate, {})}
               for aggregate in changed_aggs if aggregate in entities]
    audit = _AUDIT_CACHE.get("value") or {"ok": None, "violations": []}
    deliv, delivery_materialized = _delivery_fast(state, target, served)
    live = _live_blocks(events, state, audit, deliv, target)
    return {
        "changed": changed, "removed": [],
        "cursor": {"seq": int(target.get("seq", 0)), "hash": target.get("hash", "")},
        "audit": {"ok": audit.get("ok")}, "live": live,
        "metadata": {"since": since, "changed": len(changed),
                     "delivery_materialized": delivery_materialized},
    }


def delta_json(request):
    """Reconnect/recovery read. Steady-state clients receive this payload inside SSE."""
    try:
        since = max(0, int(request.GET.get("since", "0")))
    except (TypeError, ValueError):
        since = 0
    return JsonResponse(_delta_payload(since, served=request.GET.get("served")))


_PLURAL_TO_TYPE = {v: k for k, v in _COLLECTION.items()}


def type_json(request, type):
    """One WHOLE collection, in exactly the snapshot's row shape: {data, count, cursor, metadata}.

    Accepts the singular type (`task`) or the snapshot key (`tasks`), so a tab can hydrate the
    collection its head came from without a lookup table. Conditional on a per-collection tag:
    a tab re-reading an unchanged list gets a 304, not the list."""
    key = _COLLECTION.get(type) or (type if type in _PLURAL_TO_TYPE else None)
    if not key:
        raise Http404("unknown type")
    _, snap = _snapshot()
    data = snap.get(key) or []
    cursor = ((snap.get("live") or {}).get("cursor")) or {}
    body = {"data": data, "count": len(data),
            "cursor": {"seq": cursor.get("seq"), "hash": cursor.get("hash")},
            "metadata": {"type": _PLURAL_TO_TYPE[key], "key": key, "count": len(data)}}
    return _conditional(request, 'W/"%s"' % content_hash(data), lambda: body)


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


# Concurrent board streams per process on the THREAD-HOLDING (WSGI) path. Each open tab's
# stream pins one worker thread until it closes, so a handful of tabs can starve a small thread
# pool of the requests that actually write. 0 = unlimited. The ASGI path holds no worker thread
# per stream and is not capped.
LIVE_STREAMS_MAX = _int_setting("HUB_LIVE_STREAMS_MAX", 3)


class _Unbounded:
    def acquire(self, blocking=False):
        return True

    def release(self):
        pass


_LIVE_SLOTS = (threading.BoundedSemaphore(LIVE_STREAMS_MAX) if LIVE_STREAMS_MAX > 0
               else _Unbounded())


@require_GET
def live_events(request):
    """Persistent push stream carrying canonical patches, not polling hints.

    Mutations wake this stream through the realtime bus. The patch is built from the subscriber's
    last emitted cursor through one exact canonical head and is applied directly by the browser.
    Cursor catch-up happens once on connection/reconnection; steady state performs no interval
    query. Heartbeats are transport keepalives only and never trigger a read.
    """
    raw_since = request.headers.get("Last-Event-ID") or request.GET.get("since") or "0"
    try:
        requested = max(0, int(raw_since))
    except (TypeError, ValueError):
        requested = 0

    served = request.GET.get("served")

    def initial_cursor():
        store = hub_app.store()
        try:
            head = store.latest_cursor()
            return min(requested, head["seq"]), head
        finally:
            store.close()

    def ready_frame(cursor, head):
        payload = {"seq": cursor, "ts": head.get("ts"),
                   "realtime": hub_app.realtime_info()}
        return (f"id: {cursor}\nevent: ready\n"
                f"data: {json.dumps(payload, separators=(',', ':'))}\n\n")

    def patch_frame(payload):
        cursor = int((payload.get("cursor") or {}).get("seq", 0))
        return (f"id: {cursor}\nevent: patch\n"
                f"data: {json.dumps(payload, separators=(',', ':'))}\n\n")

    def stream():
        # A WSGI stream holds a worker THREAD for its whole life. Past the cap, the tab is turned
        # away with one `busy` frame naming a jittered retry instead of starving every ordinary
        # request of the thread pool. The slot is taken when the body is first iterated -- a
        # response that is never iterated never held one -- and released in `finally`.
        if not _LIVE_SLOTS.acquire(blocking=False):
            yield busy_frame()
            return
        try:
            yield from held_stream()
        finally:
            _LIVE_SLOTS.release()

    def busy_frame():
        retry_ms = int(random.uniform(8000, 20000))
        body = {"reason": "live_streams_saturated", "limit": LIVE_STREAMS_MAX,
                "retry_ms": retry_ms}
        return (f"retry: {retry_ms}\nevent: busy\n"
                f"data: {json.dumps(body, separators=(',', ':'))}\n\n")

    def held_stream():
        subscription = realtime.subscribe(hub_app.HUB_DIR, channel=hub_app.PROJECT_KEY)
        cursor, head = initial_cursor()
        try:
            yield "retry: 1500\n\n"
            yield ready_frame(cursor, head)
            # Re-ground lease/telemetry projections even when the ledger cursor did not advance
            # while disconnected.
            payload = _delta_payload(cursor, served=served)
            cursor = payload["cursor"]["seq"]
            yield patch_frame(payload)
            while True:
                signals = subscription.wait(timeout=15)
                if signals:
                    payload = _delta_payload(cursor, served=served)
                    cursor = payload["cursor"]["seq"]
                    yield patch_frame(payload)
                else:
                    yield (f"id: {cursor}\nevent: heartbeat\n"
                           f"data: {json.dumps({'seq': cursor}, separators=(',', ':'))}\n\n")
        except GeneratorExit:
            return

    async def async_stream():
        subscription = realtime.subscribe_async(hub_app.HUB_DIR, channel=hub_app.PROJECT_KEY)
        cursor, head = await asyncio.to_thread(initial_cursor)
        try:
            yield "retry: 1500\n\n"
            yield ready_frame(cursor, head)
            payload = await asyncio.to_thread(_delta_payload, cursor, served)
            cursor = payload["cursor"]["seq"]
            yield patch_frame(payload)
            while True:
                signals = await subscription.wait(timeout=15)
                if signals:
                    payload = await asyncio.to_thread(_delta_payload, cursor, served)
                    cursor = payload["cursor"]["seq"]
                    yield patch_frame(payload)
                else:
                    yield (f"id: {cursor}\nevent: heartbeat\n"
                           f"data: {json.dumps({'seq': cursor}, separators=(',', ':'))}\n\n")
        finally:
            subscription.close()

    content = async_stream() if hasattr(request, "scope") else stream()
    response = StreamingHttpResponse(content, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-store, no-transform, must-revalidate"
    response["X-Accel-Buffering"] = "no"
    response["X-Hub-Realtime-Scope"] = hub_app.realtime_info()["scope"]
    return response


def next_json(request):
    """DISCOVER: ranked unblocked tasks without a live lease, including stale reclaims.

    A worker's entrypoint — pull the top task, claim it, and complete the real operation. A receipt
    accompanies completion only when the task declared a transient critical probe. The board
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
    live_leases = hub_app.leases()
    wip_st = hub_app.wip_status(len(live_leases))
    if wip_st["saturated"]:
        # At saturation the rail answers 429 rather than handing out work the claim seam would
        # refuse to lease. A GET records nothing — the recorded signals come from the claim seam.
        return JsonResponse({"error": "board_saturated", **wip_st}, status=429)
    flags = state.get("flags", {})
    entities = state.get("entities", {})
    lease_map = {row.get("task"): row for row in live_leases}
    tasks = state["by_type"].get("task", [])
    ready, needs_spec = [], []
    for t in tasks:
        cls = flow.classify(t, flags.get(t["id"], {}), lease_map.get(t["id"]))
        if cls["available"]:
            ready.append(dict(t, flow_state=cls["state"], flow_reason=cls["reason"],
                              stale_reclaim=cls["stale_reclaim"]))
        elif cls["state"] == "needs_spec":
            needs_spec.append(dict(t, needs=cls["reason"], flow_state=cls["state"]))
    from hub_core import schedule
    busy_touches = set()
    for lease in live_leases:
        busy_touches.update(schedule.normalized_touches(entities.get(lease.get("task"), {})))
    ready = schedule.order_ready(ready, flags, busy_touches=busy_touches)

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
    rows = [dict(t, available=True) for t in ready[:n]]
    return JsonResponse({"data": rows, "needs_spec": needs_spec[:n], "snoozed": snoozed[:n],
                         "metadata": {"available": len(ready), "unblocked": len(ready),
                                      "ready": len(ready), "needs_spec": len(needs_spec),
                                      "snoozed": len(snoozed),
                                      "snoozed_next": snoozed[0]["not_before"] if snoozed else None,
                                      **wip_st}})


# ── The ask/answer surfaces: questions, the addressed inbox, and its long-poll ──

_ANSWER_ECHO = "\n\n---\nIn answer to your question:"


@require_GET
def questions_json(request):
    """Every question with the numbers the feed is actually about: a question is not "one
    row", it is a person blocked for a measurable length of time. Rows carry whether an
    answer exists AND whether the asker acknowledged it landing — "answered" and
    "delivered" are different facts, and only the second closes the loop."""
    state, _ = _snapshot()
    entities = state["entities"]
    acks = {}
    for ent in entities.values():
        if ent.get("type") == "ack":
            acks.setdefault(ent.get("directive"), set()).add(
                str(ent.get("agent") or "").lower())
    answers = {}
    for ent in entities.values():
        if ent.get("type") == "directive" and ent.get("answers"):
            answers[ent["answers"]] = ent
    rows = []
    now = time.time()
    for eid, ent in entities.items():
        if ent.get("type") != "note":
            continue
        tags = [str(t).lower() for t in (ent.get("tags") or [])]
        if "question" not in tags or inbox_core.AUTOMATION_TAGS.intersection(tags):
            continue
        prov = ent.get("provenance") or {}
        asker = str(ent.get("asker") or prov.get("agent") or "").lower()
        reply = answers.get(eid)
        answer_body = str((reply or {}).get("body_md") or "")
        # The answer directive echoes the question back at the end; strip it so the reply
        # reads as a reply.
        if _ANSWER_ECHO in answer_body:
            answer_body = answer_body.split(_ANSWER_ECHO)[0]
        answer_prov = (reply or {}).get("provenance") or {}
        acked = bool(reply and asker in (acks.get(reply.get("id")) or set()))
        asked_at = str(prov.get("created_at") or "")
        asked_epoch = _epoch(asked_at) or 0
        answered_at = str(answer_prov.get("created_at") or answer_prov.get("updated_at") or "") \
            if reply else ""
        row = {
            "id": eid, "asker": asker, "at": asked_at,
            "title": str(ent.get("title") or ""),
            "context": str(ent.get("body_md") or "")[:1400],
            "open": "open" in tags,
            "answered": bool(reply),
            "answer_id": (reply or {}).get("id", ""),
            "answer": answer_body[:1800],
            "answer_by": str(answer_prov.get("agent") or "") if reply else "",
            "answer_at": answered_at,
            "acked": acked,
            "asked_epoch": asked_epoch,
        }
        if reply:
            replied = _epoch(answered_at)
            row["reply_seconds"] = max(0, int(replied - asked_epoch)) \
                if (asked_epoch and replied) else None
            row["waiting_seconds"] = None
        else:
            row["reply_seconds"] = None
            row["waiting_seconds"] = max(0, int(now - asked_epoch)) if asked_epoch else None
        rows.append(row)
    # Newest first within each state; anything still needing a human ahead of what is
    # closed: waiting-for-an-answer, then answered-but-not-yet-delivered, then done.
    rows.sort(key=lambda r: r["at"], reverse=True)
    rows.sort(key=lambda r: (r["acked"], r["answered"]))

    waits = [r["waiting_seconds"] for r in rows if r["open"] and r["waiting_seconds"] is not None]
    answered_waits = sorted(r["reply_seconds"] for r in rows if r["reply_seconds"] is not None)
    mid = len(answered_waits) // 2
    median_reply = (answered_waits[mid] if len(answered_waits) % 2
                    else (answered_waits[mid - 1] + answered_waits[mid]) // 2) \
        if answered_waits else None
    # Per-person lanes: who is asking, how many are still open, how long they have waited.
    people = {}
    for r in rows:
        lane = people.setdefault(r["asker"] or "unknown",
                                 {"agent": r["asker"] or "unknown", "asked": 0, "open": 0,
                                  "worst_wait": 0})
        lane["asked"] += 1
        if r["open"]:
            lane["open"] += 1
            lane["worst_wait"] = max(lane["worst_wait"], r["waiting_seconds"] or 0)
    lanes = sorted(people.values(), key=lambda l: (-l["open"], -l["worst_wait"]))
    # A 14-day strip, so "are we keeping up" is answerable at a glance.
    days = []
    for back in range(13, -1, -1):
        lo, hi = now - (back + 1) * 86400, now - back * 86400
        days.append({
            "asked": sum(1 for r in rows if lo <= r["asked_epoch"] < hi),
            "answered": sum(1 for r in rows
                            if r["answered"] and lo <= (_epoch(r["answer_at"]) or 0) < hi),
        })
    longest = max(waits) if waits else 0
    return JsonResponse({"data": rows, "metadata": {
        "open": sum(1 for r in rows if r["open"]),
        "answered_not_acked": sum(1 for r in rows if r["answered"] and not r["acked"]),
        "closed": sum(1 for r in rows if r["acked"]),
        "longest_wait_seconds": longest,
        "longest_wait_who": next((r["asker"] for r in rows
                                  if r["open"] and (r["waiting_seconds"] or 0) == longest), ""),
        "median_reply_seconds": median_reply,
        "replied_count": len(answered_waits),
        "lanes": lanes[:6],
        "days": days}})


def _operator_agent() -> str:
    """The identity questions are addressed to and answers come from. One name, settable —
    HUB_OPERATOR_AGENT in settings or the environment."""
    import os as _os
    return str(hub_app._dj_setting("HUB_OPERATOR_AGENT")
               or _os.environ.get("HUB_OPERATOR_AGENT") or "operator").strip().lower()


@require_GET
def inbox_json(request):
    """What is addressed to ?agent= right now: directives aimed at it, the answer to its
    own question, and — for the operator — every open question."""
    agent = (request.GET.get("agent") or "").strip().lower()
    if not agent:
        return JsonResponse({"errors": [{"code": "need_agent", "msg": "pass ?agent="}]}, status=400)
    state, _ = _snapshot()
    return JsonResponse({"data": inbox_core.snapshot(state, agent, _operator_agent()),
                         "metadata": {"agent": agent, "operator": _operator_agent()}})


@require_GET
def inbox_wait(request):
    """Long-poll: return the moment something is addressed to ?agent=.

    This is the mechanism behind "asking the operator reaches them in about a second": a
    supervisor loop spends the sleep it was already doing blocked here, so an ask raises a
    notification on the answerer's side without anybody watching a browser tab, and the
    answer lands back on the asker's side the same way. Bounded hard — never longer than
    MAX_WAIT_S, never more than MAX_WAITERS at once (past the ceiling it answers
    immediately with degraded=true: an honest poll, not a starved server)."""
    agent = (request.GET.get("agent") or "").strip().lower()
    if not agent:
        return JsonResponse({"errors": [{"code": "need_agent", "msg": "pass ?agent="}]}, status=400)
    known = (request.GET.get("fp") or "")[:64]
    try:
        timeout = float(request.GET.get("wait") or inbox_core.MAX_WAIT_S)
    except (TypeError, ValueError):
        timeout = inbox_core.MAX_WAIT_S

    def snapshot_fn(who):
        s = hub_app.store()
        try:
            state = project.state(s.events())
        finally:
            s.close()
        return inbox_core.snapshot(state, who, _operator_agent())

    def signal_fn():
        # The cheap fingerprint of everything the snapshot depends on — the wait loop must
        # not fold the whole ledger per poll tick. "" on error never equals a real signal,
        # so a failed read degrades to always-fold rather than skipping a real change.
        try:
            s = hub_app.store()
            try:
                return str(s.latest_cursor().get("seq") or 0)
            finally:
                s.close()
        except Exception:                                    # noqa: BLE001
            return ""

    payload = inbox_core.wait(agent, known, timeout, snapshot_fn=snapshot_fn, signal_fn=signal_fn)
    return JsonResponse({"data": payload,
                         "metadata": {"agent": agent, "operator": _operator_agent(),
                                      "max_wait_s": inbox_core.MAX_WAIT_S}})


# ── The operational error stream, whoami, and board search ──

@require_GET
def errors_json(request):
    """The operational error stream with the bar applied at read. Deferred rows are never
    DROPPED — a stream that silently discards two thirds of its input is one whose "all
    clear" cannot be trusted; they are one query param away (?include=deferred)."""
    rows, metadata, _unclaimed = _errors_block()
    include = (request.GET.get("include") or "").lower()
    data = rows if include in ("deferred", "all") else [r for r in rows if r.get("bar") == "on"]
    metadata["bar"] = ("every row recorded in the window; `bar` says which are on the board"
                       if data is rows else
                       "critical and high problems in this system's own surfaces; add "
                       "?include=deferred for everything the bar held back")
    return JsonResponse({"data": data, "metadata": metadata})


@require_GET
def whoami_json(request):
    """What the hub ACTUALLY received on this request: the presented credential's mode and
    subject (or why it is invalid), and which X-Hub-* headers survived any proxy. A stale
    seat looks identical whether the caller never wrote, a proxy dropped a header, or the
    credential went invalid — this makes the difference one request. Never echoes tokens."""
    from . import hub_write
    auth, problem = hub_write._authenticate(request)
    seen = {k: v for k, v in request.headers.items() if k.lower().startswith("x-hub-")}
    data = {
        "operator": _operator_agent(),
        "headers_seen": seen,
        "note": ("mode 'scoped-agent' means writes are attributed to `subject` by the "
                 "credential itself; 'shared-root' means the compatibility token was "
                 "presented and per-call agent fields are labels only; null means the "
                 "presented credential (if any) is not valid here. headers_seen is what "
                 "reached the hub after any proxy — a header you sent that is missing here "
                 "was dropped in transit."),
    }
    if auth:
        data.update({"mode": auth.mode, "subject": auth.subject,
                     "actor_kind": auth.actor_kind,
                     "scopes": list(getattr(auth, "scopes", ()) or ()),
                     "credential_id": auth.credential_id})
    else:
        data.update({"mode": None, "subject": None, "problem": problem})
    return JsonResponse({"data": data})


_SEARCH_STOP = {"the", "a", "an", "is", "of", "to", "and", "or", "in", "on", "for", "it",
                "with", "at", "this", "that", "was", "are"}


@require_GET
def search_json(request):
    """Ranked multi-term search over the whole board — the PULL half of "push pointers,
    pull content". A substring scan returns nothing for a natural query even when the
    exact entity exists, and an agent that cannot find the fact at the moment of need
    re-derives it (or hits the trap it warned about). Stdlib term frequency over
    title/name/body fields, weighted headline-over-body, exact-phrase boosted."""
    q = (request.GET.get("q") or "").strip().lower()[:200]
    try:
        limit = max(1, min(50, int(request.GET.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    if not q:
        return JsonResponse({"data": [], "metadata": {"q": "", "msg": "pass ?q="}})
    terms = [t for t in re.split(r"[^a-z0-9._-]+", q) if t and t not in _SEARCH_STOP][:24]
    state, _ = _snapshot()
    hits = []
    for ent in state["entities"].values():
        if not isinstance(ent, dict):
            continue
        if ent.get("status") in ("superseded", "dropped", "rejected"):
            continue
        title = str(ent.get("title") or ent.get("name") or "")
        body = str(ent.get("body_md") or ent.get("summary") or ent.get("decision_md") or
                   ent.get("acceptance") or "")
        tags = " ".join(str(t) for t in (ent.get("tags") or []))
        hay_t, hay_b = (title + " " + tags).lower(), body.lower()
        score = 0.0
        for term in terms:
            score += 3.0 * hay_t.count(term) + 1.0 * hay_b.count(term)
        if not score:
            continue
        if q in hay_t:
            score += 8.0            # exact phrase in the headline
        elif q in hay_b:
            score += 3.0
        score += sum(1.5 for term in terms if term in hay_t)   # breadth of term coverage
        hits.append({"id": ent.get("id"), "type": ent.get("type"), "title": title[:200],
                     "status": ent.get("status") or ent.get("maturity") or "",
                     "excerpt": body[:400], "score": round(score, 2)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return JsonResponse({"data": hits[:limit],
                         "metadata": {"q": q, "terms": terms, "matched": len(hits)}})


@require_POST
@csrf_protect
def client_error(request):
    """Accept bounded browser diagnostics from a same-origin board session. The browser
    never sends stack frames, page contents, or credentials; this endpoint repeats that
    constraint server-side and the shared store performs final redaction. Browser noise is
    deliberately held BELOW the bar by the shared read-side predicate — a queue that fills
    with other people's stale-tab errors is a queue everyone learns to ignore."""
    if len(request.body or b"") > 16_384:
        return JsonResponse({"errors": [{"code": "payload_too_large"}]}, status=413)
    try:
        body = json.loads((request.body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"errors": [{"code": "object_required"}]}, status=400)
    source = str(body.get("source") or "board")[:120]
    row = hub_app.record_error(
        f"browser.{source}",
        str(body.get("message") or "Browser operation failed")[:800],
        severity=str(body.get("severity") or "error").lower(),
        code=str(body.get("code") or "client_error")[:120],
        context={"component": "hub-board",
                 "operation": str(body.get("operation") or source)[:120],
                 "path": request.path_info},
    )
    return JsonResponse({"data": {"recorded": True, "fingerprint": row["fingerprint"]}}, status=201)


# Marker consumed by the computed route audit: a same-origin, CSRF-protected, bounded
# browser telemetry capability, not general write authority.
client_error._hub_origin_gated = True
