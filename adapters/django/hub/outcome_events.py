"""Lifecycle facts as LEDGER EVENTS: lease claims, the problem lifecycle, responder runs.

None of these were in the hash-chained ledger. Task leases live in ``claims/<task>.json``, problem
claims in ``problem-claims.json``, resolutions in ``problem-resolved.json``, item claims in
``item-claims.json`` -- each the LATEST state only, overwritten in place. So first-claim time,
abandonment, claim churn, time-to-resolve and recurrence after a resolve were recoverable from
nothing the hub kept: on the instance this was lifted from, every lead-time or recurrence number
was an excavation, not a query.

The side files stay exactly as they are -- the fast, locked, current-state answer every reader
already uses. This module ADDS the history beside them: one append per transition, on a log-only
aggregate (``<project>:outcome:<kind>.<subject>``) that the fold skips (``hub_core.project`` only
materializes known types), so a lifecycle event never bumps a task's version and can never make a
writer's ``expected_version`` stale.

Event types:
  lease.claimed      a task lease granted to a new holder        {task, agent, machine, session}
  lease.taken_over   granted over a provably gone holder          {+ from}
  lease.released     released, voided or expired                  {task, agent, reason}
  problem.claimed    a console put its name on a problem          {problem, agent, session, take, from}
  problem.released   handed back                                  {problem, agent}
  problem.escalated  parked on a named ask or task                {problem, agent, blocked_on}
  problem.resolved   root cause + evidence recorded               {problem, agent, rows, evidence}
  problem.reopened   recurred after a resolve (a background sweep){problem, resolved_at, recurred_at}
  run.started        a machine won the claim to work an item      {item, aggregate, machine}
  run.finished       that run released its claim with its ending  {item, outcome, tokens, spent}

FAIL-SOFT AND AUDIBLE. A lifecycle fact that cannot be written must never fail the claim or the
resolve it describes, and it lands on the error board so the gap is seen. A live transition is
written once by its caller, so when the ledger's write lock is busy (``StoreBusy``, which fails
BEFORE anything lands) it is SPOOLED under its idempotency key and the background tick writes it
(``drain_pending``): a busy lock delays the fact instead of losing it. A SWEEP that re-derives
its facts every pass (the reopen sweep) passes ``deferrable``: a busy lock is back-pressure, counted
and logged, and the pass stops at the first one. The spool lock is held only to snapshot and to
merge back, never across the ledger writes -- held across them, a contended ledger (each BEGIN
taking up to its full wait to give up) timed the live spool out under exactly the contention that
spools, and the fact was lost.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from hub_core import atomic as _atomic
from hub_core.process_lock import LockBusy, ProcessFileLock
from hub_core.store import StoreBusy

from . import hub_app

log = logging.getLogger(__name__)

TYPES = ("lease.claimed", "lease.taken_over", "lease.released", "problem.claimed",
         "problem.released", "problem.escalated", "problem.resolved", "problem.reopened",
         "run.started", "run.finished")
#: The fields each type's readers count on. A write missing one is still recorded -- losing the
#: fact would be worse -- but the gap reaches the error board at once.
REQUIRED = {
    "lease.claimed": ("task", "agent"), "lease.taken_over": ("task", "agent"),
    "lease.released": ("task", "reason"),
    "problem.claimed": ("problem", "agent"), "problem.released": ("problem", "agent"),
    "problem.escalated": ("problem", "blocked_on"), "problem.resolved": ("problem", "agent"),
    "problem.reopened": ("problem", "resolved_at"),
    "run.started": ("item", "machine"), "run.finished": ("item", "outcome"),
}
STATS = {"written": 0, "failed": 0, "deferred": 0, "spooled": 0, "drained": 0, "last_error": ""}
#: A spooled event still unwritten after this long is a real gap, not back-pressure: loud.
PENDING_LOUD_AFTER_S = 3600
PENDING_FILE = "outcome-events.pending.jsonl"


def _pending():
    return hub_app.HUB_DIR / PENDING_FILE


def prefix() -> str:
    return "%s:outcome:" % hub_app.PROJECT_KEY


def subject(kind: str, ident: str) -> str:
    """``<project>:outcome:task.<id>`` -- one log-only aggregate per subject. Punctuation in the
    id is flattened, so the aggregate never reads as a task id (``:task:``) to a reader that
    indexes by id shape."""
    local = re.sub(r"[^a-z0-9._-]+", "-", ("%s.%s" % (kind, ident)).lower()).strip("-.")[:180]
    return prefix() + (local or kind)


def _loud(msg: str, code: str, etype: str) -> None:
    try:
        hub_app.record_error("hub.outcome", msg, severity="error", code=code,
                             context={"component": "outcome-events", "operation": etype})
    except Exception:                                        # noqa: BLE001
        pass


def emit(etype: str, kind: str, ident: str, payload: dict, *, agent: str = "", idem: str = "",
         actor_kind: str = "hub", deferrable: bool = False, at: str = "") -> dict | None:
    """Append one lifecycle event; the stored event, or None when it could not be written now
    (spooled, deferred, or recorded on the error board). Never raises."""
    if etype not in TYPES or not str(ident or "").strip():
        return None
    body = {k: v for k, v in dict(payload or {}).items() if v not in (None, "")}
    body["at"] = at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    missing = [k for k in REQUIRED.get(etype, ()) if k not in body]
    if missing:
        _loud("an outcome event is missing %s: %s %s" % (",".join(missing), etype, ident),
              "outcome_event_contract", etype)
    try:
        s = hub_app.store()
        try:
            before = s.latest_cursor().get("seq", 0)
            ev = s.append(aggregate=subject(kind, ident), type=etype, payload=body,
                          expected_version=None, agent_id=(agent or "hub")[:120],
                          actor_kind=actor_kind, git_sha=hub_app._git_head(),
                          idem_key=(idem or None))
        finally:
            s.close()
        if ev.get("seq", 0) > before:
            hub_app.publish_event(ev)
        STATS["written"] += 1
        return ev
    except Exception as exc:                                 # noqa: BLE001 - never fail the caller
        if deferrable and isinstance(exc, StoreBusy):
            STATS["deferred"] += 1
            STATS["last_error"] = "deferred %s: %s" % (etype, str(exc)[:200])
            log.info("outcome event %s for %s deferred: the write lock is busy", etype, ident)
            return None
        if isinstance(exc, StoreBusy) and idem and _spool(etype, kind, ident, body, agent=agent,
                                                          idem=idem, actor_kind=actor_kind):
            STATS["spooled"] += 1
            STATS["last_error"] = "spooled %s: %s" % (etype, str(exc)[:200])
            log.info("outcome event %s for %s spooled for the background tick", etype, ident)
            return None
        STATS["failed"] += 1
        STATS["last_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        log.warning("could not write outcome event %s for %s", etype, ident, exc_info=True)
        _loud("a lifecycle event could not be written: %s %s (%s)"
              % (etype, ident, type(exc).__name__), "outcome_event_failed", etype)
        return None


def _spool(etype, kind, ident, body, **kw) -> bool:
    rec = dict(kw, etype=etype, kind=kind, ident=ident, payload=body, spooled_at=time.time())
    try:
        path = _pending()
        path.parent.mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(path.parent, name=".outcome-pending.lock", timeout=5):
            _atomic.append_line(path, json.dumps(rec, sort_keys=True) + "\n")
        return True
    except Exception:                                        # noqa: BLE001
        log.warning("could not spool outcome event %s for %s", etype, ident, exc_info=True)
        return False


def drain_pending(now: float | None = None) -> dict:
    """Write the spooled live transitions, oldest first, under their idempotency keys (a replay
    of one that did land is a no-op). Stops at the first busy lock -- the next tick resumes -- and
    goes loud only for an event still unwritten after PENDING_LOUD_AFTER_S. One drain at a time;
    the spool lock is held only to snapshot and to merge back (lines spooled meanwhile are kept)."""
    now = time.time() if now is None else now
    path = _pending()
    if not path.exists():
        return {"pending": 0, "drained": 0}
    drained, keep = 0, []
    try:
        with ProcessFileLock(path.parent, name=".outcome-drain.lock", timeout=0):
            with ProcessFileLock(path.parent, name=".outcome-pending.lock", timeout=5):
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            busy = False
            for ln in lines:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    log.warning("unreadable outcome spool line dropped: %r", ln[:200])
                    continue
                if not busy:
                    deferred = STATS["deferred"]
                    ev = emit(rec.get("etype", ""), rec.get("kind", ""), rec.get("ident", ""),
                              rec.get("payload") or {}, agent=rec.get("agent") or "",
                              idem=rec.get("idem") or "", actor_kind=rec.get("actor_kind") or "hub",
                              deferrable=True, at=str((rec.get("payload") or {}).get("at") or ""))
                    if ev is not None:
                        drained += 1
                        continue
                    busy = STATS["deferred"] > deferred
                    if not busy:
                        continue                 # refused for another reason: emit went loud
                age = now - float(rec.get("spooled_at") or now)
                if age > PENDING_LOUD_AFTER_S and not rec.get("loud"):
                    rec["loud"] = True
                    _loud("a lifecycle event is still unwritten after %d min: %s %s (ledger busy)"
                          % (age // 60, rec.get("etype"), rec.get("ident")),
                          "outcome_event_failed", str(rec.get("etype")))
                keep.append(json.dumps(rec, sort_keys=True))
            with ProcessFileLock(path.parent, name=".outcome-pending.lock", timeout=30):
                cur = ([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                       if path.exists() else [])
                keep += cur[len(lines):]         # spooled while the ledger writes ran
                _atomic.write_text(path, "".join(k + "\n" for k in keep))
    except LockBusy:
        return {"drained": 0, "skipped": "another drain is running"}
    except Exception:                                        # noqa: BLE001 - the next tick retries
        log.warning("outcome spool drain failed", exc_info=True)
        return {"drained": drained, "error": "drain failed"}
    STATS["drained"] += drained
    return {"pending": len(keep), "drained": drained}


# ---------------------------------------------------------------- the reopen sweep

REOPEN_SWEEP_EVERY_S = 60
_LAST_SWEEP = {"at": 0.0, "emitted": 0, "seen": 0}
_EMITTED: set = set()


def sweep_reopens(now: float | None = None, force: bool = False) -> dict:
    """Write ``problem.reopened`` once per (problem, resolution) the problem fold reports as
    recurred. The fold is where recurrence is DEFINED (a row newer than the resolve), so this
    reads its verdict instead of re-deriving it. A key already on the ledger is skipped WITHOUT
    the write lock (an idempotent replay still takes it, and a sweep that re-takes a contended
    lock every pass to learn nothing is load the hub cannot spare)."""
    from hub_core import problems as _problems
    now = time.time() if now is None else now
    if not force and now - _LAST_SWEEP["at"] < REOPEN_SWEEP_EVERY_S:
        return dict(_LAST_SWEEP, skipped=True)
    _LAST_SWEEP["at"] = now
    try:
        rows, _meta = _problems.read(hub_app.HUB_DIR, hub_app.current_state(), include="all",
                                     now=now)
    except Exception:                                        # noqa: BLE001
        log.warning("problem fold unavailable for the reopen sweep", exc_info=True)
        return dict(_LAST_SWEEP, error="fold unavailable")
    emitted = 0
    s = hub_app.store()
    try:
        for p in rows:
            if not isinstance(p, dict) or not p.get("reopened"):
                continue
            res = p.get("resolved") if isinstance(p.get("resolved"), dict) else {}
            epoch = int(float(res.get("epoch") or 0))
            if not epoch:
                continue
            key = "reopened:%s:%d" % (p.get("id"), epoch)
            if key in _EMITTED or s.has_idem(subject("problem", str(p.get("id"))), key):
                _EMITTED.add(key)
                continue
            last = p.get("last_seen")
            deferred = STATS["deferred"]
            ev = emit("problem.reopened", "problem", str(p.get("id")),
                      {"problem": p.get("id"), "app": p.get("where") or "", "kind": p.get("kind") or "",
                       "severity": p.get("severity") or "", "resolved_at": res.get("at"),
                       "resolved_by": res.get("by"),
                       "recurred_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(last)))
                                       if isinstance(last, (int, float)) and last else None),
                       "count": p.get("count")},
                      idem=key, deferrable=True)
            if ev is not None:
                _EMITTED.add(key)
                emitted += 1
            elif STATS["deferred"] > deferred:
                break                     # contended: stop queueing on the lock; next pass resumes
    finally:
        s.close()
    _LAST_SWEEP.update(emitted=emitted, seen=len(rows))
    return dict(_LAST_SWEEP)


def tick() -> dict:
    """The background tick's share: drain the spool, then look for recurrences."""
    return {"drain": drain_pending(), "reopens": sweep_reopens()}


