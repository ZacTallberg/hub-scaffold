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
import logging
import re
import threading
import time
from pathlib import Path

from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from hub_core import (activity as activity_core, adherence, app_health,
                      attention as attention_core, checkpoints, cost, dag, errorlog,
                      failure_taxonomy, flow, liveness, inbox as inbox_core, offer, overlap,
                      plan as plan_rows, presence as _presence, problems as problems_core,
                      project, projections, task_health, task_rows, telemetry, upcast,
                      updates, wip)
from hub_core import overlap as overlap_core
from hub_core import record_state as _record_state
from hub_core.canonical import content_hash
from hub_core.text import preview

from . import delivery, hub_app, prewarm, realtime, roles

_COLLECTION = {"task": "tasks", "run": "runs", "adr": "adrs", "feat": "feats", "gap": "gaps", "cap": "caps",
               "deploy": "deploys", "note": "notes", "directive": "directives", "ack": "acks",
               "held": "held"}


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
    # WHO IS ACTUALLY THERE, resolved once for every row: a lease is held by a live console, and
    # the clock is only the backstop when liveness cannot be established (hub_core.liveness).
    try:
        roster = hub_app.roster()
    except Exception:                                        # noqa: BLE001 - never break a read
        roster = None
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
            # The claiming console, when the client said which: what lets one of an agent's
            # several live consoles — and only that one — show as holding this task.
            "session": str(lease.get("session") or "")[:8],
            "title": ent.get("title") or task.rsplit(":", 1)[-1],
            "status": ent.get("status"), "age_s": age,
            "heartbeat_age_s": heartbeat_age,
            "expires_in_s": max(0, int(lease.get("expires", 0) - now)),
            "last_heartbeat": heartbeat,
            "stalled": bool(heartbeat_age is not None and heartbeat_age > stall_s),
            **(liveness.holder(roster, lease, now, grace_s=hub_app.gone_grace_s())
               if roster is not None else
               {"holder_session": lease.get("session"), "holder_machine": lease.get("machine"),
                "holder_state": liveness.UNPROVABLE, "holder_gone_s": None,
                "holder_frees_in_s": None}),
            **_plan_progress(ent),
        })
    rows.sort(key=lambda r: (r.get("age_s") or 0), reverse=True)
    return rows


def _plan_progress(ent):
    """Per-task sub-progress from the worker's own plan checklist — so a task visibly climbs
    0->100 as the worker steps through it instead of flipping binary at done. plan_pct is None
    when the task carries no plan (nothing to show yet, which is not the same as no progress)."""
    plan = ent.get("plan") or []
    # Counted over WORK checkpoints only (hub_core.checkpoints): a grown placeholder or a
    # scheduler's own lifecycle row is shown in the plan but never moves "N of N done".
    counts = checkpoints.progress(plan)
    total, done, lifecycle = counts["total"], counts["done"], counts["lifecycle"]
    work = checkpoints.work_steps(plan)
    step = next((s.get("step") for s in work if not s.get("done")), None)
    # The last checkpoint note is the CONTEXT that turns "working on X" into "working on X,
    # last did Y" — the fact a peer needs to decide whether to coordinate, wait, or move on.
    noted = [s for s in work if s.get("note")]
    return {"plan_done": done, "plan_total": total, "plan_lifecycle": lifecycle,
            "step": (preview(step, 70) if step else None),
            "plan_pct": (round(done * 100 / total) if total else None),
            "last_note": (preview(noted[-1].get("note"), 90) if noted else None),
            # Runs that ended with this task unfinished (the hand-back row's own count).
            "handed_back": _handed_back_count(plan)}


def _handed_back_count(plan):
    """Runs that ended with this task unfinished (the hand-back row's own count)."""
    return sum(int(s.get("times") or 1) for s in (plan or [])
               if isinstance(s, dict) and s.get("kind") == "handed_back")


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
# The promise is enforced at READ, in the one predicate every consumer shares
# (hub_core.errorlog.passes_bar): a bar kept only in the renderer lies to every machine
# reader, and a bar applied at read reclassifies the whole retained window retroactively.


def _error_bar(row):
    """(on_bar, reason). One shared predicate for the board, the JSON API, the problem fold
    and the rail."""
    return errorlog.passes_bar(row)


def _row_app(row):
    """Which service a row belongs to — the reported app beats the one decoded from source."""
    return str((row.get("context") or {}).get("app") or row.get("origin_app") or "").lower()


def _errors_block(limit=errorlog.READ_LIMIT, app=""):
    """The operational error stream, bar-annotated, plus the SHAPE a reader actually needs:
    is it getting worse, which source is responsible, and is any of it even ours.

    COUNT THE QUEUE, NOT THE LISTING. Every count below comes from the bar's own verdict on
    each row, never from whatever subset a caller asked to SEE: a listing that includes the
    deferred rows must not report them as unassigned work (a live board once answered "67
    unclaimed" about a queue holding zero, every one of the 67 a startup notice or a
    transport blip the bar had already dismissed). `app` narrows the stream to one service
    before anything is counted, so a per-service reader gets that service's truth."""
    rows, metadata = errorlog.read(hub_app.HUB_DIR, limit=limit)
    app = str(app or "").strip().lower()
    if app:
        rows = [r for r in rows if _row_app(r) == app]
    now = time.time()
    # A problem somebody HOLDS is not unclaimed (hub_core.item_claims): a machine's live claim on
    # a fingerprint moves it from "needs you" to "in flight: <machine>", so no second agent is
    # sent to dig at work that is already held. Unreadable claims leave everything unclaimed --
    # the safe direction.
    from hub_core import item_claims
    try:
        claims = item_claims.live(hub_app.HUB_DIR, now, roster=hub_app.roster(),
                                  grace_s=hub_app.gone_grace_s())
    except Exception:                                        # noqa: BLE001
        claims = {}
    in_flight = 0
    buckets = [0] * 24
    severities = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    sources, external, on_bar_n, unclaimed = {}, 0, 0, []
    claimed_n, deferred_n, deferred_open, reasons = 0, 0, 0, {}
    for row in rows:
        ok, why = _error_bar(row)
        row["bar"] = "on" if ok else "deferred"
        if not ok:
            row["defer_reason"] = why
            deferred_n += 1
            deferred_open += 0 if row.get("acked") else 1
            reasons[why] = reasons.get(why, 0) + 1
        else:
            on_bar_n += 1
            held = claims.get(str(row.get("fingerprint") or ""))
            if held and not row.get("acked"):
                row["claimed_by"] = held
                in_flight += 1
            elif not row.get("acked"):
                unclaimed.append(row)
            else:
                claimed_n += 1
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
        "on_board": on_bar_n, "unclaimed": len(unclaimed), "claimed": claimed_n, "in_flight": in_flight,
        # The queue's oldest unclaimed row. A below-bar row ageing for a day is not a queue
        # that has been ignored for a day, and this figure is read as exactly that.
        "oldest_unclaimed_s": max((int(now - float(r.get("epoch") or now)) for r in unclaimed),
                                  default=0),
        # What the bar held back, broken out under its own key rather than folded into the
        # queue's numbers — counted, kept, one query away, and never mistaken for work.
        "deferred": deferred_n,
        "off_board": {"rows": deferred_n, "open": deferred_open,
                      "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))},
        # "Is this everything?" is the one question a list of errors can never answer about
        # itself, and the one a reader must have answered before an empty card may be read
        # as good news.
        "coverage": errorlog.coverage(rows, hub_app.HUB_DIR),
    })
    return rows, metadata, unclaimed


def _host_disk():
    """The hub's own drive, measured (cached) -- never allowed to break a snapshot."""
    try:
        from hub_core import hostdisk
        return hostdisk.reading(hub_app.HUB_DIR)
    except Exception as exc:                                 # noqa: BLE001
        return {"state": "unmeasured", "error": str(exc)[:200]}


