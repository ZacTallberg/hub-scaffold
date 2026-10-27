"""Where a hub WRITE spends its time, per phase, in the serving process.

A worker's own clock can say a claim took seventeen seconds while route timing says the route
answers in sixty milliseconds. Both can be true: the time is somewhere route timing cannot see
(the wait for a server thread, the interpreter lock, a file lock) or inside one phase of one
route. Guessing which is how a hub stays slow, so every authenticated write is split into named
phases here and the split is served twice:

* on ``/hub/perf.json`` as ``write_phases`` -- per route over the last ``WINDOW_S``: count,
  total p50/p95/max, and per phase p50/p95/max plus its SHARE of all handler time;
* on the response itself as a ``Server-Timing`` header, so the caller can subtract the server's
  total from its own wall clock: what is left is the queue and the network.

A phase is a ``with phase("name"):`` block. Phases nest; only the OUTERMOST ones are summed, so
``unattributed_share_pct`` is the handler time no phase names. ``add()`` records a phase that
was measured elsewhere (inside the event store, or before the view ran).

WHAT HAPPENS BEFORE THE VIEW. The first reading of these phases on a busy hub showed a write
whose handler took one second answering in seventeen: almost all of a write's wall time was
spent before the view ran. Two measures say where. ``enter()`` / ``leave()`` (called by the
OUTERMOST middleware) count the requests the process serves at once and remember when each
entered the stack, so a write's ``pre_view`` phase is its time in middleware before the view;
a sampler thread asks for a 50 ms sleep twice a second and records how late it woke, which is
time the interpreter was held by other threads -- a cost every request pays. Both are served as
``process_pressure``, together with the routes holding the threads right now and their ages
(a long-held read route is usually what a write is queued behind).

Process-local, bounded (RING samples per route, the last WINDOW_S), framework-free, stdlib only.
Fail-soft by construction: timing never breaks a write.
"""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager

RING = 300
WINDOW_S = 3600
_TL = threading.local()
_LOCK = threading.Lock()
_RINGS: dict = {}          # route -> [(at, total_ms, {phase: ms}, top_ms, status)]


def begin(route: str) -> None:
    """Start the record for one request on this thread (a nested begin replaces nothing: the
    outer record keeps timing, and the inner route is recorded as its own phase)."""
    rec = getattr(_TL, "rec", None)
    if rec is not None:
        rec.setdefault("nested", []).append((str(route or "")[:120], time.perf_counter()))
        return
    _TL.rec = {"route": str(route or "")[:120], "t0": time.perf_counter(), "phases": {},
               "depth": 0, "top": 0.0}


def active() -> bool:
    return getattr(_TL, "rec", None) is not None


@contextmanager
def phase(name: str):
    rec = getattr(_TL, "rec", None)
    if rec is None:
        yield
        return
    t = time.perf_counter()
    rec["depth"] += 1
    try:
        yield
    finally:
        try:
            rec["depth"] -= 1
            ms = (time.perf_counter() - t) * 1000.0
            rec["phases"][name] = rec["phases"].get(name, 0.0) + ms
            if rec["depth"] == 0:
                rec["top"] += ms
        except Exception:                                    # noqa: BLE001 - never break a write
            pass


def add(name: str, ms: float) -> None:
    """Record a phase measured elsewhere. Counted as nested (never in the attributed sum)."""
    rec = getattr(_TL, "rec", None)
    if rec is not None:
        try:
            rec["phases"][name] = rec["phases"].get(name, 0.0) + float(ms)
        except Exception:                                    # noqa: BLE001
            pass


def add_all(prefix: str, phases) -> None:
    """``add(prefix + key, ms)`` for every item of a {key: ms} mapping (a store's own split)."""
    for key, ms in (phases or {}).items() if isinstance(phases, dict) else ():
        add(prefix + str(key), ms)


def end(status) -> str:
    """Close this thread's record; return a Server-Timing header value ('' when none)."""
    rec = getattr(_TL, "rec", None)
    if rec is None:
        return ""
    nested = rec.get("nested") or []
    if nested:
        # A nested begin: close it as a phase of the outer record instead of ending the request.
        route, t = nested.pop()
        rec["phases"][route] = rec["phases"].get(route, 0.0) + (time.perf_counter() - t) * 1000.0
        return ""
    _TL.rec = None
    try:
        total = (time.perf_counter() - rec["t0"]) * 1000.0
        now = time.time()
        with _LOCK:
            ring = _RINGS.setdefault(rec["route"], [])
            ring.append((now, total, dict(rec["phases"]), rec["top"], int(status or 0)))
            if len(ring) > RING:
                del ring[:len(ring) - RING]
        parts = ["total;dur=%.1f" % total]
        parts += ["%s;dur=%.1f" % (re.sub(r"[^A-Za-z0-9_.-]", "_", k), v)
                  for k, v in rec["phases"].items()]
        return ", ".join(parts)[:1800]
    except Exception:                                        # noqa: BLE001
        return ""


