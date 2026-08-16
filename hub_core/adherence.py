"""BOARD ADHERENCE — is the board actually being followed, and is it current?

Every other measurement here answers "how much work is done". None of them answer the question an
operator actually has when they walk away from a running fleet: *is the board still telling me the
truth?* A board drifts out of usefulness long before it goes wrong — a task claimed six hours ago
with no heartbeat, a done row with no evidence behind it, a todo nobody can pull because it never
got concrete acceptance. Each is individually survivable and collectively fatal: the board still
renders green while it stops describing the work.

So this folds six dimensions, each a ratio over a set the fold can actually SEE:

  SPECCED    open tasks a worker could pull — concrete acceptance.
             An unspecced todo is not work, it is a note about work.
  PROVEN     done tasks whose real result was recorded; if a critical probe was declared, its
             exit-0 verification_run receipt is present.
  EVIDENCED  done tasks carrying evidence (a uri, a commit, a named verifier).
  FRESH      live leases heartbeated inside the stall window — a worker still holding its claim.
  CURRENT    open tasks touched by any event inside the staleness window. Work that has not moved
             in days is either blocked and unsaid, or abandoned and unsaid.
  MOVING     leased tasks whose plan checklist advanced since the claim — a worker doing the thing,
             not merely holding the lease.

EVERY RATIO CARRIES ITS DENOMINATOR AND ITS UNMEASURED COUNT, and a dimension with an empty
denominator reports None rather than 100%. This is the whole discipline of the module: a board
with no done tasks is not a perfectly-proven board, and a fleet with no leases is not a perfectly
fresh one. A bare percentage computed over nothing is the most confident lie a dashboard tells,
and the composite score is therefore averaged over MEASURED dimensions only, naming the ones it
had to skip.

Pure and stack-neutral: events + folded state + the live leases in, plain dict out. No I/O, no
clock of its own (``now`` is injectable so a caller can ask what the board looked like at a time).
"""
import datetime

# A claim older than this without a heartbeat is a worker that stopped talking. It matches the
# default lease TTL used by the claim seam: a lease that has outlived its own renewal window is
# the same signal the reclaim path already acts on.
STALL_S = 900
# Open work untouched for longer than this has stopped being current. A day is deliberately
# generous — a board is allowed to have a weekend.
STALE_S = 86400

DIMENSIONS = ("specced", "proven", "evidenced", "fresh", "current", "moving")
# What a failing dimension actually costs the operator, so the cockpit can say why a number
# matters instead of only that it is low.
MEANING = {
    "specced": "todo work no worker can pull until someone writes concrete acceptance",
    "proven": "a declared critical probe has no matching exit-0 receipt",
    "evidenced": "done with nothing recorded that a reader could go and look at",
    "fresh": "a claimed task whose worker has stopped heartbeating",
    "current": "open work that has not moved — blocked and unsaid, or abandoned and unsaid",
    "moving": "a held lease with no plan progress behind it",
}