def _attention(state, audit, inflight, adher=None, deliv=None, asks=None, error_unclaimed=None,
               disk=None):
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

    def add(rank, kind, reason, tid=None, title=None, route=None, waited_s=None):
        row = {"rank": rank, "kind": kind, "reason": reason, "id": tid, "route": route,
               "title": title or (tid.rsplit(":", 1)[-1] if tid else None)}
        if isinstance(waited_s, (int, float)) and not isinstance(waited_s, bool) and waited_s >= 0:
            row["waited_s"] = int(waited_s)
        items.append(row)

    for t in tasks:
        if t.get("poison_blocked") and t.get("operator_attention", True):
            add(1, "circuit-open",
                t.get("poison_reason") or "verification keeps failing — the circuit breaker is open",
                t["id"], t.get("title"))
        elif t.get("operator_attention") and t.get("last_failure"):
            add(1, "consequential-failure",
                (t.get("last_failure") or {}).get("note") or "failure requires operator authority",
                t["id"], t.get("title"))

    # THE HUB'S OWN DISK. Below the warning threshold every append, snapshot and backup on
    # this drive is one busy hour from failing -- and each failure would otherwise surface as
    # a different, misleading symptom. Named here while there is still room to act.
    if disk and disk.get("state") in ("warn", "critical"):
        from hub_core import hostdisk
        add(0 if disk["state"] == "critical" else 2, "host-disk-low", hostdisk.describe(disk),
            None, "%.1f GB free on %s" % (disk.get("free_gb") or 0.0,
                                           disk.get("drive") or "the hub's drive"))

    for r in (inflight or []):
        if r.get("stalled"):
            add(1, "stalled-lease",
                f"{r.get('agent')} has held the lease {_fmt_age(r.get('age_s'))} without finishing",
                r.get("task"), r.get("title"), waited_s=r.get("age_s"))

    # OPEN QUESTIONS are operator work: an ask nobody sees is a worker blocked on one fact,
    # and the cost of a question compounds for as long as it sits. Every row says how long it
    # has waited, and a STUCK ask (past HUB_ASK_STUCK_S) outranks a fresh one — the rail reads
    # the same `waited_s` the inbox carries, so the detector can never go silent on a key seam.
    # A question a machine has CLAIMED is waiting, not unclaimed: it drops down the rail and names
    # who holds it, instead of reading as "nobody has looked" (hub_core.item_claims).
    for q in (asks or []):
        held = q.get("claimed_by")
        if held:
            add(4, "question-in-flight",
                f"in flight on {held.get('machine')} ({_fmt_age(held.get('age_s'))}): "
                f"{preview(q.get('title'), 100)}", q.get("id"), q.get("title"),
                route={"view": "overview", "focus": "asks"}, waited_s=q.get("waited_s"))
            continue
        age = (" — waiting " + q["age"]) if q.get("age") else ""
        kind = "gate" if q.get("kind") == "gate" else (
            "stuck-question" if q.get("stuck") else "open-question")
        add(1 if (q.get("stuck") or q.get("kind") == "gate") else 2, kind,
            f"{q.get('from')} asks: {preview(q.get('title'), 120)}{age}", q.get("id"),
            q.get("title"), route={"view": "overview", "focus": "asks"}, waited_s=q.get("waited_s"))

    # HELD WORK AGES IN PUBLIC (hub_core.held): every open hold is on the rail with who holds it
    # and how long, its rank climbing with its age -- faster for a commit on one disk only.
    from hub_core import held as _held
    for h in _held.queue(state, time.time())[:8]:
        rank = {"critical": 1, "warn": 3}.get(h["urgency"], 5)
        add(rank, "held-" + h["urgency"],
            "held %s by %s: %s" % (_fmt_age(h["age_s"]), h.get("agent") or "someone",
                                   _held.detail(h)[:160]),
            h["id"], h.get("title"))

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
    # Folded into PROBLEMS: one line per thing somebody fixes, not per occurrence.
    for p in (error_unclaimed or [])[:5]:
        if "state" in p:
            add(2, "problem-unclaimed",
                "[%s] %s  (x%d, last %s ago)" % (p.get("where") or "?", str(p.get("title") or "")[:140],
                                                int(p.get("count") or 1), _fmt_age(p.get("since_last_s"))),
                None, p.get("id"), route={"view": "overview", "focus": "problems"})
            continue
        where = (p.get("context") or {}).get("app") or p.get("origin_app") or p.get("origin") or ""
        add(2, "error-unclaimed",
            (f"[{where}] " if where else "") + preview(p.get("message"), 140),
            None, str(p.get("source") or "error"),
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

    # A SLOW ROUTE is operator work: a route clients wait on that crossed the slow threshold in
    # at least one process window. Worded worst-window on purpose — it is NOT an average, and a
    # sentence that reads as steady-state will be acted on as one.
    try:
        from .middleware import route_timings, slow_routes
        timings = route_timings()
        for route in slow_routes(timings)[:3]:
            v = timings.get(route) or {}
            add(3, "slow-route",
                "%s: worst-window p95 %.1f s over %d request(s) in %d window(s) this hour"
                % (route, (v.get("p95_worst_ms") or 0) / 1000.0, v.get("count") or 0,
                   v.get("windows") or 0), None, route, route={"view": "perf"})
    except Exception:                                        # noqa: BLE001 - never break the rail
        pass

    if _readiness(state).get("ready", 0) == 0 and not (inflight or []):
        add(0, "board-drained",
            "no ready work and no worker in flight — spec a needs-spec item or file new work")

    # Within a rank the LONGEST WAIT leads — the same key the inbox and the Questions card
    # order by. A row with no measurable age sorts after every aged one (never as age 0), and
    # the id only breaks exact ties, so an old ask can never sit behind a fresh one by id.
    items.sort(key=lambda i: (i["rank"], "waited_s" not in i, -int(i.get("waited_s") or 0),
                              str(i.get("id") or "")))
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
            tr.append({"action": action, "title": preview(title, 64), "ts": e.get("ts"),
                       "seq": e.get("seq")})

    # Live consoles per agent, from observed presence: an agent working WITHOUT a formal
    # claim must not render as idle — that is the exact case that makes a busy fleet look
    # asleep. A console's focus becomes the card's "on" line when no lease exists, and the
    # per-console rows are the surface that stops two sessions from unknowingly working the
    # same thing.
    sessions_by_agent, overlaps = {}, []
    for s in _activity_rows(state):
        sessions_by_agent.setdefault(s["agent"], []).append(s)
    try:
        from hub_core import presence as _presence_overlap
        # FILE crossovers: two consoles editing the same <project>/<path> right now.
        overlaps = _presence_overlap.file_overlaps(hub_app.live_sessions())
    except Exception:                                        # noqa: BLE001 - never 500 the board
        overlaps = []

    # EVERY computer a person has, each with its own check-in state. A card that reads only the
    # first machine lets a busy computer stand in for a silent one ("+1 more", "online") — the
    # second machine's own freshness never renders. A row that names a machine without kit
    # telemetry is a bare caller, not a computer: it is named under `phantoms` for tracing and
    # kept out of every device list and count.
    machines_by_agent, phantoms_by_agent = {}, {}
    try:
        from hub_core import presence as _presence_core
        now_epoch = time.time()
        for ag, seen in (hub_app.read_presence() or {}).items():
            for m in (seen or {}).get("machines") or []:
                name = str(m.get("machine") or "")
                if not name:
                    continue
                state = _presence_core.device_state(m, now_epoch)
                if not _presence_core.is_kit_machine(m):
                    phantoms_by_agent.setdefault(ag, []).append(
                        {"machine": name, "last_seen_age_s": state.get("last_seen_age_s")})
                    continue
                machines_by_agent.setdefault(ag, []).append({
                    "machine": name, "connection": state["connection"],
                    "heartbeat_age_s": state.get("heartbeat_age_s"),
                    "last_seen_age_s": state.get("last_seen_age_s"),
                    "client": m.get("client") or "",
                    "consoles": [{"session": str(x.get("id") or "")[:8],
                                  "focus": str(x.get("focus") or "")[:100],
                                  "age_s": round(now_epoch - x["at"]) if x.get("at") else None,
                                  "files": x.get("files") or []}
                                 for x in (m.get("sessions") or [])
                                 if x.get("at") and now_epoch - x["at"] <= _presence_core.SESSION_ACTIVE_S][:6]})
    except Exception:                                        # noqa: BLE001 - never 500 the board
        machines_by_agent, phantoms_by_agent = {}, {}
    order = {"online": 0, "activity-only": 1, "degraded": 2, "unverified": 3, "offline": 4}
    for rows in machines_by_agent.values():
        rows.sort(key=lambda r: (order.get(r["connection"], 5), r.get("last_seen_age_s") or 0))

    cards = []
    for ag in set(lease_by_agent) | set(trails) | set(sessions_by_agent):
        lt = _parse(last_ts.get(ag))
        idle_s = int((now - lt).total_seconds()) if lt else None
        lease = lease_by_agent.get(ag)
        sessions = sessions_by_agent.get(ag, [])
        if not lease and not sessions and (idle_s is None or idle_s > 1800):
            continue
        newest_session_age = sessions[0].get("age_s") if sessions else None
        # HEADLINE THE FRESHEST WORKING CONSOLE'S FOCUS, not whichever console reported last:
        # an idle window's stale focus reads as what the person is doing now, and is not.
        lead = activity_core.headline(sessions) or {}
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
            "focus": (lead.get("focus") if not lease else "") or "",
            "focus_console": (lead.get("name") or lead.get("session") or "") if lead else "",
            "consoles": len(sessions),
            "consoles_without_task": sum(1 for c in sessions if not c.get("has_task")),
            "age_s": lease.get("age_s") if lease else None,
            "idle_s": idle_s, "trail": trails.get(ag, []),
            "sessions": sessions[:6],
            "machines": machines_by_agent.get(ag, []),
            "phantoms": phantoms_by_agent.get(ag, []),
            "overlaps": [o for o in overlaps if ag in (o["a"]["agent"], o["b"]["agent"])][:4],
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
_STATE_CACHE = {"seq": None, "hash": None, "events": None, "state": None, "full_at": 0.0}
# The incremental fold is chain-checked page by page, but it still inherits whatever the last
# full replay produced. A periodic full replay bounds how long any drift (an upcaster changed
# under a running process, a projection bug fixed by a hot reload) can survive in memory.
FULL_REPLAY_S = 15 * 60
_STATE_LOCK = threading.RLock()
_SNAP_CACHE = {"key": None, "value": None}
# Per-phase build timings of the snapshot: the last build and the worst seen per phase in this
# process, so "the board is slow" names a phase instead of a feeling.
_SNAP_TIMINGS = {"last": {}, "worst": {}, "count": 0}
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

        stale = time.time() - float(_STATE_CACHE.get("full_at") or 0) >= FULL_REPLAY_S
        if cached_seq is None or seq < cached_seq or (seq == cached_seq and head_hash != cached_hash) \
                or (incremental and not contiguous) or stale:
            events = _events_through(store, 0, seq)
            state = project.state(events)
            _STATE_CACHE["full_at"] = time.time()
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


def _delivery_path():
    return hub_app.HUB_DIR / "delivery_projection.json"


def _delivery_key(cursor, served):
    # The artifact stamp is production's direct running identity. Include it in the key so a new
    # image can never inherit a delivery projection materialized by an older one, even when both
    # point at the same durable ledger cursor.
    return (cursor.get("seq", 0), cursor.get("hash", ""), served, hub_app._running_sha())


def read_delivery_sidecar(key):
    """The projection the backgrounder published for EXACTLY this key, else None."""
    try:
        data = json.loads(_delivery_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("block") if data.get("key") == list(key) else None


def write_delivery_sidecar(key, block):
    from hub_core import atomic
    target = _delivery_path()
    tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps({"key": list(key), "block": block, "written_at": time.time()},
                              default=str), encoding="utf-8")
    atomic.replace(tmp, target)


def _delivery_fast(state, cursor, served):
    key = _delivery_key(cursor, served)
    with _DELIVERY_LOCK:
        exact = _DELIVERY_CACHE["values"].get(key)
        if exact is not None:
            return exact, True
    # Exact sha/served_sha/tasks_closed proof is pure entity projection and belongs on the direct
    # path. Git ancestry is legacy/source-checkout enrichment only.
    provisional = delivery.direct_block(state, served=served)
    if delivery.repository_available() and not roles.runs_background_here():
        # HUB_ROLE=web: the backgrounder materializes; this process only reads what it published.
        published = read_delivery_sidecar(key)
        if published is not None:
            _cache_delivery(key, published)
            return published, True
        if roles.backgrounder_fresh(hub_app.HUB_DIR):
            # Honest unknown until the backgrounder's tick lands and wakes the stream.
            return provisional, False
        # The backgrounder is stale or absent: build ONE projection here (single-flight, below)
        # rather than serving an unmeasured leg forever.
    with _DELIVERY_LOCK:
        should_build = key not in _DELIVERY_CACHE["building"]
        if should_build:
            _DELIVERY_CACHE["building"].add(key)
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


def _built(events, state, person=None):
    try:
        from hub_core import built as _built_core
        from hub_core import presence as _presence_core
        return _built_core.by_person(events, state, hub_app.read_presence_rows(),
                                     is_service=_presence_core.is_service_identity,
                                     is_kit=_presence_core.is_kit_machine, person=person)
    except Exception:                                        # noqa: BLE001 - never 500 the board
        return []


def _built_summary(events, state):
    """The cockpit's compact form: per person, totals and the three newest completions."""
    return [{"person": r["person"], "identities": r["identities"], "totals": r["totals"],
             "authored_counts": r["authored_counts"],
             "recent": [t["title"] for t in r["tasks"][:3]]}
            for r in _built(events, state)[:12]]


@require_GET
def built_json(request):
    """GET /hub/built.json[?person=] — what each person built, derived from the ledger (completed
    tasks, releases, authored gaps/feats/ADRs/decisions/notes), machines folded into the person
    reporting from them."""
    s = hub_app.store()
    try:
        cur = s.latest_cursor()
        events, state = _projected(s, cur)
    finally:
        s.close()
    rows = _built(events, state, person=(request.GET.get("person") or "").strip() or None)
    return JsonResponse({"data": rows, "metadata": {
        "people": len(rows),
        "totals": {k: sum(r["totals"][k] for r in rows) for k in ("tasks", "deploys", "authored")},
        "how": "derived from ledger events; a machine identity folds into the agent reporting "
               "from it (raw presence rows, offline machines included)"}})


def _file_overlaps(sessions_live):
    try:
        from hub_core import presence as _presence_overlap
        return _presence_overlap.file_overlaps(sessions_live)[:12]
    except Exception:                                        # noqa: BLE001
        return []


def _crossover_block(state):
    """Every crossover pair between live consoles, for the board — the board is read by the
    operator, who is told every pair; each console is told only its own (inbox/overlap.json).
    Fail-soft."""
    try:
        rows, sigs, _titles = _crossovers(state)
    except Exception as exc:                                 # noqa: BLE001
        return {"pairs": [], "consoles": 0, "available": False, "reason": type(exc).__name__}
    pairs = [{"id": s["id"], "kind": s["kind"], "detail": s["detail"],
              "a": {k: s["a"].get(k) for k in ("agent", "machine", "session", "name", "project")},
              "b": {k: s["b"].get(k) for k in ("agent", "machine", "session", "name", "project")}}
             for s in sigs[:20]]
    return {"pairs": pairs, "count": len(sigs), "consoles": len(rows)}


def _live_side_blocks(state, lease_rows=None):
    """The addressed plane, the operational stream, and the live consoles — computed once
    per live payload so the attention rail, the cockpit cards, and the JSON endpoints all
    read ONE answer to "what is open" and "what is broken". Fail-soft: a sidecar problem
    must never take the board down."""
    asks = hub_app.question_items(state)
    # A question a machine has claimed is IN FLIGHT, not waiting on nobody (hub_core.item_claims).
    try:
        from hub_core import item_claims
        claims = item_claims.live(hub_app.HUB_DIR, roster=hub_app.roster(),
                                  grace_s=hub_app.gone_grace_s())
        for q in asks:
            if q.get("id") in claims:
                q["claimed_by"] = claims[q["id"]]
    except Exception:                                        # noqa: BLE001
        pass
    error_rows, error_meta, error_unclaimed = _errors_block()
    sessions_live = _activity_rows(state, lease_rows)
    # THE QUEUE, FOLDED: rows are occurrences, problems are what somebody fixes. The rail
    # reads unclaimed PROBLEMS; the raw rows stay one card away.
    try:
        probs, prob_meta = problems_core.read(hub_app.HUB_DIR, state)
        error_meta["problems"] = {"items": probs[:20], **prob_meta}
        error_unclaimed = [p for p in probs if p["state"] == "unclaimed"]
    except Exception as exc:                                 # noqa: BLE001 - never break a read
        error_meta["problems"] = {"items": [], "available": False,
                                  "reason": type(exc).__name__}
    # EVERY SERVICE, OBSERVED? — the one card that says whether an empty queue means anything.
    try:
        apps = hub_app.apps_config()
        health, health_meta = app_health.rows(hub_app.HUB_DIR, state, apps=apps,
                                              native=hub_app.native_slug(),
                                              native_deploy=hub_app.native_deploy(state))
        error_meta["app_health"] = {"rows": health[:30], **health_meta}
    except Exception as exc:                                 # noqa: BLE001
        error_meta["app_health"] = {"rows": [], "available": False, "reason": type(exc).__name__}
    return asks, error_rows, error_meta, error_unclaimed, sessions_live


def _activity_rows(state, lease_rows=None):
    """Every live console bound to its project and the task THAT console holds (the uniform
    session shape plus project/has_task/task_id). Fail-soft: presence must never 500 a read."""
    try:
        tasks = (state.get("by_type") or {}).get("task") or [
            e for e in (state.get("entities") or {}).values() if e.get("type") == "task"]
        return activity_core.rows(hub_app.live_sessions(),
                                  hub_app.leases() if lease_rows is None else lease_rows, tasks)
    except Exception:                                        # noqa: BLE001
        return []


@require_GET
def activity_json(request):
    """What every live console is doing: agent, machine, console name/session, project, repo,
    state, focus, recently edited files, and the task THIS console holds (or `has_task: false`
    — the work a board otherwise loses). ?agent= narrows to one agent's consoles; ?session=
    adds `no_task_for` — the projects that console is in without a task."""
    state, _ = _snapshot()
    rows = _activity_rows(state)
    agent = (request.GET.get("agent") or "").strip().lower()
    if agent:
        rows = [r for r in rows if str(r.get("agent") or "").lower() == agent]
    meta = activity_core.summary(rows)
    session = (request.GET.get("session") or request.headers.get("X-Hub-Session") or "").strip()
    if session:
        meta["session"] = session[:8]
        meta["no_task_for"] = [r.get("project") for r in activity_core.no_task_for(rows, session)]
    return JsonResponse({"data": rows, "metadata": meta})


def _consoles(state, sessions_live=None):
    """Every live console with the task IT claimed (never inferred from a directory)."""
    try:
        rows = hub_app.live_sessions() if sessions_live is None else sessions_live
        return _presence.attribute_leases(rows, hub_app.leases())
    except Exception:                                        # noqa: BLE001 - never 500 the board
        return []


def _task_activity(events, consoles):
    """{task_id: epoch} — the newest ledger event per task, raised by the live console that
    holds it (a held task is MOVING while its console acts, even between typed steps)."""
    out = {}
    for e in events or []:
        agg = str(e.get("aggregate") or "")
        if ":task:" not in agg:
            continue
        stamp = _epoch(e.get("ts"))
        if stamp:
            out[agg] = max(out.get(agg, 0.0), stamp)
    now = time.time()
    for c in consoles or []:
        tid = c.get("task_id")
        if tid and c.get("state") == "working" and c.get("age_s") is not None:
            out[tid] = max(out.get(tid, 0.0), now - float(c["age_s"]))
    return out


def _work_blocks(events, state, sessions_live, asks, error_unclaimed):
    """Task health, the attended/unattended console split, crossovers, and the operational
    attention list — one computation per payload, read by the board, the JSON API and the
    inbox alike. Each block is fail-soft: a sidecar problem degrades one card, never the board."""
    tasks = state.get("by_type", {}).get("task", [])
    consoles = _consoles(state, sessions_live)
    activity = _task_activity(events, consoles)
    out = {}
    try:
        live_projects = {c.get("project") for c in consoles if c.get("project") and not c.get("finished")}
        health = task_health.summarize(tasks, activity)
        health["hygiene"] = task_health.hygiene_items(tasks, activity, live_projects)[:12]
        health["unstarted"] = task_health.unstarted_requests(
            tasks, state.get("by_type", {}).get("run", []), hub_app.leases())[:12]
        out["task_health"] = health
    except Exception:                                        # noqa: BLE001
        out["task_health"] = None
    split = _presence.split(consoles)
    out["sessions"] = {k: v[:24] for k, v in split.items()}
    out["sessions"]["counts"] = {k: len(v) for k, v in split.items()}
    try:
        out["crossovers"] = [overlap.public(sig) for sig in overlap.signals(consoles, tasks)][:24]
    except Exception:                                        # noqa: BLE001
        out["crossovers"] = []
    out["needs_attention"] = _attention_payload(state, consoles, activity, asks, error_unclaimed)
    return out


def hub_client_version():
    """The version of the worker client this hub serves: a digest of hub_core/client.py and
    the modules its verbs import. Clients send theirs on every call (X-Hub-Client-Version)."""
    cached = _CLIENT_VERSION.get("value")
    if cached is not None:
        return cached
    import hashlib
    from pathlib import Path
    import hub_core as _hc
    root = Path(_hc.__file__).parent
    digest = hashlib.sha256()
    for name in ("client.py", "checkpoints.py", "verifier.py"):
        try:
            digest.update((root / name).read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            pass
    _CLIENT_VERSION["value"] = digest.hexdigest()[:12]
    return _CLIENT_VERSION["value"]


_CLIENT_VERSION = {}


def _attention_payload(state, consoles, activity, asks, error_unclaimed):
    """Gather the operational attention context and build the list (hub_core.attention)."""
    from hub_core import agent_auth
    ctx = {"now": time.time(), "operator": _operator_agent(), "sources": {},
           "tasks": state.get("by_type", {}).get("task", []), "activity": activity,
           "runs": state.get("by_type", {}).get("run", []), "sessions": consoles,
           "questions": asks, "errors_unclaimed": error_unclaimed,
           "hub_client": hub_client_version()}
    for name, read in (("leases", hub_app.leases), ("presence", hub_app.read_presence),
                       ("credentials", lambda: agent_auth.CredentialRegistry(
                           hub_app.HUB_DIR).list_public())):
        try:
            ctx[name] = read()
        except Exception as exc:                             # noqa: BLE001
            ctx["sources"][name] = "unreadable: %s" % type(exc).__name__
    ctx["presence"] = {k: v for k, v in (ctx.get("presence") or {}).items()
                       if not _presence.is_service_identity(k)}
    try:
        return attention_core.build(hub_app.HUB_DIR, ctx)
    except Exception as exc:                                 # noqa: BLE001
        return {"verdict": "attention could not be computed (%s)" % type(exc).__name__,
                "items": [], "counts": {"total": 0}, "sources": {"attention": "failed"}}


def _held_rows(state):
    from hub_core import held as _held
    try:
        rows = _held.queue(state, time.time())
        for row in rows:
            row["detail"] = _held.detail(row)
        return rows[:20]
    except Exception:                                        # noqa: BLE001 - never break a read
        return []


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
    disk = _host_disk()
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
        "asks_stuck": inbox_core.stuck_summary(asks),
        # THE OPERATIONAL ERROR STREAM, bar-annotated, with the shape a reader needs
        # (trend, sources, coverage) — the failures the ledger audit cannot see.
        "errors": error_rows[:40],
        "error_log": error_meta,
        # EVERY LIVE CONSOLE, flat: the surface that stops two sessions from unknowingly
        # working the same thing. The per-agent fleet cards roll these up.
        "sessions_live": sessions_live[:12],
        # THE AGENTS' OWN FEED: what they did, in their words, newest first — rides the same
        # push tick so a posted fix appears on every open board within a second.
        "updates": updates.read(hub_dir, 40),
        # THE PROMOTION LANE: finished work held back from live, oldest first (hub_core.held).
        "held": _held_rows(state),
        "file_overlaps": _file_overlaps(sessions_live),
        "distribution": hub_app.distribution_report()[0],
        "built": _built_summary(events, state),
        "crossovers": _crossover_block(state),
        # The hub's own drive: always reported with its numbers, a condition only below
        # the thresholds (hub_core.hostdisk).
        "host_disk": disk,
        "attention": _attention(state, audit, inflight, adher, deliv,
                                asks=asks, error_unclaimed=error_unclaimed, disk=disk),
        "telemetry": telemetry.read_aggregate(hub_dir),
        "cost": cost.cost_block(hub_dir, state),
        "wip": hub_app.wip_status(len(inflight)),
        **_work_blocks(events, state, sessions_live, asks, error_unclaimed),
    }


def _sweep_leases():
    """A task nobody holds is handed back HERE, on the board's own read path, not by whatever
    died holding it (hub_core.lease_sweep). Throttled inside; never breaks a read."""
    try:
        from . import lease_sweep
        lease_sweep.sweep()
    except Exception:                                        # noqa: BLE001
        import logging
        logging.getLogger("hub.lease_sweep").warning("sweep pass failed", exc_info=True)


def _snapshot(served=None):
    _sweep_leases()
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
               updates.stamp(hub_app.HUB_DIR), git_head, int(time.time() // 5))
        if _SNAP_CACHE["key"] == key:
            return _SNAP_CACHE["value"]
        timings, mark = {}, [time.perf_counter()]

        def _tick(phase):
            now = time.perf_counter()
            timings[phase] = round((now - mark[0]) * 1000, 1)
            mark[0] = now

        events, state = _projected(s, cur)
        _tick("fold")
        # Realtime lease/telemetry refreshes must not repeatedly pay for a repository audit whose
        # inputs did not change. Structural audit truth changes with the ledger or build identity;
        # cache on exactly those inputs and keep the five-second live cockpit refresh lightweight.
        audit_key = (cur["seq"], cur["hash"], served, git_head)
        if _AUDIT_CACHE["key"] == audit_key:
            audit = _AUDIT_CACHE["value"]
        else:
            audit = hub_app.run_audit(s, served=served)
            _AUDIT_CACHE["key"], _AUDIT_CACHE["value"] = audit_key, audit
        _tick("audit")
        build = hub_app.build_meta(served, state=state)
        _tick("build")
        last = events[-1] if events else {}
        inflight = _inflight(state)
        lease_rows = hub_app.leases()           # ONE lease read shared by every block below
        adher = adherence.score(events, state, leases=inflight)
        _tick("leases_adherence")
        # Repository ancestry can require many Git calls. First paint uses the truthful cached or
        # provisional view; the completed materialization publishes its own live patch.
        # Until then, an unmeasured leg stays an honest unknown rather than silent green.
        deliv, _ = _delivery_fast(state, cur, served)
        _tick("delivery")
        hub_dir = hub_app.HUB_DIR
        side_asks, side_error_rows, side_error_meta, side_unclaimed, side_sessions = \
            _live_side_blocks(state, lease_rows)
        _tick("side_blocks")
        side_disk = _host_disk()
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
            "asks_stuck": inbox_core.stuck_summary(side_asks),
            "errors": side_error_rows[:40],
            "error_log": side_error_meta,
            "sessions_live": side_sessions[:12],
            "updates": updates.read(hub_dir, 40),
            "held": _held_rows(state),
            "file_overlaps": _file_overlaps(side_sessions),
            "distribution": hub_app.distribution_report()[0],
            "built": _built_summary(events, state),
            "crossovers": _crossover_block(state),
            "host_disk": side_disk,
            "attention": _attention(state, audit, inflight, adher, deliv,
                                    asks=side_asks, error_unclaimed=side_unclaimed,
                                    disk=side_disk),
            # Task health (moving / ready-to-close / stalled / orphaned), the attended vs
            # unattended console split, crossovers between consoles, and the operational
            # "needs attention" list — hub_core.task_health / presence / overlap / attention.
            **_work_blocks(events, state, side_sessions, side_asks, side_unclaimed),
            # Cost/latency aggregated FROM the OTLP GenAI lines workers emit — the standard's
            # aggregate, never a bespoke side-channel field.
            "telemetry": telemetry.read_aggregate(hub_dir),
            "cost": cost.cost_block(hub_dir, state),
            # The adaptive WIP ceiling the claim seam enforces, so the cockpit shows the fleet's
            # own concurrency budget rather than the operator guessing at it.
            "wip": hub_app.wip_status(len(inflight)),
        }
        _tick("live_blocks")
        snap = projections.hub_snapshot(state, build=build, audit=audit, live=live)
        _tick("projection")
        timings["total"] = round(sum(float(v) for v in timings.values()), 1)
        live["timings_ms"] = timings
        _SNAP_TIMINGS["last"] = dict(timings)
        for phase, value in timings.items():
            _SNAP_TIMINGS["worst"][phase] = max(float(_SNAP_TIMINGS["worst"].get(phase) or 0),
                                                float(value))
        _SNAP_TIMINGS["count"] = int(_SNAP_TIMINGS["count"]) + 1
        maintain = hub_app.maintain_action()
        if maintain:
            snap["maintain"] = maintain
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


# -- Input-state memo for the heavy JSON reads ------------------------------------------------
#
# hub.json and next.json were rebuilt per request: open the store (sqlite connect, ledger lock,
# schema check), run `git rev-parse`, fingerprint leases, and then -- for hub.json -- hash and
# re-serialize the whole snapshot BEFORE the ETag could even be compared. A 304 saved bytes,
# never CPU, and a supervisor or several tabs asking the same question paid for it every time.
#
# The memo keys a view's BYTES on a stats-only fingerprint of what the view is built from, taken
# without opening anything, and serves those bytes (or a 304) while the fingerprint holds and the
# build is younger than the view's reuse cap. The cap bounds everything the stamps do not model:
# time-only boundaries (not_before timers, rolling windows, a lease expiring with no file moving)
# and a code landing that moves the git head. hub.json's cap equals the snapshot memo's own
# five-second time bucket, so the memo adds no staleness the snapshot did not already allow.
#
# Two rules keep it honest. A build is remembered only if its inputs did not move WHILE it ran --
# otherwise the bytes describe a state that no longer matches the key. And any stamp that cannot
# be read yields None, which means "build", never "assume unchanged".
MEMO_REUSE_S = {"hub": 5.0, "next": 10.0}
_VIEW_MEMO: dict = {}
_VIEW_MEMO_LOCK = threading.Lock()


def _view_inputs():
    """Stats-only fingerprint of the stores the heavy reads are built from.

    The ledger contributes its SIZE, not its mtime: opening the store restamps the file's mtime,
    while every append (and every heal) changes its size. Leases, presence, the error sidecars
    and worker telemetry change with no ledger event, so each contributes its own stamp."""
    import os as _os
    try:
        ledger = (hub_app.HUB_DIR / "events.jsonl").stat().st_size
        claims = ()
        if hub_app.CLAIMS.exists():
            claims = tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                                  for e in _os.scandir(hub_app.CLAIMS) if e.name.endswith(".json")))
        return (ledger, claims, hub_app.presence_stamp(), errorlog.stamp(hub_app.HUB_DIR),
                _telemetry_fp())
    except OSError:
        return None


