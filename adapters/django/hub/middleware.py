import json
import os
import re
import sqlite3
import threading
import time

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import JsonResponse


class NoStoreHTMLMiddleware:
    """Dynamic dashboard pages must never be served stale from the browser cache after a
    deploy (no-store on interactive pages). Static assets are untouched. Dual-capable so this
    tiny header policy never forces an ASGI deployment, including persistent SSE, through Django's
    sync adaptation thread."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        response = self.get_response(request)
        return self._finish(response)

    async def __acall__(self, request):
        response = await self.get_response(request)
        return self._finish(response)

    @staticmethod
    def _finish(response):
        if response.get("Content-Type", "").startswith("text/html"):
            response.setdefault("Cache-Control", "no-store, must-revalidate")
        return response


class LedgerBusyMiddleware:
    """A busy ledger answers 503 + Retry-After. It must never answer 500.

    Every Hub surface folds the event log, and every fold — read or write — can need the ledger
    lock (an append, and the index heal that runs when a store is opened). When the wait budget
    is spent the store raises ``hub_core.store.StoreBusy``; a raw ``sqlite3.OperationalError:
    database is locked`` from a path written later is the same condition. Uncaught, either climbs
    out of the view as an Internal Server Error and puts a red defect row on the error queue for
    something transient by construction.

    503 is the true statement: nothing was written, come back in a moment. Clients that retry
    (``hub_core.client`` does, on ``code: busy``) clear it without anyone being asked to look.
    The response is marked already-logged so Django's own 5xx ERROR log does not put a defect on
    the board in place of the one removed; instead a WARNING row carrying the wait and the path is
    recorded — back-pressure is trended, not triaged.

    Place it early in MIDDLEWARE (before any read gate) so a lock burst is answered whether the
    request was heading for a read or a write."""

    RETRY_AFTER = "2"
    # Dual-capable for the same reason as NoStoreHTMLMiddleware: a sync-only middleware would
    # force an ASGI deployment's persistent SSE rail through Django's sync adaptation thread.
    # process_exception stays a plain method; Django runs exception hooks in sync mode either way.
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        return self._finish(self.get_response(request))

    async def __acall__(self, request):
        return self._finish(await self.get_response(request))

    def _finish(self, response):
        # Views that map the condition themselves (the write seam returns a {"code": "busy"}
        # 503) get the header from this one place rather than from each call site.
        if response.status_code == 503 and not response.has_header("Retry-After"):
            response["Retry-After"] = self.RETRY_AFTER
        return response

    def process_exception(self, request, exception):
        from hub_core.store import StoreBusy

        if isinstance(exception, StoreBusy):
            waited = exception.waited_s
        elif isinstance(exception, sqlite3.OperationalError) and (
                "locked" in str(exception).lower() or "busy" in str(exception).lower()):
            waited = 0.0
        else:
            return None
        try:
            from . import hub_app
            hub_app.record_error(
                "hub.ledger",
                "ledger lock unavailable; answered 503 (retryable, nothing was written)",
                severity="warning", code="ledger_busy", details=str(exception)[:500],
                context={"component": "store", "path": request.path[:240],
                         "method": request.method, "waited_s": round(float(waited or 0), 1)})
        except Exception:                                    # noqa: BLE001
            pass                  # the refusal is served whether or not the record lands
        response = JsonResponse(
            {"errors": [{"code": "busy", "retry_after": int(self.RETRY_AFTER),
                         "msg": "the hub ledger is busy; nothing was written — retry"}]},
            status=503)
        response["Retry-After"] = self.RETRY_AFTER
        response._has_been_logged = True
        return response


class RouteTimingMiddleware:
    """How long each /hub route takes, per process, so a slow route is a NUMBER on
    /hub/perf.json instead of a "hub unreachable" banner on every client.

    A bounded ring per route (count, p50, p95, max, last) in memory, flushed to a sidecar every
    FLUSH_S so a reader sees every worker process. Samples expire after WINDOW_S: an unrelated
    request must never make an old slow route appear newly observed. Per-entity paths collapse
    to one route. An explicit ``perf.json?profile=snapshot`` is excluded — profiling adds
    interpreter overhead, and mixing it into ordinary samples manufactures a latency alarm.
    Fail-soft: timing must never be the reason a request fails. Dual-capable for the same
    reason as the middleware above (a persistent SSE stream must not be forced through a sync
    adaptation thread); for a streaming response the sample is the time to first byte."""

    FLUSH_S = 30
    RING = 200
    WINDOW_S = 3600
    sync_capable = True
    async_capable = True
    _ENTITY = re.compile(r"/(?:[0-9a-f]{8,}|[a-z][a-z0-9-]*-[0-9a-f]{6,}|\d{3,})[^/]*")

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)
        self._rings = {}
        self._flushed = 0.0
        self._timing_lock = threading.Lock()

    @classmethod
    def _route(cls, request) -> str:
        path = request.path_info or "/"
        if "/hub" not in path:
            return ""
        if path.endswith("/perf.json") and request.GET.get("profile"):
            return ""
        path = path[path.index("/hub"):]
        return cls._ENTITY.sub("/<id>", path.split("?")[0])[:80]

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        route = self._route(request)
        if not route:
            return self.get_response(request)
        started = time.perf_counter()
        try:
            return self.get_response(request)
        finally:
            self._sample(route, started)

    async def __acall__(self, request):
        route = self._route(request)
        if not route:
            return await self.get_response(request)
        started = time.perf_counter()
        try:
            return await self.get_response(request)
        finally:
            self._sample(route, started)

    def _sample(self, route, started):
        try:
            ms = int((time.perf_counter() - started) * 1000)
            now = time.time()
            with self._timing_lock:
                ring = self._rings.setdefault(route, [])
                ring.append((now, ms))
                if len(ring) > self.RING:
                    del ring[:len(ring) - self.RING]
                flush = now - self._flushed > self.FLUSH_S
                if flush:
                    self._flushed = now
            if flush:
                self._flush(now)
        except Exception:                                    # noqa: BLE001
            pass

    def _flush(self, now):
        from . import hub_app
        path = hub_app.HUB_DIR / "route-timings.json"
        with self._timing_lock:
            self._rings = {route: recent for route, ring in self._rings.items()
                           if (recent := [(at, ms) for at, ms in ring if now - at < self.WINDOW_S])}
            rings = {route: list(ring) for route, ring in self._rings.items()}
        mine = {}
        for route, ring in rings.items():
            ordered = sorted(ms for _at, ms in ring)
            # Nearest rank (ceil), small samples included: p95 of two samples is the slower one,
            # never below the reported median.
            mine[route] = {"count": len(ordered), "p50_ms": ordered[len(ordered) // 2],
                           "p95_ms": ordered[(95 * len(ordered) + 99) // 100 - 1],
                           "max_ms": ordered[-1], "last_ms": ring[-1][1], "at": ring[-1][0]}
        try:
            from hub_core.process_lock import ProcessFileLock
            hub_app.HUB_DIR.mkdir(parents=True, exist_ok=True)
            with ProcessFileLock(hub_app.HUB_DIR, name=".route-timings.lock", timeout=2):
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(current, dict):
                        current = {}
                except (OSError, ValueError):
                    current = {}
                current[str(os.getpid())] = {"version": 2, "at": now, "routes": mine}
                # Drop windows that have not flushed within the window (recycled processes).
                current = {pid: v for pid, v in current.items() if isinstance(v, dict)
                           and v.get("version") == 2
                           and 0 <= now - float(v.get("at") or 0) < self.WINDOW_S}
                tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
                tmp.write_text(json.dumps(current), encoding="utf-8")
                os.replace(tmp, path)
        except (OSError, TimeoutError):
            pass


def route_timings() -> dict:
    """Per-route latency across every PROCESS WINDOW that flushed within the last hour.

    NAMED FOR WHAT IT COMPUTES. Percentiles merge across process windows with max(), so
    ``p50_worst_ms``/``p95_worst_ms`` are the WORST SINGLE WINDOW's figures — not percentiles
    over all requests — and ``windows`` counts those windows (a restart starts a new one; it is
    not a count of concurrent workers). A field called "p50" that means max-of-p50s will be read
    as "the median is 40 s" by the first person who sees it. Counts add and are a true total."""
    from . import hub_app
    try:
        current = json.loads((hub_app.HUB_DIR / "route-timings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(current, dict):
        return {}
    out, newest = {}, {}
    now = time.time()
    for window in current.values():
        if not isinstance(window, dict) or window.get("version") != 2:
            continue
        if not 0 <= now - float(window.get("at") or 0) < RouteTimingMiddleware.WINDOW_S:
            continue
        for route, r in (window.get("routes") or {}).items():
            at = float(r.get("at") or 0)
            if not 0 <= now - at < RouteTimingMiddleware.WINDOW_S:
                continue
            o = out.setdefault(route, {"count": 0, "p50_worst_ms": 0, "p95_worst_ms": 0,
                                       "max_ms": 0, "last_ms": 0, "windows": 0})
            o["count"] += int(r.get("count") or 0)
            o["p50_worst_ms"] = max(o["p50_worst_ms"], int(r.get("p50_ms") or 0))
            o["p95_worst_ms"] = max(o["p95_worst_ms"], int(r.get("p95_ms") or 0))
            o["max_ms"] = max(o["max_ms"], int(r.get("max_ms") or 0))
            if at > newest.get(route, 0):
                o["last_ms"] = int(r.get("last_ms") or 0)
                newest[route] = at
            o["windows"] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["p95_worst_ms"]))


#: Routes that BLOCK by design (the inbox long-poll, the live stream): a slow sample is healthy.
LONG_POLL_ROUTES = ("/hub/inbox/wait", "/hub/live/events")


def slow_routes(timings=None, threshold_ms=None) -> list:
    """Routes whose worst-window p95 reached the slow-route threshold (HUB_SLOW_ROUTE_MS,
    default 8000), long-polls exempt."""
    try:
        threshold_ms = int(threshold_ms or os.environ.get("HUB_SLOW_ROUTE_MS") or 8000)
    except (TypeError, ValueError):
        threshold_ms = 8000
    timings = route_timings() if timings is None else timings
    return [route for route, v in timings.items()
            if v.get("p95_worst_ms", 0) >= threshold_ms
            and not any(route.endswith(lp) for lp in LONG_POLL_ROUTES)]
