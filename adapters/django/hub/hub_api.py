"""Hub machine READ API. Every surface renders FROM the event-log snapshot (single source of
truth); the HTML embeds the same payload. Doctrine sections 1-2 + the hub API contract.

The LIVE endpoint emits only an authenticated cursor and a safe event envelope; the browser then
reconciles from the same canonical snapshot the HTML and the JSON API serve. Animation is never
allowed to become a second source of truth — an event says *something moved*, and the board is
re-read to find out what.
"""
import json
import re
import time

from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from hub_core import (adherence, cost, dag, errorlog, failure_taxonomy, inbox as inbox_core,
                      project, projections, telemetry, wip)

from . import delivery, hub_app

_COLLECTION = {"task": "tasks", "adr": "adrs", "feat": "feats", "gap": "gaps", "cap": "caps",
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
    is the true in-flight signal. An expired lease is not in flight (the task is already free to
    reclaim); a live lease older than stall_s is flagged so a stuck worker is visible rather than
    merely absent from the completions feed. A lease is a PROMISE, not an observation, so it is
    additionally checked against one: a holder whose presence has gone silent past the horizon is
    reported `abandoned` — the row still shows who holds it (somebody's work stopped mid-task,
    which is information), it just stops counting as somebody working."""
    now = time.time()
    rows = []
    ents = state.get("entities", {})
    for task, lease in hub_app.all_leases().items():
        ent = ents.get(task, {})
        if ent.get("status") in ("done", "dropped"):
            continue
        claimed = lease.get("claimed") or 0
        age = int(now - claimed) if claimed else None
        rows.append({
            "task": task, "agent": lease.get("agent"), "machine": lease.get("machine") or "",
            "title": ent.get("title") or task.rsplit(":", 1)[-1],
            "status": ent.get("status"), "age_s": age,
            "stalled": bool(age is not None and age > stall_s),
            "abandoned": bool(lease.get("abandoned")),
            "unheard_s": lease.get("unheard_s"),
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
        entry["count"] += 1 + int(row.get("occurrences_since_last") or 0)
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
        if t.get("poison_blocked"):
            add(1, "circuit-open",
                t.get("poison_reason") or "verification keeps failing — the circuit breaker is open",
                t["id"], t.get("title"))

    for r in (inflight or []):
        if r.get("abandoned"):
            add(1, "abandoned-claim",
                f"{r.get('agent')} holds the lease but has not been observed for "
                f"{_fmt_age(r.get('unheard_s'))} — the work stopped mid-task",
                r.get("task"), r.get("title"))
        elif r.get("stalled"):
            add(1, "stalled-lease",
                f"{r.get('agent')} has held the lease {_fmt_age(r.get('age_s'))} without finishing",
                r.get("task"), r.get("title"))

    # OPEN QUESTIONS are operator work: an ask nobody sees is a worker blocked on one fact,
    # and the cost of a question compounds for as long as it sits.
    for q in (asks or []):
        add(1, "open-question",
            f"{q.get('from')} asks: {str(q.get('title') or '')[:120]}", q.get("id"),
            q.get("title"), route={"view": "overview", "focus": "asks"})

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

    # Live consoles per agent, from observed presence: an agent working WITHOUT a formal
    # claim must not render as idle — that is the exact case that makes a busy fleet look
    # asleep. A session's focus also becomes the card's "on" line when no lease exists.
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
        if lease and lease.get("abandoned"):
            status = "abandoned"
        elif lease and lease.get("stalled"):
            status = "stalled"
        elif lease:
            status = "working"
        elif sessions and (sessions[0].get("age_s") or 10 ** 9) < 300:
            status = "active"           # a live console, just not on a board task
        elif idle_s is not None and idle_s < 300:
            status = "recent"
        else:
            status = "idle"
        cards.append({
            "agent": ag, "status": status,
            "machine": (lease or {}).get("machine") or
                       (sessions[0].get("machine") if sessions else "") or "",
            "task": lease.get("title") if lease else None,
            "task_id": lease.get("task") if lease else None,
            "focus": (sessions[0].get("focus") if (not lease and sessions) else "") or "",
            "age_s": lease.get("age_s") if lease else None,
            "idle_s": idle_s, "trail": trails.get(ag, []),
            "sessions": sessions[:6],
            "done_total": sum(1 for t in state.get("by_type", {}).get("task", [])
                              if t.get("status") == "done"
                              and (t.get("provenance") or {}).get("agent") == ag),
            **_plan_progress(ents.get(lease.get("task"), {}) if lease else {}),
        })
    rank = {"working": 0, "stalled": 0, "abandoned": 0, "active": 1, "recent": 1, "idle": 2}
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
        # Presence and the error sidecars ride it for the same reason: both change with no
        # ledger event, and a memo that ignores them serves a live board whose fleet strip
        # and error card are frozen at whatever the last append happened to capture.
        key = (cur["seq"], cur["hash"], served, _leases_fp(), _telemetry_fp(),
               hub_app.presence_stamp(), errorlog.stamp(hub_app.HUB_DIR), hub_app._git_head())
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
        # The addressed plane and the operational stream, computed once for every consumer:
        # the attention rail, the cockpit cards, the delta, and the JSON endpoints must all
        # read ONE answer to "what is open" and "what is broken".
        asks = inbox_core.question_items(state)
        error_rows, error_meta, error_unclaimed = _errors_block()
        try:
            sessions_live = hub_app.live_sessions()
        except Exception:                                    # noqa: BLE001 - never 500 the board
            sessions_live = []
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
            # OPEN QUESTIONS: who is blocked on one fact, and for how long. The whole loop —
            # ask, deliver, answer, ack — closes through the board, so the board shows it.
            "asks": asks[:12],
            "asks_open": len(asks),
            # THE OPERATIONAL ERROR STREAM, bar-annotated, with the shape a reader needs
            # (trend, sources, coverage) — the failures the ledger audit cannot see.
            "errors": error_rows[:40],
            "error_log": error_meta,
            # EVERY LIVE CONSOLE, flat: the surface that stops two sessions from unknowingly
            # working the same thing. The per-agent fleet cards roll these up; this is the
            # per-console truth.
            "sessions_live": sessions_live[:12],
            "attention": _attention(state, audit, inflight, adher, deliv,
                                    asks=asks, error_unclaimed=error_unclaimed),
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
    """The snapshot's ETag: the head-cursor hash (append-only + hash-chained, so unchanged
    head means unchanged fold), `served` (it feeds the coherence block — two probes must not
    share one ETag), and a digest of the presence/error sidecar stamps — those change the
    payload with NO ledger event, and an ETag blind to them makes the 304 path suppress
    exactly the live blocks the sidecars feed."""
    import hashlib as _h
    cur = (snap.get("live") or {}).get("cursor") or {}
    served = ((snap.get("build") or {}).get("served_sha")) or ""
    side = _h.sha256(repr((hub_app.presence_stamp(),
                           errorlog.stamp(hub_app.HUB_DIR))).encode()).hexdigest()[:12]
    return '"%s.%s.%s"' % (cur.get("hash", ""), served, side)


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
    s = hub_app.store()
    try:
        head = s.latest_cursor()
        try:
            since = max(0, int(request.GET.get("since", "0")))
        except (TypeError, ValueError):
            since = 0
        changed_aggs, seen = [], set()
        for ev in s.events_after(since, limit=10_000):
            agg = ev.get("aggregate")
            if agg and agg not in seen:
                seen.add(agg)
                changed_aggs.append(agg)
    finally:
        s.close()
    entities = state.get("entities", {})
    flags = state.get("flags", {})
    changed = [{**entities[a], **flags.get(a, {})} for a in changed_aggs if a in entities]
    audit = snap.get("audit") or {}
    live = snap.get("live") or {}
    return JsonResponse({
        "changed": changed, "removed": [],
        "cursor": {"seq": head["seq"], "hash": head["hash"]},
        "audit": {"ok": audit.get("ok")},
        # The cockpit blocks ride the delta too. Without them a live board patched entity rows
        # while its hero, fleet and attention rail stayed frozen at page-load values until the
        # next full sync — the most-watched part of the page was the last to move.
        "live": {k: live.get(k) for k in ("cursor", "progress", "adherence", "fleet", "inflight",
                                          "attention", "readiness", "activity", "dag",
                                          "worker_health", "wip", "delivery",
                                          # The live-only blocks ride the delta too: a
                                          # presence or errors tick patches them without a
                                          # full sync, so the fleet strip and the error card
                                          # are never the last part of the page to move.
                                          "asks", "asks_open", "errors", "error_log",
                                          "sessions_live")},
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
        # Sidecar baselines belong at connection open, BEFORE the first yield: a write that
        # lands between open and the first poll must read as a change, not vanish into the
        # baseline. Presence and errors change with no ledger event — that is exactly why a
        # stream that watches only the ledger leaves the fleet strip and the error card
        # updating only when something unrelated happens to append.
        presence_seen = hub_app.presence_stamp()
        errors_seen = errorlog.stamp(hub_app.HUB_DIR)
        try:
            head = store.latest_cursor()
            cursor = min(requested, head["seq"]) if requested else head["seq"]
            # Some reverse proxies hold a tiny first frame despite X-Accel-Buffering. An SSE
            # comment is invisible to EventSource; crossing the common 8 KiB buffer makes
            # the ready event observable immediately and keeps the polling fallback honest.
            yield "retry: 1500\n: " + (" " * 8192) + "\n\n"
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
                stamp = hub_app.presence_stamp()
                if stamp != presence_seen:
                    presence_seen = stamp
                    yield (f"id: {cursor}\nevent: presence\n"
                           f"data: {json.dumps({'seq': cursor}, separators=(',', ':'))}\n\n")
                    heartbeat_at = time.monotonic() + 8
                estamp = errorlog.stamp(hub_app.HUB_DIR)
                if estamp != errors_seen:
                    errors_seen = estamp
                    yield (f"id: {cursor}\nevent: errors\n"
                           f"data: {json.dumps({'seq': cursor}, separators=(',', ':'))}\n\n")
                    heartbeat_at = time.monotonic() + 8
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


def _inbox_agent(request):
    return (request.GET.get("agent") or "").strip().lower()


@require_GET
def inbox_json(request):
    """What is addressed to ?agent= right now: directives aimed at it, the answer to its
    own question, and — for the operator — every open question."""
    agent = _inbox_agent(request)
    if not agent:
        return JsonResponse({"errors": [{"code": "need_agent", "msg": "pass ?agent="}]}, status=400)
    state, _ = _snapshot()
    return JsonResponse({"data": inbox_core.snapshot(state, agent, hub_app.operator_agent()),
                         "metadata": {"agent": agent, "operator": hub_app.operator_agent()}})


@require_GET
def inbox_wait(request):
    """Long-poll: return the moment something is addressed to ?agent=.

    This is the mechanism behind "asking the operator reaches them in about a second": a
    supervisor loop spends the sleep it was already doing blocked here, so an ask raises a
    notification on the answerer's side without anybody watching a browser tab, and the
    answer lands back on the asker's side the same way. Bounded hard — never longer than
    MAX_WAIT_S, never more than MAX_WAITERS at once (past the ceiling it answers
    immediately with degraded=true: an honest poll, not a starved server)."""
    agent = _inbox_agent(request)
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
        return inbox_core.snapshot(state, who, hub_app.operator_agent())

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
                         "metadata": {"agent": agent, "operator": hub_app.operator_agent(),
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


def whoami_json(request):
    """What the hub ACTUALLY received on this request: the presented token's CLASS, the
    identity it proves (or merely accepts), and which X-Hub-* headers arrived. A stale seat
    looks identical whether the caller never wrote, a proxy dropped a header, or the token
    went invalid — this makes the difference one request. Never echoes the token."""
    from . import hub_write
    resolved = hub_write.resolve_token(request.headers.get("X-Write-Token") or "")
    seen = {k: v for k, v in request.headers.items() if k.lower().startswith("x-hub-")}
    seen.pop("X-Write-Token", None)
    klass, bound = resolved if resolved else (None, None)
    return JsonResponse({"data": {
        "token_class": klass,
        "bound_agent": bound,
        "identity_is_proven": klass == "minted",
        "headers_seen": seen,
        "operator": hub_app.operator_agent(),
        "note": ("token_class 'minted' means writes are attributed to bound_agent by the "
                 "credential itself; 'general' means the caller declares its agent per "
                 "call; null means the presented token (if any) is not valid here. "
                 "headers_seen is what reached the hub after any proxy — a header you sent "
                 "that is missing here was dropped in transit."),
    }})


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
        title = str(ent.get("title") or ent.get("name") or ent.get("statement") or "")
        body = str(ent.get("body_md") or ent.get("summary") or ent.get("decision_md") or
                   ent.get("why") or "")
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
    row = errorlog.record(
        hub_app.HUB_DIR,
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
