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

    The same holds for the runtime lock (``TimeoutError("runtime lock busy …")``) and for a 503 a
    view RETURNS: only raised exceptions reach ``process_exception``, so a view that maps
    back-pressure itself returns a 503 whose first error carries a self-described code.
    ``SELF_DESCRIBED_503`` names those codes and whether the returning site already recorded its
    own row (True: only the duplicate ``django.request`` twin is suppressed) or not (False: this
    middleware writes the same warning the raised path writes, so both fold into ONE problem).
    Codes NOT in the table keep their error row on purpose — a real fault that answers 503 must
    still reach somebody. Extend the table by subclassing.

    Place it early in MIDDLEWARE (before any read gate) so a lock burst is answered whether the
    request was heading for a read or a write."""

    RETRY_AFTER = "2"
    # The write seam's own busy 503 records its ledger_busy row itself (record_ledger_busy).
    SELF_DESCRIBED_503 = {"busy": True}
    _MAX_SNIFF_BYTES = 4096
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
        return self._finish(request, self.get_response(request))

    async def __acall__(self, request):
        return self._finish(request, await self.get_response(request))

    def _finish(self, request, response):
        if getattr(response, "status_code", 200) == 503:
            if not response.has_header("Retry-After"):
                response["Retry-After"] = self.RETRY_AFTER
            self._absorb_self_described(request, response)
        return response

    # -- the returned 503 --
    def _absorb_self_described(self, request, response):
        if getattr(response, "_has_been_logged", False):
            return
        err = self._first_error(response)
        records_itself = self.SELF_DESCRIBED_503.get(str(err.get("code") or ""))
        if records_itself is None:
            return
        response._has_been_logged = True
        if not records_itself:
            self._record(request, str(err.get("msg") or err.get("code") or "")[:500],
                         returned=True)

    @classmethod
    def _first_error(cls, response):
        if getattr(response, "streaming", False):
            return {}
        if "json" not in (response.get("Content-Type") or "").lower():
            return {}
        try:
            body = response.content
        except Exception:                                    # noqa: BLE001
            return {}
        if not body or len(body) > cls._MAX_SNIFF_BYTES:
            return {}
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return {}
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return errors[0]
        return {}

    # -- the raised one --
    @staticmethod
    def is_busy(exception) -> bool:
        from hub_core.store import StoreBusy

        if isinstance(exception, StoreBusy):
            return True
        text = str(exception).lower()
        if isinstance(exception, TimeoutError):
            return "lock busy" in text
        if isinstance(exception, sqlite3.OperationalError):
            return "locked" in text or "busy" in text
        return False

    def process_exception(self, request, exception):
        if not self.is_busy(exception):
            return None
        self._record(request, exception, returned=False,
                     waited=float(getattr(exception, "waited_s", 0) or 0))
        response = JsonResponse(
            {"errors": [{"code": "busy", "retry_after": int(self.RETRY_AFTER),
                         "msg": "the hub ledger is busy; nothing was written — retry"}]},
            status=503)
        response["Retry-After"] = self.RETRY_AFTER
        response._has_been_logged = True
        return response

    def _record(self, request, details, *, returned, waited=0.0):
        """The one ledger_busy WARNING row, written through the same helper the write seam uses
        (hub_app.record_ledger_busy), so every busy refusal folds into ONE problem."""
        try:
            from . import hub_app
            hub_app.record_ledger_busy(request.path, request.method, waited,
                                       ("returned: " if returned else "") + str(details)[:480])
        except Exception:                                    # noqa: BLE001
            pass                    # the refusal is served whether or not the row can be written

class InFlightMiddleware:
    """OUTERMOST: how many requests this process serves at once, and when each ENTERED the
    Django stack (hub_core.write_timing). A write's Server-Timing then carries ``pre_view`` --
    its time in middleware before the view -- and /hub/perf.json ``process_pressure`` the
    concurrency at arrival, the routes holding threads right now and the interpreter's wake-up
    lag: together they say where a write's wall time goes when its handler is fast (a server
    whose every thread is held by a slow read queues every write behind it). Every path is
    counted, not just /hub ones -- a host app's slow page holds a thread the hub waits for too.
    Dual-capable; fail-soft: counting never breaks a request."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def _enter(self, request) -> bool:
        from hub_core import write_timing
        try:
            request._hub_entered, request._hub_in_flight = write_timing.enter(request.path_info)
            return True
        except Exception:                                    # noqa: BLE001
            return False

    @staticmethod
    def _leave():
        from hub_core import write_timing
        try:
            write_timing.leave()
        except Exception:                                    # noqa: BLE001
            pass

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        if not self._enter(request):
            return self.get_response(request)
        try:
            return self.get_response(request)
        finally:
            self._leave()

    async def __acall__(self, request):
        if not self._enter(request):
            return await self.get_response(request)
        try:
            return await self.get_response(request)
        finally:
            self._leave()


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