def _memo_hit(name, extra, sent_etag=None):
    """Return (ready response or None, memo key or None) for one view at the current inputs."""
    inputs = _view_inputs()
    if inputs is None:
        return None, None
    key = (name, extra, inputs)
    with _VIEW_MEMO_LOCK:
        hit = _VIEW_MEMO.get(key)
    if hit is None or time.monotonic() - hit[0] >= MEMO_REUSE_S[name]:
        return None, key
    _at, etag, body = hit
    if etag and sent_etag == etag:
        resp = HttpResponse(status=304)
    else:
        resp = HttpResponse(body, content_type="application/json")
    if etag:
        resp["ETag"] = etag
    resp["X-Hub-Memo"] = "hit"
    return resp, key


def _memo_store(key, resp, etag=None):
    """Keep a 200 build only if its inputs did not move while it was built."""
    if key is None or getattr(resp, "status_code", 0) != 200:
        return
    if key[2] != _view_inputs():
        return
    with _VIEW_MEMO_LOCK:
        if len(_VIEW_MEMO) >= 32:
            _VIEW_MEMO.clear()
        _VIEW_MEMO[key] = (time.monotonic(), etag, resp.content)


def hub_json(request):
    served = request.GET.get("served")
    sent = request.headers.get("If-None-Match")
    # Built once per input state: a supervisor fleet re-grounding every heartbeat must not fold
    # the ledger on every read (MEMO_REUSE_S bounds how stale a reused build may be).
    hit, key = _memo_hit("hub", served, sent)
    if hit is not None:
        return hit
    _, snap = _snapshot(served)
    wire = _wire_snapshot(snap)
    # 304 on the caller's last tag: a reconnect re-ground or supervisor read gets an empty body
    # when nothing changed, instead of the full snapshot every time.
    resp = _conditional(request, _etag(wire), lambda: wire)
    _memo_store(key, resp, resp["ETag"])
    return resp


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
    from . import veil
    if not veil.veil_for(request).visible(ent):
        raise Http404("no entity %s" % eid)      # omission: exactly the unknown-entity answer
    flags = state.get("flags", {}).get(eid, {})
    data = {**ent, **flags}
    if type == "task":
        data = {**_annotated_tasks(state, [ent])[0], **flags}
        # Whether it can be taken — the same classification as next.json, read off the live
        # lease: `stale_reclaim` tells an unattended launcher "a run that ended left this in
        # progress" without attempting a claim. The fencing token never leaves the claims dir.
        lease = next((row for row in hub_app.leases() if row.get("task") == eid), None)
        data["readiness"] = flow.classify(ent, flags, lease)
    # The evidence ladder is ASKED FOR, never automatic: it can put several questions to git,
    # and an ordinary record read must not pay for that. A failure inside it annotates the
    # record instead of taking it down -- the entity still answers, and the failure is named.
    if type == "task" and str(request.GET.get("lineage") or "").strip() in ("1", "true", "yes"):
        from hub_core import lineage as _lineage
        try:
            data["lineage"] = _lineage.ladder(ent, state, hub_app.commit_resolver(),
                                              hub_app.HUB_DIR / "ancestry.json")
        except Exception as exc:                             # noqa: BLE001
            data["lineage"] = {"hops": [], "complete": False,
                               "error": "%s: %s" % (exc.__class__.__name__, str(exc)[:160])}
    return JsonResponse({"data": data})