def _pct(values, p):
    s = sorted(values)
    return round(s[max(0, (p * len(s) + 99) // 100 - 1)], 1) if s else None


def summary() -> dict:
    """Per timed route over the last WINDOW_S: count, total p50/p95/max, and per phase
    p50/p95/max plus its SHARE of all handler time on that route."""
    now = time.time()
    with _LOCK:
        rings = {r: [x for x in ring if now - x[0] < WINDOW_S] for r, ring in _RINGS.items()}
    out = {}
    for route, ring in sorted(rings.items()):
        if not ring:
            continue
        totals = [x[1] for x in ring]
        whole = sum(totals) or 1.0
        phases = {}
        for name in sorted({k for x in ring for k in x[2]}):
            vals = [x[2].get(name, 0.0) for x in ring]
            phases[name] = {"p50_ms": _pct(vals, 50), "p95_ms": _pct(vals, 95),
                            "max_ms": round(max(vals), 1),
                            "share_pct": round(100 * sum(vals) / whole, 1)}
        out[route] = {"count": len(ring), "p50_ms": _pct(totals, 50), "p95_ms": _pct(totals, 95),
                      "max_ms": round(max(totals), 1),
                      "unattributed_share_pct": round(
                          100 * sum(max(0.0, x[1] - x[3]) for x in ring) / whole, 1),
                      "phases": phases}
    return out


# ── before the view: concurrency at arrival, the routes holding threads, interpreter lag ──

_INFLIGHT = [0]
_INFLIGHT_RING: list = []          # (at, in_flight at entry)
_GIL_RING: list = []               # (at, lag_ms)
_CURRENT: dict = {}                # thread id -> (route, started) of every request in flight
_SAMPLER = {"started": False}


def enter(route: str = "") -> tuple:
    """A request entered the outermost middleware: ``(perf_counter_at_entry, in_flight)``."""
    with _LOCK:
        _CURRENT[threading.get_ident()] = (str(route or "")[:80], time.time())
        _INFLIGHT[0] += 1
        n = _INFLIGHT[0]
        _INFLIGHT_RING.append((time.time(), n))
        if len(_INFLIGHT_RING) > 4000:
            del _INFLIGHT_RING[:1000]
    _start_sampler()
    return time.perf_counter(), n


def leave() -> None:
    with _LOCK:
        _CURRENT.pop(threading.get_ident(), None)
        _INFLIGHT[0] = max(0, _INFLIGHT[0] - 1)


def _start_sampler() -> None:
    if _SAMPLER["started"]:
        return
    with _LOCK:
        if _SAMPLER["started"]:
            return
        _SAMPLER["started"] = True

    def run():
        while True:
            try:
                t = time.perf_counter()
                time.sleep(0.05)
                lag = (time.perf_counter() - t - 0.05) * 1000.0
                with _LOCK:
                    _GIL_RING.append((time.time(), lag))
                    if len(_GIL_RING) > 7200:
                        del _GIL_RING[:1800]
                time.sleep(0.45)
            except Exception:                                # noqa: BLE001
                time.sleep(5)

    threading.Thread(target=run, name="hub-write-timing-sampler", daemon=True).start()


def pool_threads() -> int:
    """This process's request-thread pool, as its server runner declares it in
    ``HUB_SERVER_THREADS`` (0 = not declared: the at-cap share is then not computed)."""
    try:
        return max(0, int(os.environ.get("HUB_SERVER_THREADS") or 0))
    except (TypeError, ValueError):
        return 0


def process_pressure() -> dict:
    """In-flight requests at each arrival, the routes holding threads now (with the oldest
    age), and the interpreter's wake-up lag, over the last WINDOW_S."""
    now = time.time()
    with _LOCK:
        inflight = [n for at, n in _INFLIGHT_RING if now - at < WINDOW_S]
        lag = [ms for at, ms in _GIL_RING if now - at < WINDOW_S]
        current = _INFLIGHT[0]
        holding = [(r, now - t) for r, t in _CURRENT.values()]
    by_route: dict = {}
    for route, age in holding:
        key = re.sub(r"/[0-9a-f]{8,}[^/]*|/\d{3,}(?=[./])", "/<id>", route)
        by_route.setdefault(key, []).append(age)
    cap = pool_threads()
    return {"in_flight_now": current,
            "in_flight_routes": {r: {"n": len(a), "oldest_s": round(max(a), 1)} for r, a in
                                 sorted(by_route.items(), key=lambda kv: -len(kv[1]))},
            "in_flight_at_arrival": {
                "n": len(inflight), "p50": _pct(inflight, 50), "p95": _pct(inflight, 95),
                "max": max(inflight) if inflight else None, "threads": cap or None,
                "at_or_over_cap_pct": (round(100 * sum(1 for n in inflight if n >= cap)
                                             / len(inflight), 1) if inflight and cap else None)},
            "interpreter_wake_lag_ms": {
                "n": len(lag), "p50": _pct(lag, 50), "p95": _pct(lag, 95),
                "max": round(max(lag), 1) if lag else None,
                "over_100ms_pct": (round(100 * sum(1 for x in lag if x > 100) / len(lag), 1)
                                   if lag else None)}}
