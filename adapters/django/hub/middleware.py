import json
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
    """A busy ledger answers 503 + Retry-After — never 500, and never a red board row.

    Every Hub surface folds the event log, and a fold or an append can need a lock that is
    momentarily held: the store raises ``TimeoutError("ledger lock busy …")``, the runtime
    lock ``TimeoutError("runtime lock busy …")``, SQLite ``OperationalError("database is
    locked")``. Uncaught, each climbs out of the view as an Internal Server Error, a traceback,
    and — because Django logs every 5xx through ``django.request`` at ERROR, the stream the
    Hub ingests — a red row on the problem queue for a condition that is transient by
    construction. 503 is the true statement: nothing was written, come back in a moment. A
    client that retries writes then clears it without a person ever being asked to look.

    WHY IT IS NOT LOGGED AS AN ERROR. ``django.utils.log.log_response`` skips a response
    already marked ``_has_been_logged``; the row written instead is a WARNING carrying the
    path, which the queue's read-time bar counts and never queues.

    AND THE SAME FOR A 503 A VIEW *RETURNS*. Only raised exceptions reach
    ``process_exception``. A view that maps back-pressure itself returns a 503 whose first
    error carries a self-described code; ``SELF_DESCRIBED_503`` names those codes and whether
    the returning site already recorded its own row (True: only the duplicate
    ``django.request`` twin is suppressed) or not (False: this middleware writes the same
    warning the raised path writes, so both fold into ONE problem). Codes NOT in the table keep
    their error row on purpose — a real fault that answers 503 must still reach somebody;
    silencing it would be the opposite failure. Extend the table by subclassing.

    Place it early in MIDDLEWARE so a lock burst is answered whichever view it hit.
    """

    RETRY_AFTER = "2"
    SELF_DESCRIBED_503 = {"busy": False}
    _MAX_SNIFF_BYTES = 4096
    _MESSAGE = "ledger lock unavailable; answered 503 (retryable, nothing was written)"
    # Dual-capable for the same reason as NoStoreHTMLMiddleware: a sync-only middleware would
    # force the persistent SSE rail through Django's sync adaptation thread under ASGI.
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
        text = str(exception).lower()
        if isinstance(exception, TimeoutError):
            return "lock busy" in text
        if isinstance(exception, sqlite3.OperationalError):
            return "locked" in text or "busy" in text
        return False

    def process_exception(self, request, exception):
        if not self.is_busy(exception):
            return None
        self._record(request, str(exception)[:500], returned=False)
        response = JsonResponse(
            {"errors": [{"code": "busy", "retry_after": int(self.RETRY_AFTER),
                         "msg": "the hub ledger is busy; nothing was written — retry"}]},
            status=503)
        response["Retry-After"] = self.RETRY_AFTER
        response._has_been_logged = True
        return response

    def _record(self, request, details, *, returned):
        try:
            from . import hub_app
            hub_app.record_error(
                "hub.ledger", self._MESSAGE, severity="warning", code="ledger_busy",
                details=details,
                context={"component": "store", "path": request.path[:240],
                         "method": request.method,
                         "operation": "returned" if returned else "raised"})
        except Exception:                                    # noqa: BLE001
            pass                    # the refusal is served whether or not the row can be written