def _annotated_tasks(state, tasks):
    """Task rows with holder / responder / pushed / handed_back / deployed / ci_problem."""
    try:
        live = {c.get("session") for c in hub_app.live_sessions() if not c.get("finished")}
    except Exception:                                        # noqa: BLE001
        live = set()
    try:
        errors, _meta = errorlog.read(hub_app.HUB_DIR)
    except Exception:                                        # noqa: BLE001
        errors = []
    return task_rows.annotate(tasks, leases=hub_app.leases(),
                              runs=state.get("by_type", {}).get("run", []),
                              live_sessions=live, error_rows=errors)


@require_GET
def project_tasks_json(request, slug):
    """GET /hub/project/<slug>/tasks.json — one project's open tasks plus those finished in the
    last 14 days, each annotated, for a consuming app to poll. Answers 304 on a matching
    If-None-Match: the validator moves only when something a reader shows moves."""
    state, _ = _snapshot()
    rows = _annotated_tasks(state, state.get("by_type", {}).get("task", []))
    payload = task_rows.feed(rows, slug)
    tag = '"%s"' % payload.pop("etag")
    if request.headers.get("If-None-Match") == tag:
        resp = HttpResponse(status=304)
    else:
        resp = JsonResponse({"data": payload})
    resp["ETag"] = tag
    resp["Cache-Control"] = "no-cache"
    return resp