_KICK_LOCK = threading.Lock()
_KICK = {"at": 0.0}


def kick() -> bool:
    """A single-process hub has no background tick: the board's read path asks for one pass,
    throttled and single-flight, on a daemon thread. In the ``web`` role it does nothing -- the
    background process owns it."""
    try:
        from . import roles
        if roles.role() == "web" or time.time() - _KICK["at"] < REOPEN_SWEEP_EVERY_S:
            return False
        if not _KICK_LOCK.acquire(blocking=False):
            return False
    except Exception:                                        # noqa: BLE001
        return False
    _KICK["at"] = time.time()

    def _run():
        try:
            tick()
        except Exception:                                    # noqa: BLE001
            log.warning("outcome tick failed", exc_info=True)
        finally:
            _KICK_LOCK.release()
    try:
        threading.Thread(target=_run, name="hub-outcome-tick", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        _KICK_LOCK.release()
        return False
    return True


# ---------------------------------------------------------------- the reader

def events(types=None, since_seq: int = 0, limit: int = 500) -> list:
    """Lifecycle events after ``since_seq``, oldest first, optionally of some types: a seek on
    the store's aggregate index, never a fold."""
    s = hub_app.store()
    try:
        rows = s.events_prefixed(prefix(), since_seq=since_seq, limit=limit)
    finally:
        s.close()
    want = set(types) if types else None
    return [e for e in rows if want is None or e.get("type") in want]


def outcomes_json(request):
    """GET /hub/outcomes.json?type=<t>[,<t>]&since=<seq>&limit=<n> -- the lifecycle history:
    claims, the problem lifecycle and responder runs, each event with who wrote it and when.
    ``metadata.next`` is the cursor to page from; ``metadata.stats`` is this process's write
    counters (written / spooled / deferred / failed / drained)."""
    from django.http import JsonResponse
    types = [t for t in str(request.GET.get("type") or "").split(",") if t.strip()]
    try:
        since = max(0, int(request.GET.get("since") or 0))
        limit = max(1, min(int(request.GET.get("limit") or 200), 1000))
    except ValueError:
        return JsonResponse({"errors": [{"code": "bad_cursor"}]}, status=400)
    unknown = [t for t in types if t not in TYPES]
    if unknown:
        return JsonResponse({"errors": [{"code": "unknown_type", "types": unknown,
                                         "known": list(TYPES)}]}, status=400)
    rows = events(types or None, since, limit)
    return JsonResponse({"data": [{"seq": e.get("seq"), "type": e.get("type"),
                                   "subject": str(e.get("aggregate") or "")[len(prefix()):],
                                   "agent": e.get("agent_id"), "actor_kind": e.get("actor_kind"),
                                   **(e.get("payload") or {})} for e in rows],
                         "metadata": {"next": rows[-1].get("seq") if rows else since,
                                      "types": list(TYPES), "stats": dict(STATS)}})