def _parse(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None  # absorbs: absent/malformed ts — this row is unmeasured, never counted good


def _ratio(ok, total, unmeasured=0):
    """One dimension's honest shape. `pct` is None on an empty denominator — NOT 100."""
    return {"ok": ok, "total": total, "unmeasured": unmeasured,
            "pct": (round(100.0 * ok / total, 1) if total else None)}


def _acceptance_ok(task):
    """A concrete acceptance: present, and not a placeholder standing in for one. A stub that
    says 'TBD' passes a truthiness check and fails a worker."""
    text = str(task.get("acceptance") or "").strip()
    if len(text) < 12:
        return False
    return text.lower().rstrip(".") not in ("tbd", "todo", "n/a", "na", "none", "unknown", "?")


def _receipt_ok(task):
    """Ordinary completion needs no test; an explicitly declared critical probe needs a receipt."""
    if not str(task.get("verification_command") or "").strip():
        return True
    runs = task.get("verification_run") or []
    if isinstance(runs, dict):
        runs = [runs]
    return any(isinstance(r, dict) and r.get("exit_code") == 0 for r in runs)


def _evidence_ok(task):
    """Something a reader could go and LOOK at: a uri, a named verifier, or a commit."""
    prov = task.get("provenance") or {}
    uris = task.get("evidence_uri") or []
    if isinstance(uris, str):
        uris = [uris]
    return bool([u for u in uris if str(u).strip()]
                or (task.get("verified_by") or [])
                or (prov.get("commits") or []))


def _last_touch(events):
    """{aggregate: latest event datetime} — when each entity last actually moved."""
    out = {}
    for ev in events:
        agg = ev.get("aggregate")
        when = _parse(ev.get("ts"))
        if agg and when and (agg not in out or when > out[agg]):
            out[agg] = when
    return out


def score(events, state, leases=(), now=None, stall_s=STALL_S, stale_s=STALE_S):
    """The adherence block. `leases` is the live-lease list the cockpit already computes
    ({task, agent, age_s, stalled} each) — passed in rather than re-read, so the rail and this
    score can never disagree about which worker holds what."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    tasks = state.get("by_type", {}).get("task", [])
    entities = state.get("entities", {})
    touched = _last_touch(events)

    open_tasks = [t for t in tasks if (t.get("status") or "").lower() in ("todo", "blocked")]
    done_tasks = [t for t in tasks if (t.get("status") or "").lower() == "done"]
    leases = list(leases or [])

    dims, offenders = {}, {}

    def record(name, bad_rows):
        # Offenders are what turns a score into an action. Bounded: a rail is for acting on, and
        # a board that is 400 tasks out of discipline needs the first few and the count, not 400
        # rows through the live payload.
        if bad_rows:
            offenders[name] = [{"id": t.get("id"), "title": t.get("title")} for t in bad_rows[:8]]

    unspecced = [t for t in open_tasks if not _acceptance_ok(t)]
    dims["specced"] = _ratio(len(open_tasks) - len(unspecced), len(open_tasks))
    record("specced", unspecced)

    unproven = [t for t in done_tasks if not _receipt_ok(t)]
    dims["proven"] = _ratio(len(done_tasks) - len(unproven), len(done_tasks))
    record("proven", unproven)

    unevidenced = [t for t in done_tasks if not _evidence_ok(t)]
    dims["evidenced"] = _ratio(len(done_tasks) - len(unevidenced), len(done_tasks))
    record("evidenced", unevidenced)

    stalled = [lease for lease in leases if lease.get("stalled")
               or (lease.get("age_s") is not None and lease["age_s"] > stall_s)]
    dims["fresh"] = _ratio(len(leases) - len(stalled), len(leases))
    record("fresh", [{"id": lease.get("task"), "title": lease.get("title")} for lease in stalled])

    # A task the ledger never touched has no age to judge; it is unmeasured, not stale. Counting
    # it as either would make the number describe the ledger's completeness instead of the board's.
    rotting, undated = [], 0
    for t in open_tasks:
        when = touched.get(t.get("id"))
        if not when:
            undated += 1
            continue
        if (now - when).total_seconds() > stale_s:
            rotting.append(t)
    measurable_open = len(open_tasks) - undated
    dims["current"] = _ratio(measurable_open - len(rotting), measurable_open, undated)
    record("current", rotting)

    # MOVING is only askable of a lease whose task carries a plan. A worker on a task with no
    # checklist is not stuck — nobody gave it steps to advance — so it is unmeasured.
    still, no_plan = [], 0
    for lease in leases:
        ent = entities.get(lease.get("task")) or {}
        plan = ent.get("plan") or []
        if not plan:
            no_plan += 1
            continue
        if not any(s.get("done") for s in plan):
            still.append({"id": lease.get("task"), "title": lease.get("title")})
    measurable_leases = len(leases) - no_plan
    dims["moving"] = _ratio(measurable_leases - len(still), measurable_leases, no_plan)
    record("moving", still)

    measured = [d for d in DIMENSIONS if dims[d]["pct"] is not None]
    skipped = [d for d in DIMENSIONS if dims[d]["pct"] is None]
    overall = round(sum(dims[d]["pct"] for d in measured) / len(measured), 1) if measured else None
    # The weakest MEASURED dimension is what an operator should fix first; on a board where
    # nothing could be measured there is no such thing, and the cockpit must say so.
    weakest = min(measured, key=lambda d: dims[d]["pct"]) if measured else None

    return {
        "score": overall,
        "dimensions": dims,
        "measured": measured,
        "unmeasurable": skipped,
        "weakest": weakest,
        "weakest_meaning": MEANING.get(weakest) if weakest else None,
        "offenders": offenders,
        "meaning": MEANING,
        "window": {"stall_s": stall_s, "stale_s": stale_s},
    }