@require_GET
def attention_json(request):
    """GET /hub/attention.json — the operational needs-attention list with owner, fix, values
    and age. Ages are not in the validator (a reader ticks them), so a quiet board answers 304."""
    state, snap = _snapshot()
    payload = (snap.get("live") or {}).get("needs_attention") or {}
    tag = '"%s"' % attention_core.etag(payload)
    if request.headers.get("If-None-Match") == tag:
        resp = HttpResponse(status=304)
    else:
        resp = JsonResponse({"data": payload})
    resp["ETag"] = tag
    resp["Cache-Control"] = "no-cache"
    return resp


@require_GET
def consoles_json(request):
    """GET /hub/consoles.json — every live console (attended, unattended, finished recap) with
    what it is doing and the task it claimed, plus every crossover pair. ``?session=`` returns
    only the signals addressed to that console, phrased from its side."""
    state, _ = _snapshot()
    consoles = _consoles(state)
    tasks = state.get("by_type", {}).get("task", [])
    sigs = overlap.signals(consoles, tasks)
    split = _presence.split(consoles)
    data = {"attended": split["attended"], "unattended": split["unattended"],
            "finished": split["finished"],
            "counts": {k: len(v) for k, v in split.items()},
            "crossovers": [overlap.public(sig) for sig in sigs]}
    session = (request.GET.get("session") or "").strip()[:8]
    if session:
        agent = next((c.get("agent") for c in consoles if c.get("session") == session), "")
        data["addressed"] = [it for it in overlap.items_for_agent(sigs, agent)
                             if it.get("session") == session]
    return JsonResponse({"data": data})


def components_json(request):
    """Standard components and app skeletons, resolved on THIS read (hub_core.catalog).

    ``?kind=component|skeleton`` narrows it (``template`` is accepted as an older spelling of
    skeleton). A skeleton's ``applied`` rows carry each component's CURRENT get/entry/delivery,
    ``missing`` names anything it applies that is not a registered component, and
    ``order_problems`` names a dependency that is not a component or a cycle — reported, never
    guessed around."""
    from hub_core import catalog
    kind = catalog.canonical_kind(request.GET.get("kind"))
    if kind not in ("", "component", "skeleton"):
        return JsonResponse({"errors": [{"code": "unknown_kind", "kind": kind,
                                         "allowed": ["component", "skeleton"]}]}, status=400)
    state, _ = _snapshot()
    data = catalog.catalog(state, kind)
    meta = {"kind": kind or "all"}
    if "components" in data:
        meta["components"] = len(data["components"])
    if "skeletons" in data:
        meta["skeletons"] = len(data["skeletons"])
        meta["skeletons_with_gaps"] = sum(1 for s in data["skeletons"]
                                          if s["missing"] or s["order_problems"])
    return JsonResponse({"data": data, "metadata": meta})


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
    """DISCOVER -- built once per input state and reused for at most MEMO_REUSE_S["next"].

    A supervisor or a fleet of worker beacons asks this same question every heartbeat, and each
    ask replayed the whole ledger. A lease that EXPIRES moves no file, so the reuse cap is what
    bounds how late a stale reclaim appears; a claim or release rewrites the claims directory
    and invalidates the memo at once. A 429 (saturated) answer is never remembered."""
    try:
        n = max(1, min(int(request.GET.get("n", "1")), 50))
    except ValueError:
        n = 1
    # WHO IS ASKING is part of the answer (work given to somebody else, machine affinity, the
    # unattended lane), so it is part of the memo key: one caller's filtered rail is never
    # served to another.
    who = offer.caller(request.GET, request.headers)
    lane = request.GET.get("unattended") in ("1", "true")
    hit, key = _memo_hit("next", (n, who.get("agent"), who.get("machine"),
                                  bool(who.get("unattended")), lane))
    if hit is not None:
        return hit
    resp = _next_json_build(n, who, lane)
    _memo_store(key, resp)
    return resp


def _next_json_build(n, who, unattended_lane=False):
    """DISCOVER: ranked unblocked tasks without a live lease, including stale reclaims.

    A worker's entrypoint — pull the top task, claim it, and complete the real operation. A receipt
    accompanies completion only when the task declared a transient critical probe. The board
    sequences the fleet by readiness and priority; there is no dedicated leader. Non-ready todos
    are returned SEPARATELY with the reason, never silently withheld: a worker handed a stub
    stalls trying to write its own acceptance."""
    # A CLI worker is a fresh process for every pull, so the snapshot memo is cold every time.
    # DISCOVER needs only the folded task graph — building the audit, telemetry, cost and DAG
    # blocks here would put the whole cockpit on the critical path of every claim.
    _sweep_leases()        # an abandoned in-progress task is re-offered with its reason
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
    ready, needs_spec, withheld = [], [], {}
    # WHO IS ASKING decides what is offered (hub_core.offer, the same rule `take` applies):
    # work given to somebody else by name, work only another machine can do, and an
    # unattended run's escalation that is a person's. A withheld row is COUNTED by reason in
    # the metadata, never silently dropped -- a rail that hides work reads as an empty one.
    now_s = time.time()
    for t in tasks:
        cls = flow.classify(t, flags.get(t["id"], {}), lease_map.get(t["id"]))
        if cls["available"]:
            why = offer.withheld(t, agent=who["agent"], machine=who["machine"],
                                 unattended=who["unattended"], now=now_s)
            if why:
                key = why.split(":")[0].split(" (")[0]
                withheld[key] = withheld.get(key, 0) + 1
                continue
            ready.append(dict(t, flow_state=cls["state"], flow_reason=cls["reason"],
                              stale_reclaim=cls["stale_reclaim"]))
        elif cls["state"] == "needs_spec":
            needs_spec.append(dict(t, needs=cls["reason"], flow_state=cls["state"]))
    from hub_core import schedule
    busy_touches = set()
    for lease in live_leases:
        busy_touches.update(schedule.normalized_touches(entities.get(lease.get("task"), {})))
    ready = schedule.order_ready(ready, flags, busy_touches=busy_touches)
    if unattended_lane:
        # THE UNATTENDED LANE: what a supervisor may hand an unattended worker without asking —
        # tasks explicitly marked unattended, P0-P2 only (P3 is a wish list), never a decision
        # (a person's call). Work handed back by a run that ended unfinished is todo again and
        # re-offered here like any other; one a live run is on is not.
        running = task_health.live_run_subjects(state["by_type"].get("run", []))
        ready = [t for t in ready
                 if task_health.is_unattended(t) and not task_health.is_decision(t)
                 and str(t.get("priority") or "") in ("P0", "P1", "P2")
                 and t["id"] not in running]

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
    rows = [dict(t, available=True) for t in _annotated_tasks(state, ready[:n])]
    return JsonResponse({"data": rows, "needs_spec": needs_spec[:n], "snoozed": snoozed[:n],
                         "metadata": {"available": len(ready), "unblocked": len(ready),
                                      "ready": len(ready), "needs_spec": len(needs_spec),
                                      "snoozed": len(snoozed),
                                      "snoozed_next": snoozed[0]["not_before"] if snoozed else None,
                                      "withheld": withheld, "caller": who,
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
            "to": str(ent.get("to") or "").lower(),
            "title": str(ent.get("title") or ""),
            "context": str(ent.get("body_md") or ""),
            "open": "open" in tags,
            "withdrawn": "withdrawn" in tags,
            "answered": bool(reply),
            "answer_id": (reply or {}).get("id", ""),
            "answer": answer_body,
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
    # Anything still needing a human ahead of what is closed: waiting-for-an-answer, then
    # answered-but-not-yet-delivered, then done. Open rows are LONGEST WAIT FIRST (an unknown
    # age last, never first) — newest-first put the ask that had waited two days at the bottom
    # of the list, behind twenty fresher ones. Closed rows stay newest first.
    rows.sort(key=lambda r: r["at"], reverse=True)
    rows.sort(key=lambda r: (r["waiting_seconds"] is None, -(r["waiting_seconds"] or 0))
              if r["open"] and not r["answered"] else (True, 0))
    rows.sort(key=lambda r: (r["acked"], r["answered"]))
    for r in rows:
        r["stuck"] = bool(r["open"] and not r["answered"]
                          and (r["waiting_seconds"] or 0) >= inbox_core.ASK_STUCK_S)
    stuck_rows = [r for r in rows if r["stuck"]]

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
        # STUCK: open, unanswered, and waiting past HUB_ASK_STUCK_S. The count, the threshold it
        # was measured against, and the ids — a stuck ask that only a detector can see is not
        # surfaced.
        "stuck": len(stuck_rows),
        "stuck_after_seconds": inbox_core.ASK_STUCK_S,
        "stuck_ids": [r["id"] for r in stuck_rows][:20],
        "unstick_after_seconds": inbox_core.ASK_UNSTICK_S,
        "replied_count": len(answered_waits),
        "lanes": lanes[:6],
        "days": days}})


def _operator_agent() -> str:
    """The identity questions are addressed to and answers come from. One name, settable —
    HUB_OPERATOR_AGENT in settings or the environment."""
    import os as _os
    return str(hub_app._dj_setting("HUB_OPERATOR_AGENT")
               or _os.environ.get("HUB_OPERATOR_AGENT") or "operator").strip().lower()


def _inbox_reader(request):
    """(agent, machine, session) for an inbox read. The session and machine come from the
    query or the same X-Hub-* headers every write carries, so a console that names itself is
    delivered its own mail — and mail for a console that has ended falls through to it."""
    agent = (request.GET.get("agent") or "").strip().lower()
    machine = (request.GET.get("machine") or request.headers.get("X-Hub-Machine") or "")
    session = (request.GET.get("session") or request.headers.get("X-Hub-Session") or "")
    return agent, machine.strip().lower()[:120], session.strip()[:64]


def _inbox_kwargs(request, machine, session):
    try:
        live = hub_app.live_sessions() if session else None
    except Exception:                                        # noqa: BLE001 - never break delivery
        live = None
    return {"machine": machine, "session": session, "live": live,
            "human_gate": hub_app.human_gate(), "gate_satisfied": hub_app.gate_satisfied(),
            "visible": _veil_visible(request)}