class WarmupHoldMiddleware:
    """While THIS process is still warming (``prewarm``), a heavy ``GET /hub/*`` read WAITS for
    the warm-up instead of computing a cold fold -- and past a shared deadline it is shed 503.

    Measured on the instance this was lifted from: a deploy rolled back because the trivial
    liveness probe waited in the server's queue behind every request thread, each parked in the
    cold fold or a cold board snapshot that every reconnecting client asked for at once while
    the prewarm held the interpreter. A thread that computes a cold read costs the whole
    warm-up; a thread that SLEEPS costs nothing and leaves the interpreter to the prewarm.

    So a read that arrives while the process warms sleeps in 0.1 s steps (never folding,
    never building a snapshot) and is served warm once ``prewarm`` reports done. Every hold ends
    at ONE deadline, ``HUB_WARMUP_HOLD_S`` (20 s) after the process started, so nothing -- a
    probe included -- waits behind the holds longer than that; at most ``HUB_WARMUP_HOLD_MAX``
    reads hold at once (default the server's pool, ``HUB_WAITRESS_THREADS``, else 16: a request
    that finds every thread holding waits in the queue and is served warm). A read past the
    deadline or the cap is SHED: 503 + Retry-After, a short self-refreshing page for a browser,
    marked as already logged (back-pressure, never a defect). Writes, ``/hub/perf.json`` and
    everything outside ``/hub/`` are never held. ``HUB_WARMUP_HOLD_S=0`` sheds at once;
    ``HUB_WARMUP_SHED=0`` turns it off. A process that never prewarms (tests, management
    commands, ``runserver``) is open from the start. Counts ride ``/hub/perf.json`` as
    ``warmup``. Dual-capable: an ASGI deployment holds with ``asyncio.sleep``."""

    PASS = ("/hub/perf.json",)
    MAX_S = 180.0
    HOLD_S = 20.0
    STATS = {"held": 0, "held_now": 0, "served_after_hold": 0, "hold_ms_max": 0.0,
             "shed": 0, "shed_hold_full": 0, "shed_hold_expired": 0}
    _STATS_LOCK = threading.Lock()
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)
        self.born = time.monotonic()
        self.on = str(os.environ.get("HUB_WARMUP_SHED", "1")).strip().lower() not in (
            "0", "false", "no", "off")
        self.open = not self.on
        try:
            self.hold_s = max(0.0, float(os.environ.get("HUB_WARMUP_HOLD_S", self.HOLD_S)))
        except ValueError:
            self.hold_s = self.HOLD_S
        try:
            pool = max(1, int(os.environ.get("HUB_WAITRESS_THREADS") or 16))
        except ValueError:
            pool = 16
        try:
            self.hold_max = max(0, int(os.environ.get("HUB_WARMUP_HOLD_MAX", pool)))
        except ValueError:
            self.hold_max = pool

    @classmethod
    def stats(cls) -> dict:
        with cls._STATS_LOCK:
            return dict(cls.STATS)

    def _warming(self) -> bool:
        if self.open:
            return False
        from . import prewarm
        state = prewarm.status().get("state")
        if state not in ("queued", "running") or time.monotonic() - self.born > self.MAX_S:
            self.open = True              # once open, never held or shed again in this process
            return False
        return True

    def _applies(self, request) -> bool:
        path = request.path_info or ""
        return (request.method == "GET" and path.startswith("/hub/") and path not in self.PASS
                and self._warming())

    def _count(self, key, add=1):
        with self._STATS_LOCK:
            self.STATS[key] += add

    def _enter(self) -> bool:
        if time.monotonic() >= self.born + self.hold_s:
            self._count("shed_hold_expired")
            return False
        with self._STATS_LOCK:
            if self.STATS["held_now"] >= self.hold_max:
                self.STATS["shed_hold_full"] += 1
                return False
            self.STATS["held_now"] += 1
            self.STATS["held"] += 1
        return True

    def _leave(self, t0, served):
        with self._STATS_LOCK:
            self.STATS["held_now"] -= 1
            if served:
                self.STATS["served_after_hold"] += 1
                self.STATS["hold_ms_max"] = max(self.STATS["hold_ms_max"],
                                                round((time.monotonic() - t0) * 1000.0, 1))
            else:
                self.STATS["shed_hold_expired"] += 1

    def _hold(self) -> bool:
        if not self._enter():
            return False
        t0, deadline, served = time.monotonic(), self.born + self.hold_s, False
        try:
            while self._warming():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                time.sleep(min(0.1, left))
            served = True
            return True
        finally:
            self._leave(t0, served)

    async def _ahold(self) -> bool:
        import asyncio
        if not self._enter():
            return False
        t0, deadline, served = time.monotonic(), self.born + self.hold_s, False
        try:
            while self._warming():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                await asyncio.sleep(min(0.1, left))
            served = True
            return True
        finally:
            self._leave(t0, served)

    def _shed(self, request):
        from django.http import HttpResponse
        self._count("shed")
        if "text/html" in (request.headers.get("Accept") or ""):
            # A PERSON gets a page that reloads itself, not a JSON envelope; same status, so a
            # proxy and an agent read it exactly as before.
            resp = HttpResponse(_WARMING_HTML, status=503, content_type="text/html; charset=utf-8")
            resp["Retry-After"] = "5"
        else:
            resp = JsonResponse({"errors": [{"code": "warming",
                                             "msg": "this hub process started moments ago and is "
                                                    "warming its fold; ask again shortly"}]},
                                status=503)
            resp["Retry-After"] = "15"
        resp._has_been_logged = True        # back-pressure, never a defect
        return resp

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        if self._applies(request) and not self._hold():
            return self._shed(request)
        return self.get_response(request)

    async def __acall__(self, request):
        if self._applies(request) and not await self._ahold():
            return self._shed(request)
        return await self.get_response(request)


_WARMING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Hub starting</title>
<style>:root{--bg:#f6f7f9;--fg:#1d2430;--mut:#5b6575}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--fg:#e6e9ee;--mut:#9aa4b2}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:0 16px}
main{max-width:34rem}h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:.25rem 0;color:var(--mut)}</style>
</head><body><main><h1>The hub restarted and is loading the board</h1>
<p>This process started moments ago and is warming its view of the ledger. Nothing is lost: this
page reloads itself every 5 seconds and shows the board as soon as it is ready.</p>
</main></body></html>"""
