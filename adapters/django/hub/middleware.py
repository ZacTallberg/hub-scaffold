import sqlite3

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