def _veil_visible(request):
    """The caller's visibility filter over addressed items (the contributor veil), or None
    when the caller sees everything."""
    try:
        from . import veil as _veil
        view = _veil.veil_for(request)
    except Exception:                                        # noqa: BLE001
        return None
    return None if view.open else view.visible


@require_GET
def inbox_json(request):
    """What is addressed to ?agent= right now: messages to it, the questions it should see
    (addressed to it; for the operator, every unaddressed one; for everyone, any ask stuck past
    the unstick window), directives aimed at it, the answer to its own question, and the lanes
    that deliver themselves (decisions, rotting tasks, attention, crossovers). Name a console
    (?session= or X-Hub-Session) to receive only that console's mail — plus mail for a console
    of yours that has since ended."""
    agent, machine, session = _inbox_reader(request)
    if not agent:
        return JsonResponse({"errors": [{"code": "need_agent", "msg": "pass ?agent="}]}, status=400)
    state, snap = _snapshot()
    data = _addressed(state, snap, agent, **_inbox_kwargs(request, machine, session))
    if request.GET.get("expired") in ("1", "true", "yes"):
        # LISTED, NOT DELIVERED: expired mail rides beside the addressed set, outside anything
        # a waiter fingerprints, so asking to see it can never push it into a console.
        try:
            data = dict(data) if isinstance(data, dict) else {"items": data}
            data["expired"] = inbox_core.expired_message_items(state, agent, machine)
        except Exception:                                    # noqa: BLE001 - never break the inbox
            data["expired"] = []
    return JsonResponse({"data": data,
                         "metadata": {"agent": agent, "operator": _operator_agent(),
                                      "session": session, "machine": machine}})


def _deciders():
    from . import hub_write
    return hub_write._deciders()


def _addressed(state, snap, agent, **kwargs):
    """Everything addressed to ``agent`` right now: its messages, questions, directives and
    answers (inbox_core.snapshot: session routing, human gates, the veil, offer receipts), plus
    the lanes that deliver THEMSELVES —

    * ``decision``   open decisions, to whoever decides (HUB_DECIDERS, default the operator)
    * ``task-stall`` rotting tasks and unstarted unattended requests, to the operator
    * ``attention``  operational conditions past their patience, to their owner / the operator
    * ``overlap``    crossovers with another console, to each attended console's agent, once
                     per side (``POST /hub/api/overlap-seen`` records delivery).

    The self-delivering lanes pass through the same visibility filter as the addressed set, so
    the contributor veil hides them exactly as it hides a message."""
    operator = _operator_agent()
    # The operator also receives what only a person can clear: seats gone silent, drift.
    base = inbox_core.snapshot(state, agent, operator, _operator_extra(agent),
                               hub_dir=hub_app.HUB_DIR, **kwargs)
    items = list(base.get("items") or [])
    extra = []
    live = (snap or {}).get("live") or {}
    if agent in _deciders():
        extra += inbox_core.decision_items(state)
    if agent == operator:
        health = live.get("task_health") or {}
        extra += list(health.get("hygiene") or []) + list(health.get("unstarted") or [])
    extra += attention_core.items_for(agent, live.get("needs_attention"))
    # Fresh unclaimed PROBLEMS this agent owns (and, for the operator, anything unclaimed too
    # long) — the folded queue delivers itself like everything else.
    try:
        extra += problems_core.items_for_agent(hub_app.HUB_DIR, state, agent, operator,
                                               apps=hub_app.apps_config(),
                                               live_rows=hub_app.live_sessions())
    except Exception:                                        # noqa: BLE001
        pass
    # Crossovers this side has not been told: with a named console, only that console's;
    # otherwise every attended console of the agent. Problem claims count as shared work.
    try:
        titles = _live_titles(state)
        sigs = overlap.signals(_consoles(state), state.get("by_type", {}).get("task", []),
                               titles=titles, claims=problems_core.read_claims(hub_app.HUB_DIR))
        mine = overlap.items_for_agent(sigs, agent)
        session = str(kwargs.get("session") or "")[:8]
        if session:
            mine = [i for i in mine if i.get("session") == session]
        extra += overlap.unseen(hub_app.HUB_DIR, mine)
    except Exception:                                        # noqa: BLE001
        pass
    visible = kwargs.get("visible")
    if visible is not None:
        try:
            extra = [i for i in extra if visible(i)]
        except Exception:                                    # noqa: BLE001 - fail closed
            extra = []
    items += extra
    return {"items": items, "fingerprint": inbox_core.fingerprint(items), "count": len(items)}


def _operator_extra(agent):
    """Computed items only the operator receives: seats gone silent and persistent drift."""
    if (agent or "").strip().lower() != _operator_agent():
        return []
    return hub_app.distribution_inbox_items()


def _addressee(request):
    """Which console/computer is asking — a pinned directive is delivered only there."""
    return {"machine": (request.GET.get("machine") or "").strip().lower()[:120],
            "session": (request.GET.get("session") or "").strip()[:64]}


def _live_titles(state):
    return {eid: str(e.get("title") or "") for eid, e in (state.get("entities") or {}).items()
            if isinstance(e, dict) and e.get("type") == "task"}


def _crossovers(state):
    """(consoles, signals) over the live fleet — computed once on the hub."""
    titles = _live_titles(state)
    rows = overlap_core.consoles(hub_app.live_sessions(), hub_app.leases(), titles)
    claims = problems_core.read_claims(hub_app.HUB_DIR)
    return rows, overlap_core.signals(rows, titles=titles, claims=claims), titles




@require_GET
def inbox_wait(request):
    """Long-poll: return the moment something is addressed to ?agent=.

    This is the mechanism behind "asking reaches the answerer in about a second": a
    supervisor loop spends the sleep it was already doing blocked here, so an ask raises a
    notification on the answerer's side without anybody watching a browser tab, and the
    answer lands back on the asker's side the same way. Bounded hard — never longer than
    MAX_WAIT_S, never more than MAX_WAITERS at once (past the ceiling it answers from the
    caller's last projection with degraded=true: an honest poll, not a starved server)."""
    agent, machine, session = _inbox_reader(request)
    if not agent:
        return JsonResponse({"errors": [{"code": "need_agent", "msg": "pass ?agent="}]}, status=400)
    known = (request.GET.get("fp") or "")[:64]
    try:
        timeout = float(request.GET.get("wait") or inbox_core.MAX_WAIT_S)
    except (TypeError, ValueError):
        timeout = inbox_core.MAX_WAIT_S
    tier = getattr(request, "hub_tier", "") or ""

    def snapshot_fn(who):
        state, snap = _snapshot()
        return _addressed(state, snap, who, **_inbox_kwargs(request, machine, session))

    def signal_fn():
        # The cheap fingerprint of everything the snapshot depends on — the wait loop must
        # not fold the whole ledger per poll tick. The presence stamp rides it BUCKETED
        # (session routing, crossovers and attention read presence, and presence moves on
        # every request), leases ride it because a claim moves no ledger head, and problems and
        # crossovers change with NO ledger event, so the error stream's stamp and a minute bucket
        # (problem freshness) ride it too. "" on error
        # never equals a real signal, so a failed read degrades to always-fold rather than
        # skipping a real change.
        try:
            s = hub_app.store()
            try:
                seq = s.latest_cursor().get("seq") or 0
            finally:
                s.close()
        except Exception:                                    # noqa: BLE001
            return ""
        try:
            stamp = hub_app.presence_stamp()
        except Exception:                                    # noqa: BLE001
            stamp = None
        try:
            leases_fp = _leases_fp()
        except Exception:                                    # noqa: BLE001
            leases_fp = ""
        try:
            errors_stamp = errorlog.stamp(hub_app.HUB_DIR)
        except Exception:                                    # noqa: BLE001
            errors_stamp = ""
        return inbox_core.change_signal(seq, stamp, leases_fp, errors_stamp,
                                        int(time.time() // 60))

    payload = inbox_core.wait(agent, known, timeout, snapshot_fn=snapshot_fn,
                              signal_fn=signal_fn, key=(agent, machine, session, tier))
    return JsonResponse({"data": payload,
                         "metadata": {"agent": agent, "operator": _operator_agent(),
                                      "session": session, "machine": machine,
                                      "max_wait_s": inbox_core.MAX_WAIT_S}})


@require_GET
def perf_json(request):
    """Where the time goes: per-route latency (worst process window in the last hour), the
    slow-route verdict, the snapshot's per-phase build timings, and which process answered (its
    role, the backgrounder's clock, the startup prewarm cost).

    ``?profile=snapshot`` runs ONE ordinary snapshot build under Python's thread-local profiler
    on this request's thread and returns the 30 costliest functions (locations and durations
    only — never arguments, locals or entity bodies). It is CPU on the serving process, so it
    needs a credential: the shared-root token or a scoped credential holding ``perf:profile``.
    Its own request is excluded from the route samples it would otherwise distort."""
    if request.GET.get("profile"):
        from . import hub_write
        auth, _problem = hub_write._authenticate(request)
        if not auth or not auth.allows("perf:profile"):
            return JsonResponse({"errors": [{"code": "insufficient_scope",
                                              "required": "perf:profile"}]}, status=403)
        if request.GET["profile"] != "snapshot":
            return JsonResponse({"errors": [{"code": "unknown_profile"}]}, status=400)
        import pstats
        import profile as thread_profile
        from pathlib import Path
        _SNAP_CACHE["key"] = None                  # profile a real build, not a memo hit
        profiler = thread_profile.Profile(timer=time.perf_counter)
        wall, cpu = time.perf_counter(), time.thread_time()
        _, snap = profiler.runcall(_snapshot, request.GET.get("served"))
        elapsed_ms = round((time.perf_counter() - wall) * 1000, 1)
        cpu_ms = round((time.thread_time() - cpu) * 1000, 1)
        functions = []
        for (filename, line, name), (_prim, calls, own, cumulative, _callers) in sorted(
                pstats.Stats(profiler).stats.items(), key=lambda item: -item[1][3])[:30]:
            path = Path(filename)
            try:
                location = path.relative_to(hub_app.BASE_DIR.parent).as_posix()
            except ValueError:
                location = path.name
            functions.append({"file": location, "line": line, "function": name,
                              "calls": calls, "own_ms": round(own * 1000, 1),
                              "cumulative_ms": round(cumulative * 1000, 1)})
        return JsonResponse({"data": {"profile": "snapshot", "elapsed_ms": elapsed_ms,
                                      "thread_cpu_ms": cpu_ms, "functions": functions,
                                      "snapshot_ms": (snap.get("live") or {}).get("timings_ms"),
                                      "cursor": (snap.get("live") or {}).get("cursor")}})
    import os as _os
    from .middleware import route_timings, slow_routes
    routes = route_timings()
    slow = slow_routes(routes)
    return JsonResponse({"data": {
        "routes": routes,
        "routes_verdict": (
            "no recent route observations" if not routes else
            "every observed route stayed under the slow-route threshold, in every window"
            if not slow else
            "%d route(s) crossed the slow-route threshold in at least one process window "
            "(worst-window p95, not an average): %s" % (len(slow), ", ".join(slow))),
        "slow_routes": slow,
        "snapshot": {"last_ms": dict(_SNAP_TIMINGS["last"]),
                     "worst_ms": dict(_SNAP_TIMINGS["worst"]),
                     "builds": int(_SNAP_TIMINGS["count"])},
        # Which process answered, in which role, how fresh the backgrounder's clock is, and
        # what the startup prewarm cost -- a slow response is traced to a process, not "the hub".
        "process": dict(roles.process_info(hub_app.HUB_DIR), pid=_os.getpid()),
        "prewarm": prewarm.status(),
    }, "process": roles.process_info(hub_app.HUB_DIR), "prewarm": prewarm.status(),
        "metadata": {"window_s": 3600, "long_polls_exempt": ["/hub/inbox/wait", "/hub/live/events"],
                    "fields": "p50_worst_ms/p95_worst_ms are the worst single process window's "
                              "percentiles; count is a true total"}})


@require_GET
def tiers_json(request):
    """Who is which visibility tier, and the declared facets (ids and labels only)."""
    from hub_core import veil as core
    from . import veil
    doc = core.registry(veil.registry_path())
    return JsonResponse({"data": core.read_tiers(hub_app.HUB_DIR), "metadata": {
        "active": veil.active(), "tiers": list(core.TIERS),
        "facets": [{"id": f["id"], "label": f["label"], "tiers": f["tiers"]}
                   for f in doc.get("facets") or []],
        **({"broken": doc["broken"]} if doc.get("broken") else {})}})


@require_GET
def veil_audit_json(request):
    """Render every veiled read route AS A CONTRIBUTOR, in process, and scan what would be
    served for every hidden term. A hit is a leak: the route handed a contributor a word the
    veil exists to withhold. Hits are recorded on the operational stream (so they page), and
    the report lists route, facet and term. Routes that need an argument are rendered with a
    representative one; the long-poll is rendered as an immediate poll."""
    from django.test import RequestFactory
    from django.urls import get_resolver
    from . import veil
    veil_obj = veil.Veil("contributor")
    if veil_obj.open:
        return JsonResponse({"data": {"routes_checked": 0, "hits": [],
                                      "verdict": "no facets are hidden from contributors"}})
    patterns = []

    def walk(entries, prefix=""):
        for entry in entries:
            sub = getattr(entry, "url_patterns", None)
            if sub is not None:
                walk(sub, prefix + str(entry.pattern))
            else:
                patterns.append((prefix + str(entry.pattern), entry))
    walk(get_resolver().url_patterns)
    factory = RequestFactory()
    state, snap = _snapshot()
    sample_agent = next(iter(veil.core.read_tiers(hub_app.HUB_DIR)), "contributor")
    samples = {"<str:type>.json": ("tasks.json", {"type": "task"}),
               "<str:type>/<str:local>.json": None}
    checked, hits, skipped = [], [], []
    for route, entry in patterns:
        callback = entry.callback
        if getattr(callback, "_hub_visibility", None) != "veiled":
            continue
        tail = route.split("hub/", 1)[-1] if "hub/" in route else route
        if "<" in tail and tail not in samples:
            skipped.append(tail)
            continue
        sample = samples.get(tail)
        if tail in samples and sample is None:
            skipped.append(tail)
            continue
        path = "/" + route.replace("<str:type>.json", sample[0]) if sample else "/" + route
        kwargs = sample[1] if sample else {}
        query = {"agent": sample_agent, "wait": "0", "q": "the"}
        request = factory.get(path, query)
        request.hub_tier = "contributor"
        try:
            response = callback(request, **kwargs)
            if getattr(response, "streaming", False):
                skipped.append(tail)
                continue
            body = response.content
            if str(response.get("Content-Type", "")).startswith("application/json"):
                body = veil.scrub_response(veil_obj, response).content
            text = body.decode("utf-8", "replace")
        except Exception as exc:                             # noqa: BLE001
            skipped.append("%s (%s)" % (tail, type(exc).__name__))
            continue
        checked.append(tail)
        for term, facet in veil_obj.terms():
            if re.search(veil.core._term_pattern(term), text, re.I):
                hits.append({"route": tail or "(board)", "facet": facet, "term": term})
    if hits:
        try:
            hub_app.record_error(
                "hub.veil", "the veil audit found %d hidden term(s) served to a contributor"
                % len(hits), severity="error", code="veil_leak",
                context={"component": "veil", "hits": hits[:10]})
        except Exception:                                    # noqa: BLE001
            pass
    return JsonResponse({"data": {
        "routes_checked": len(checked), "checked": checked, "skipped": skipped, "hits": hits,
        "verdict": ("LEAK: %d hidden term(s) reached a contributor" % len(hits)) if hits else
                   "no hidden term reached a contributor on %d route(s)" % len(checked)}})


@require_GET
def receipts_json(request):
    """The notification lifecycle: what was offered to whom, delivered, refused, resolved.
    ?ref= is one item's thread (oldest first); ?undelivered=1 lists offers nobody ever
    acknowledged; otherwise the newest receipts (?stage=, ?agent=, ?limit=)."""
    from hub_core import receipts
    ref = (request.GET.get("ref") or "").strip()
    if ref:
        return JsonResponse({"data": receipts.for_ref(hub_app.HUB_DIR, ref),
                             "metadata": {"ref": ref}})
    if request.GET.get("undelivered"):
        try:
            within = max(60, min(int(request.GET.get("within_s") or 86400), 30 * 86400))
        except (TypeError, ValueError):
            within = 86400
        rows = receipts.undelivered(hub_app.HUB_DIR, within)
        kind = (request.GET.get("kind") or "").strip().lower()
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return JsonResponse({"data": rows, "metadata": {"undelivered": len(rows),
                                                        "within_s": within}})
    try:
        limit = max(1, min(int(request.GET.get("limit") or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    rows = receipts.recent(hub_app.HUB_DIR, limit, stage=(request.GET.get("stage") or "").strip(),
                           agent=(request.GET.get("agent") or "").strip().lower())
    return JsonResponse({"data": rows, "metadata": {"count": len(rows),
                                                    "stages": list(receipts.STAGES)}})


@require_GET
def agent_updates_json(request):
    """The agents' first-person feed, newest first (``?limit=``, default 60, max 200)."""
    try:
        limit = max(1, min(int(request.GET.get("limit") or 60), updates.KEEP))
    except (TypeError, ValueError):
        limit = 60
    rows = updates.read(hub_app.HUB_DIR, limit)
    day = time.time() - 86400
    return JsonResponse({"data": rows, "metadata": {
        "count": len(rows), "last_24h": sum(1 for r in rows if (r.get("epoch") or 0) >= day),
        "kinds": list(updates.KINDS)}})


# ── The operational error stream, whoami, and board search ──

@require_GET
def distribution_json(request):
    """GET /hub/distribution.json — every seat graded against what this hub publishes.

    Offline seats are named and never graded as drift; phantom callers and legacy rows are
    listed and never counted; the verdict states what it did NOT grade."""
    report, pub = hub_app.distribution_report()
    return JsonResponse({"data": report, "metadata": {
        "published": {k: {"sha": v["sha"], "published_at": v["published_at"]} for k, v in pub.items()},
        "how": "each seat reports X-Hub-Artifacts (name=sha16) on every request; the hub hashes "
               "the files it publishes (LF-normalized) and compares"}})


@require_GET
def ci_events_json(request):
    """GET /hub/ci-events.json?pipeline=|job=|project=[&limit=] — the RAW CI deliveries behind a
    CI row, newest first. Unlike the rest of the read surface this needs a credential with the
    ``ci:read`` scope: a webhook body carries the project's URLs, branch names and the
    committer, which is diagnostic material, not board content. The store describes itself
    (path, size, writability), so an empty answer says whether nothing arrived or nothing can
    be kept."""
    from hub_core import ci_events as _ci
    from . import hub_write
    auth, problem = hub_write._authenticate(request)
    if not auth or not auth.allows("ci:read"):
        return JsonResponse({"errors": [{"code": "forbidden", "required": "ci:read",
                                          "msg": problem or "credential lacks ci:read"}]}, status=403)
    pipeline = (request.GET.get("pipeline") or "").strip()
    job = (request.GET.get("job") or "").strip()
    project = (request.GET.get("project") or "").strip()
    if not (pipeline or job or project):
        return JsonResponse({"errors": [{"code": "need_filter",
                                          "msg": "name a pipeline, a job or a project"}]}, status=400)
    try:
        limit = int(request.GET.get("limit") or 20)
    except ValueError:
        limit = 20
    rows, store = _ci.retained(hub_app.HUB_DIR, pipeline=pipeline, job=job, project=project,
                               limit=limit)
    return JsonResponse({"data": rows, "metadata": {"count": len(rows), "store": store,
                         "how": "the deliveries this hub kept: two rotated files, the recent "
                                "window, not all history"}})


@require_GET
def errors_json(request):
    """The operational error stream with the bar applied at read. Deferred rows are never
    DROPPED — a stream that silently discards two thirds of its input is one whose "all
    clear" cannot be trusted; they are one query param away (?include=deferred)."""
    try:
        limit = max(1, min(errorlog.KEEP_ROWS, int(request.GET.get("limit") or errorlog.READ_LIMIT)))
    except (TypeError, ValueError):
        limit = errorlog.READ_LIMIT
    # ?app= narrows to ONE service's rows — "is anything broken in budget-app?" must not need
    # the whole window downloaded and filtered by hand; the counts describe the same slice.
    app = re.sub(r"[^a-z0-9-]", "", (request.GET.get("app") or "").strip().lower())[:60]
    rows, metadata, _unclaimed = _errors_block(limit, app=app)
    include = (request.GET.get("include") or "").lower()
    data = rows if include in ("deferred", "all") else [r for r in rows if r.get("bar") == "on"]
    if app:
        # One service's own slice — what an app's chrome shows as "recent errors". The counts
        # above were already computed over this slice, so they describe the same rows.
        metadata["app"] = app
        cov = metadata.get("coverage") or {}
        cov["channels"] = [c for c in cov.get("channels") or [] if c.get("key") == "app"]
    metadata["bar"] = ("every row recorded in the window; `bar` says which are on the board"
                       if include in ("deferred", "all") else
                       "critical and high problems in this system's own surfaces; add "
                       "?include=deferred for everything the bar held back")
    return JsonResponse({"data": data, "metadata": metadata})


@require_GET
def item_claims_json(request):
    """Every live per-machine item claim (hub_core.item_claims): which machine is on which
    question or error fingerprint, for how long, and when the claim releases on its own."""
    from hub_core import item_claims
    try:
        roster = hub_app.roster()
    except Exception:                                        # noqa: BLE001 - unprovable, not gone
        roster = None
    rows = item_claims.live(hub_app.HUB_DIR, roster=roster, grace_s=hub_app.gone_grace_s())
    return JsonResponse({"data": rows, "count": len(rows),
                         "metadata": {"ttl_s": item_claims.TTL_S,
                                      "gone_grace_s": hub_app.gone_grace_s()}})


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


def _doctrine_files() -> dict:
    """name -> path of the documents this board serves through its facet fences. Adopters name
    their own with HUB_DOCTRINE_FILES ({"name": "relative/or/absolute/path.md"}); relative
    paths resolve from HUB_WORK_ROOT. Only files that exist are offered."""
    import os as _os
    configured = hub_app._dj_setting("HUB_DOCTRINE_FILES")
    if not configured and _os.environ.get("HUB_DOCTRINE_FILES"):
        try:
            configured = json.loads(_os.environ["HUB_DOCTRINE_FILES"])
        except ValueError:
            configured = None
    if isinstance(configured, dict) and configured:
        pairs = {str(k): Path(str(v)) for k, v in configured.items()}
    else:
        pairs = {"doctrine": hub_app.PROJECT / "DOCTRINE.md",
                 "charter": hub_app.WORK_ROOT / "CHARTER-CORE.md",
                 "agents": hub_app.WORK_ROOT / "AGENTS.md"}
    out = {}
    for name, path in pairs.items():
        path = path if path.is_absolute() else hub_app.WORK_ROOT / path
        if path.is_file():
            out[name] = path
    return out


@require_GET
def doctrine_json(request):
    """One standing document, rendered through its facet fences for THIS caller.

    Which fenced blocks a reader sees is decided by the credential it presents (scope
    ``facet:<name>``, ``facet:*`` or ``*``), never by a parameter it chooses; an anonymous read
    sees no facet. Hidden blocks are omitted without a trace — the response names only the
    facets this caller CAN see, never the ones withheld."""
    import hashlib
    from hub_core import facets as _facets
    from . import hub_write
    files = _doctrine_files()
    name = (request.GET.get("doc") or "doctrine").strip().lower()
    if name not in files:
        return JsonResponse({"errors": [{"code": "unknown_doc", "msg": "pass ?doc= one of the "
                                         "served documents", "served": sorted(files)}]},
                            status=404)
    auth, _problem = hub_write._authenticate(request)
    visible = _facets.visible_from_scopes(getattr(auth, "scopes", ()) if auth else ())
    try:
        raw = files[name].read_text(encoding="utf-8-sig")
    except OSError as exc:
        return JsonResponse({"errors": [{"code": "unreadable", "msg": type(exc).__name__}]},
                            status=503)
    # Doctrine read from disk never passes the write seam's guard, so it gets the same check
    # here: a lost escape (\a in a Windows path) would otherwise ride every agent's prompt.
    from hub_core import textguard as _textguard
    problems = _textguard.control_char_problems({name: raw})
    if problems:
        logging.getLogger("hub.doctrine").error("doctrine %s refused: %s", name,
                                                _textguard.message(problems))
        return JsonResponse({"errors": [{"code": "control_chars", "doc": name,
                                         "msg": _textguard.message(problems),
                                         "problems": problems}]}, status=503)
    text = _facets.render(raw, visible)
    return JsonResponse({"data": {
        "doc": name, "served": sorted(files),
        "facets_visible": sorted(visible), "subject": getattr(auth, "subject", None),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}})


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
    source = re.sub(r"[^a-z0-9_.-]", "", str(body.get("source") or "board").lower())[:120] or "board"
    context = {"component": "hub-board",
               "operation": str(body.get("operation") or source)[:120],
               "path": request.path_info}
    # WHERE in the board's own script an uncaught exception fired: a same-origin PATH plus
    # line/column, never a stack, never a foreign script's URL (a cross-origin script's
    # details are opaque to the page by design, and an absolute URL could carry a query).
    loc = body.get("location") if isinstance(body.get("location"), dict) else {}
    loc_path = str(loc.get("path") or "")
    if loc_path.startswith("/") and not loc_path.startswith("//") and "?" not in loc_path:
        context["path"] = loc_path[:240]
        for key in ("line", "col"):
            try:
                context[key] = str(max(0, int(loc.get(key))))
            except (TypeError, ValueError):
                pass
    row = hub_app.record_error(
        f"browser.{source}",
        str(body.get("message") or "Browser operation failed")[:800],
        severity=str(body.get("severity") or "error").lower(),
        code=str(body.get("code") or "client_error")[:120],
        context=context,
    )
    return JsonResponse({"data": {"recorded": True, "fingerprint": row["fingerprint"]}}, status=201)


# Marker consumed by the computed route audit: a same-origin, CSRF-protected, bounded
# browser telemetry capability, not general write authority.
client_error._hub_origin_gated = True


# ── Problems, service health, doctor and crossovers: the folded, owned, reachable queue ──

@require_GET
def problems_json(request):
    """The error stream FOLDED into problems — one line per thing somebody fixes, with its
    state (unclaimed / in_flight / escalated / resolved), holder, occurrence count, recency
    and the exact command to claim or resolve it. ?include=resolved|all, ?app=<slug>,
    ?id=<p-id> for one problem with its full stored trace. The counts describe the QUEUE,
    whatever the listing includes."""
    state, _ = _snapshot()
    include = (request.GET.get("include") or "").strip().lower()
    app = re.sub(r"[^a-z0-9._-]", "", (request.GET.get("app") or "").strip().lower())[:60]
    pid = (request.GET.get("id") or "").strip().lower()
    if pid:
        found = problems_core.find(hub_app.HUB_DIR, pid, state)
        if not found:
            return JsonResponse({"errors": [{"code": "no_such_problem", "id": pid}]}, status=404)
        found["holder_phrase"] = problems_core.holder_phrase(found)
        found["owners"] = sorted(problems_core.owners_of(found, hub_app.apps_config(),
                                                         hub_app.live_sessions()))
        return JsonResponse({"data": found})
    probs, meta = problems_core.read(hub_app.HUB_DIR, state, include=include, app=app)
    apps = hub_app.apps_config()
    live_rows = hub_app.live_sessions()
    for p in probs:
        # The trace is the heavy field; the list carries the cause line and a pointer.
        p["details"] = (p.get("details") or "")[:400]
        p["holder_phrase"] = problems_core.holder_phrase(p)
        p["owners"] = sorted(problems_core.owners_of(p, apps, live_rows))
    meta["include"] = include or "queue"
    if app:
        meta["app"] = app
    return JsonResponse({"data": probs, "metadata": meta})


@require_GET
def app_health_json(request):
    """Every service, and whether its failures can reach the board: observed / partial /
    dark / unbuilt, with the gap NAMED as an observation. Reading it starts a bounded
    background liveness sweep of every declared health_url; each row says how old its probe
    is, so a stale "up" is never spent as news."""
    state, _ = _snapshot()
    apps = hub_app.apps_config()
    app_health.sweep_in_background(hub_app.HUB_DIR, apps)
    rows, meta = app_health.rows(hub_app.HUB_DIR, state, apps=apps, native=hub_app.native_slug(),
                                 native_deploy=hub_app.native_deploy(state))
    return JsonResponse({"data": rows, "metadata": meta})


@require_GET
def doctor_json(request):
    """One service diagnosed from evidence: synthesized even when nobody declared it (if it
    ever reported), its OPEN problems (resolved ones are history, listed apart), and a plain
    reading — BLOCKED on unclaimed problems, WAITING on held/escalated ones."""
    slug = re.sub(r"[^a-z0-9._-]", "", (request.GET.get("app") or "").strip().lower())[:60]
    if not slug:
        return JsonResponse({"errors": [{"code": "need_app", "msg": "pass ?app=<slug>"}]}, status=400)
    state, _ = _snapshot()
    data = app_health.doctor(hub_app.HUB_DIR, slug, state, apps=hub_app.apps_config(),
                             native=hub_app.native_slug(),
                             native_deploy=hub_app.native_deploy(state))
    return JsonResponse({"data": data}, status=200 if data["verdict"] != "unknown" else 404)


@require_GET
def overlap_json(request):
    """Crossovers between live consoles. With ?session= (or ?agent=) only the signals that
    concern that side, phrased from it, with unseen ones flagged; with neither, every live
    console and every pair — the roster, which is only paid for when somebody asks."""
    state, _ = _snapshot()
    rows, sigs, titles = _crossovers(state)
    agent = (request.GET.get("agent") or "").strip().lower()
    session = (request.GET.get("session") or "").strip()[:8]
    if agent or session:
        mine = overlap_core.items(sigs, agent=agent, session=session, titles=titles)
        fresh = {it["id"] for it in overlap_core.unseen(hub_app.HUB_DIR, mine)}
        for it in mine:
            it["unseen"] = it["id"] in fresh
        return JsonResponse({"data": mine, "metadata": {"agent": agent, "session": session,
                                                       "consoles": len(rows),
                                                       "systems": list(overlap_core.systems())}})
    pairs = [{"id": s["id"], "kind": s["kind"], "detail": s["detail"],
              "a": {k: s["a"].get(k) for k in ("agent", "machine", "session", "name", "project")},
              "b": {k: s["b"].get(k) for k in ("agent", "machine", "session", "name", "project")}}
             for s in sigs]
    consoles = [{k: r.get(k) for k in ("agent", "machine", "session", "name", "project", "focus",
                                       "files", "task_id", "task_title", "unattended", "age_s")}
                for r in rows]
    return JsonResponse({"data": {"consoles": consoles, "pairs": pairs},
                         "metadata": {"consoles": len(consoles), "pairs": len(pairs),
                                      "systems": list(overlap_core.systems())}})


@require_GET
def enroll_status_json(request):
    """Is this credential still enrolled? Answers active / revoked / expired / unknown for
    ?credential=<id> and never anything secret — a machine that was un-enrolled from the
    board learns it on its next check instead of retrying a dead token forever."""
    from hub_core import agent_auth
    cid = (request.GET.get("credential") or "").strip()[:80]
    if not cid:
        return JsonResponse({"errors": [{"code": "need_credential"}]}, status=400)
    now = time.time()
    for rec in agent_auth.CredentialRegistry(hub_app.HUB_DIR).list_public():
        if rec.get("credential_id") != cid:
            continue
        if rec.get("revoked_at"):
            status = "revoked"
        elif rec.get("expires_at") and _epoch(rec.get("expires_at")) and _epoch(rec.get("expires_at")) < now:
            status = "expired"
        else:
            status = "active"
        return JsonResponse({"data": {"credential_id": cid, "status": status,
                                      "subject": rec.get("subject"),
                                      "revoked_at": rec.get("revoked_at"),
                                      "expires_at": rec.get("expires_at")}})
    return JsonResponse({"data": {"credential_id": cid, "status": "unknown"}}, status=404)
