"""Server-side failure capture and the hub forwarder.

A Django exception lands in a service log on a host most readers have no shell on. This
module lands every failure in ONE local table (the same one the browser reports into) and
forwards it to the hub's operational error stream, attributed to this app. It is armed from
``AppConfig.ready()`` (``apps.py`` does it) and needs nothing else from the app:

* ``got_request_exception``            -> ``server`` (a view raised)
* a root logging handler                -> ``django`` / ``data`` / ``other`` for any log.error
* ``threading.excepthook``/``sys.excepthook`` -> ``background`` (a thread or process died)
* ``record_agent`` / ``agent_faults``   -> ``agent`` (a fault inside an agentic chat loop)
* the browser half (``report_errors.js`` -> ``views.report``) -> js/promise/http/stream/...

Rules, each one paid for by a real incident on the system this was extracted from:

* RECORDING IS FAIL-SOFT, AND ITS OWN DEATH IS AUDIBLE. ``record()`` never raises into the
  caller; when it cannot write, it says so on a path that cannot re-enter it.
* A FAILED WRITE MUST NOT POISON THE CALLER'S TRANSACTION -- the write runs in a savepoint.
* A RECORD FROM INSIDE AN EVENT LOOP hops to a worker thread (Django refuses an ORM write
  from a running loop, and a swallowed refusal writes NOTHING while looking installed).
* IDENTICAL FAILURES ARE ONE ROW, with a count; a burst cap per fingerprint stops a hot loop.
* A TRACEBACK IS KEPT FOR ITS END (``fit_stack``): head and tail, gap stated.
* DESIGNED DEGRADATION IS NOT A FAULT: a readiness probe's 503, a view's handled 502/504 about
  its upstream and a peer hanging up are warnings; a handled gateway answer on a route that
  proxies the HUB ITSELF is recorded locally and not forwarded (the hub knows it is down).
* THE FORWARDER PROVES ITS CHAIN: one ``info`` arming row per serving process stating which
  producers armed (``error`` if one failed), ``forwarding_status()`` for delivery, a test run
  never forwards, and ``manage.py error_selftest`` fires every producer for real.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)

MAX_MESSAGE = 4000
#: The exception type, its message and the raising frame sit at the END of a trace, so an
#: oversized one keeps both ends (see fit_stack). 32 KB holds a deep chained traceback.
MAX_STACK = 32000
STACK_HEAD = 4000
BURST_WINDOW_S = 60
BURST_MAX_WRITES = 6          # per fingerprint per window

# Volatile fragments kept out of a fingerprint, or "the same bug" splits into many rows.
_VOLATILE = re.compile(
    r"0x[0-9a-f]+|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b\d{3,}\b",
    re.I)

_burst: dict[str, tuple[float, int]] = {}

# Re-entrancy guard. `.active` is held by record() for one write; `.on` by the log handler
# for one emit. A database fault logs an error, the handler records it, the write fails and
# logs again -- the burst limiter cannot stop that loop (each nested message differs).
_reentry = threading.local()


def _model():
    """Resolved lazily so this module imports before the app registry is ready."""
    from django.apps import apps
    return apps.get_model("app_errors", "AppError")


def fit_stack(stack, limit=MAX_STACK):
    """Keep both ends of an oversized traceback and say what was dropped."""
    text = str(stack or "")
    if len(text) <= limit:
        return text
    tail = limit - STACK_HEAD
    return "%s\n... %d characters elided ...\n%s" % (
        text[:STACK_HEAD], len(text) - STACK_HEAD - tail, text[-tail:])


def fingerprint(kind: str, message: str, source: str = "") -> str:
    basis = "|".join([kind, _VOLATILE.sub("#", message or "")[:400],
                      _VOLATILE.sub("#", source or "")[:200]])
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:40]


def _burst_ok(fp: str) -> bool:
    now = time.monotonic()
    started, n = _burst.get(fp, (now, 0))
    if now - started > BURST_WINDOW_S:
        _burst[fp] = (now, 1)
        return True
    if n >= BURST_MAX_WRITES:
        return False
    _burst[fp] = (started, n + 1)
    return True


# =============================================================================
# A RECORD FROM INSIDE THE EVENT LOOP
# =============================================================================
# The write hops to a short-lived worker thread, joined for a bounded time. The burst cap is
# spent BEFORE the hop, once, so a hot loop cannot start a thread per failure and a hopped
# record is not counted twice.
RECORD_HOP_JOIN_S = 2.0
_loop_hop = threading.local()


def _in_running_loop() -> bool:
    try:
        import asyncio
        asyncio.get_running_loop()
        return True
    except Exception:                  # noqa: BLE001 -- RuntimeError means "no loop here"
        return False


def _close_thread_connection() -> None:
    """A worker thread's database connection is its own; nothing else would close it."""
    try:
        from django.db import connection
        if not connection.in_atomic_block:
            connection.close()
    except Exception:                  # noqa: BLE001 -- hygiene, never the failure
        pass


def _record_off_loop(kind: str, message: str, **fields) -> None:
    try:
        if not _burst_ok(fingerprint(kind, message, fields.get("source", ""))):
            return

        def _write():
            _loop_hop.burst_checked = True
            try:
                record(kind, message, **fields)
            finally:
                _loop_hop.burst_checked = False
                _close_thread_connection()

        worker = threading.Thread(target=_write, name="app-errors-record", daemon=True)
        worker.start()
        worker.join(RECORD_HOP_JOIN_S)
    except Exception as exc:           # noqa: BLE001 -- fail-soft, by contract
        log.exception("app_errors: could not hand a %s failure off the event loop", kind)
        _announce_recorder_fault(kind, exc)


# =============================================================================
# WHEN THE RECORDER ITSELF CANNOT RECORD
# =============================================================================
# A recorder that swallows its own failure is indistinguishable, from every surface anyone
# reads, from an app with no errors. So its death is announced -- on a path that never
# touches the ORM, never calls record(), holds its own re-entrancy flag, and is bounded by
# interval AND a per-process ceiling, so a permanently broken database costs a handful of
# rows, not a flood. The counter always moves, so recorder_faults() answers locally.
_FAULT_MIN_INTERVAL_S = 300
_FAULT_MAX_ANNOUNCEMENTS = 12
_fault_state = {"faults": 0, "announced": 0, "last_detail": "", "last_at": 0.0}
_fault_lock = threading.Lock()
_fault_reentry = threading.local()


def recorder_faults() -> dict:
    """How many times THIS PROCESS could not write a row, and what failed last."""
    with _fault_lock:
        return dict(_fault_state)


def _announce_recorder_fault(kind: str, exc: BaseException) -> None:
    if getattr(_fault_reentry, "on", False):
        return
    _fault_reentry.on = True
    try:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        now = time.monotonic()
        with _fault_lock:
            _fault_state["faults"] += 1
            _fault_state["last_detail"] = ("%s: %s" % (type(exc).__name__, exc))[:MAX_MESSAGE]
            n = _fault_state["faults"]
            due = (_fault_state["announced"] < _FAULT_MAX_ANNOUNCEMENTS
                   and (_fault_state["announced"] == 0
                        or now - _fault_state["last_at"] >= _FAULT_MIN_INTERVAL_S))
            if due:
                _fault_state["announced"] += 1
                _fault_state["last_at"] = now
        if not due:
            return
        # stderr first: it works with no database, no settings and no network.
        try:
            sys.stderr.write("app_errors: RECORDER FAILED (%d so far) writing a %s row: %s\n"
                             % (n, kind, _fault_state["last_detail"]))
        except Exception:              # noqa: BLE001
            pass
        _hub_forward("other",
                     "error recorder FAILED to write a %s row (%d this process): %s"
                     % (kind, n, _fault_state["last_detail"]),
                     detail, "app_errors recorder", "", severity="critical",
                     code="app_recorder_failed", automatic=True)
    except Exception:                  # noqa: BLE001 -- the last belt; never raise from here
        pass
    finally:
        _fault_reentry.on = False


def record(kind: str, message: str, *, stack: str = "", source: str = "",
           page_url: str = "", status: int | None = None, actor: str = "",
           user_agent: str = "", severity: str = "error", forward: bool = True) -> None:
    """Land one failure locally, then forward it. Never raises.

    ``severity`` is what the HUB is told; the local row is written either way.
    ``forward=False`` keeps the row local -- only for handled degradation of something this
    app merely proxies, which the hub hears about from the thing that actually broke.
    """
    if getattr(_reentry, "active", False):
        return
    if _in_running_loop():
        _record_off_loop(kind, message, stack=stack, source=source, page_url=page_url,
                         status=status, actor=actor, user_agent=user_agent, severity=severity,
                         forward=forward)
        return
    _reentry.active = True
    try:
        Model = _model()
        fp = fingerprint(kind, message, source)
        if not getattr(_loop_hop, "burst_checked", False) and not _burst_ok(fp):
            return
        from django.db import transaction
        from django.db.models import F
        from django.utils import timezone
        # A nested atomic() is a SAVEPOINT: a failed insert (a missing table, a constraint)
        # rolls back this write and nothing else, so the caller's next query does not raise
        # TransactionManagementError and get blamed for it.
        with transaction.atomic():
            updated = Model.objects.filter(fingerprint=fp).update(
                count=F("count") + 1, last_seen=timezone.now(),
                resolved_at=None)          # a recurrence re-opens a closed row
            if not updated:
                Model.objects.create(
                    fingerprint=fp, kind=kind,
                    message=(message or "")[:MAX_MESSAGE],
                    stack=fit_stack(stack),
                    source=(source or "")[:500],
                    page_url=(page_url or "").split("?")[0][:500],
                    status=status, actor=(actor or "")[:150],
                    user_agent=(user_agent or "")[:300])
        # Local row first, always. Forwarding is an extra, never a dependency.
        if forward:
            _hub_forward(kind, message, stack, source, page_url, severity=severity,
                         automatic=True)
    except Exception as exc:           # noqa: BLE001 -- fail-soft, by contract
        log.exception("app_errors: could not record a %s failure", kind)
        _announce_recorder_fault(kind, exc)
    finally:
        _reentry.active = False


def _captured_path(request) -> str:
    """The PATH as the address bar shows it (script prefix included), never the query."""
    if request is None:
        return ""
    try:
        return str(getattr(request, "path", "") or "")
    except Exception:                  # noqa: BLE001 -- never mask the real error
        return ""


def _actor(request) -> str:
    try:
        return str(getattr(getattr(request, "user", None), "username", "") or "")
    except Exception:                  # noqa: BLE001
        return ""


def _on_request_exception(sender, request=None, **kwargs):
    exc_type, exc, tb = sys.exc_info()
    if exc is None:
        return
    record("server", f"{exc_type.__name__}: {exc}",
           stack="".join(traceback.format_exception(exc_type, exc, tb)),
           source=f"{getattr(request, 'method', '?')} {_captured_path(request)}",
           page_url=_captured_path(request), status=500, actor=_actor(request),
           user_agent=(request.META.get("HTTP_USER_AGENT", "") if request else ""))


def install() -> None:
    """Arm every server-side producer, then send the arming row. Idempotent."""
    from django.core.signals import got_request_exception
    got_request_exception.connect(_on_request_exception,
                                  dispatch_uid="app_errors.request_exception")
    install_log_capture()
    install_excepthooks()
    _hub_arm()


def record_background(where: str, exc: BaseException) -> None:
    """For a job that catches its own exception but still wants it on the board."""
    record("background", f"{type(exc).__name__}: {exc}",
           stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
           source=where)


# =============================================================================
# AUTOMATIC PRODUCERS: django / data / other / background
# =============================================================================
# A kind that nothing produces is a silent channel. Classification is explicit, and its
# failure mode is a mislabelled row, never a missing one: anything unrecognised is `other`.
_LOG_LEVEL = logging.ERROR
_auto_state = {"logging": False, "excepthooks": False, "failed": {}}
_HANDLER_NAME = "app_errors_log_capture"

# The recorder logs its own failures; without this the first failed write recurses.
_SELF_LOGGERS = ("app_errors", __name__)
_ACCESS_LOG_LOGGER = "django.server"

# Exception types that mean THE DATA was wrong rather than the code. Named literally,
# because a fuzzy rule here would quietly relabel half the board.
_DATA_EXCEPTIONS = {
    "IntegrityError", "DataError", "ValidationError", "JSONDecodeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "InvalidOperation", "ParserError",
    "EmptyDataError", "XMLSyntaxError", "ExpatError", "DecodeError",
    "InvalidHeaderError", "BadZipFile", "UnpicklingError",
}

# A request this app REFUSED (Django's SuspiciousOperation family on django.security.*) is
# the check working, not a defect: kept, and told to the hub as a warning.
_REFUSED_INBOUND_LOGGER = "django.security"


def _refused_inbound(logger_name: str) -> bool:
    return (logger_name == _REFUSED_INBOUND_LOGGER
            or logger_name.startswith(_REFUSED_INBOUND_LOGGER + "."))


# A peer that hung up: asyncio logs a connection reset when a client (a closed tab, a proxy
# dropping an idle upstream, a cancelled stream) closes first. Nothing here can prevent it.
_PEER_DISCONNECT_LOGGER = "asyncio"
_PEER_DISCONNECTS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


def _peer_disconnect(logger_name: str, exc_type) -> bool:
    return (logger_name == _PEER_DISCONNECT_LOGGER and isinstance(exc_type, type)
            and issubclass(exc_type, _PEER_DISCONNECTS))


# A readiness probe answering 503 is the probe WORKING: it refuses traffic by design while
# the app warms up, and the deploy polls it through exactly that window. Keyed on the path
# and the status, never the message; kept as a warning, so a readiness that stays down is
# still counted, and a 503 from any other view still pages.
_PROBE_PATHS = ("/health/ready/", "/health/live/")


def _probe_warmup(rec) -> bool:
    if getattr(rec, "name", "") != "django.request" or getattr(rec, "status_code", None) != 503:
        return False
    request = getattr(rec, "request", None)
    path = str(getattr(request, "path_info", "") or getattr(request, "path", "") or "")
    return path.endswith(_PROBE_PATHS)


# A view that ANSWERS 502/504 is reporting its upstream, not itself. 503 is excluded: a view
# answering 503 is this app saying IT is unavailable, and that still pages.
_UPSTREAM_STATUSES = frozenset({502, 504})


def _answered(rec) -> bool:
    exc_info = getattr(rec, "exc_info", None)
    return not (exc_info and exc_info[0] is not None)


def _upstream_answer(rec) -> bool:
    return (getattr(rec, "name", "") == "django.request" and _answered(rec)
            and getattr(rec, "status_code", None) in _UPSTREAM_STATUSES)


# A ROUTE THAT PROXIES THE HUB ITSELF (a hub-hosted feed, a shared banner, a profile read)
# answering a gateway status is the hub being down. The hub already knows; twenty apps each
# filing it as their own fault is noise, and a hub problem's severity may freeze at its first
# row, so even a demoted row would re-open it. Recorded locally in full, NOT forwarded.
# Narrow by construction: only django.request, only an ANSWERED status (never a raised
# fault), only 502/503/504, and only on the suffixes the app declares in
# settings.APP_ERRORS_HUB_BRIDGE_PATHS. A 500 there, or a gateway answer anywhere else, is
# untouched.
_BRIDGE_STATUSES = frozenset({502, 503, 504})
_BRIDGE_REASONS = ("Bad Gateway: ", "Service Unavailable: ", "Gateway Timeout: ")


def _bridge_paths() -> tuple:
    try:
        from django.conf import settings
        return tuple(getattr(settings, "APP_ERRORS_HUB_BRIDGE_PATHS", ()) or ())
    except Exception:                  # noqa: BLE001
        return ()


def _is_bridge_degradation(rec) -> bool:
    suffixes = _bridge_paths()
    if not suffixes or getattr(rec, "name", "") != "django.request" or not _answered(rec):
        return False
    status = getattr(rec, "status_code", None)
    try:
        message = rec.getMessage()
    except Exception:                  # noqa: BLE001
        message = ""
    if status is None:
        if not message.startswith(_BRIDGE_REASONS):
            return False
    elif status not in _BRIDGE_STATUSES:
        return False
    request = getattr(rec, "request", None)
    path = str(getattr(request, "path_info", "") or getattr(request, "path", "") or "")
    if not path:
        path = message.split(": ", 1)[-1].strip() if ": " in message else ""
    return bool(path) and path.split("?")[0].endswith(suffixes)


def classify(rec) -> tuple[str, str, bool]:
    """(kind, severity, forward) for one ERROR+ log record. One place, every handler."""
    name = rec.name or ""
    exc_name = rec.exc_info[0].__name__ if rec.exc_info and rec.exc_info[0] is not None else ""
    if exc_name in _DATA_EXCEPTIONS:
        return "data", "error", True
    if _refused_inbound(name):
        return "django", "warning", True
    if _peer_disconnect(name, rec.exc_info[0] if rec.exc_info else None):
        return "other", "warning", True
    if _probe_warmup(rec):
        return "django", "warning", True
    if _is_bridge_degradation(rec):
        return "django", "warning", False
    if _upstream_answer(rec):
        return "django", "warning", True
    if name == "django" or name.startswith("django."):
        return "django", "error", True
    return "other", "error", True


class _LogCapture(logging.Handler):
    """Land every ERROR-and-above logging record as a row. Never raises, never loops."""

    def emit(self, rec) -> None:                                   # noqa: D102
        if getattr(_reentry, "on", False):
            return
        # One record, one row, however many loggers deliver it (root AND a non-propagating one).
        if getattr(rec, "_app_errors_seen", False):
            return
        try:
            rec._app_errors_seen = True
        except Exception:                                          # noqa: BLE001
            pass
        # A caller that records the failure itself (record_agent, record_background) and also
        # logs it marks the line extra={"app_errors_recorded": True}: one failure, one row.
        if getattr(rec, "app_errors_recorded", False):
            return
        try:
            _reentry.on = True
            name = rec.name or ""
            if name.startswith(_SELF_LOGGERS):
                return
            # The development server's ACCESS LOG restates, at ERROR, every 5xx response that
            # django.request already reported -- a second row that would also bypass the
            # readiness/upstream/bridge rules below, which read django.request records.
            if name == _ACCESS_LOG_LOGGER:
                return
            exc_name = ""
            if rec.exc_info and rec.exc_info[0] is not None:
                exc_name = rec.exc_info[0].__name__
            # django.request logs every raised 500 WITH the exception, and
            # got_request_exception already recorded it as `server`. The same logger without
            # an exception (a view that RETURNED a 5xx) is covered nowhere else, so it stays.
            if name == "django.request" and exc_name:
                return
            kind, severity, forward = classify(rec)
            message = rec.getMessage()
            if exc_name and exc_name not in message:
                message = f"{exc_name}: {message}"
            stack = ""
            if rec.exc_info and rec.exc_info[0] is not None:
                stack = "".join(traceback.format_exception(*rec.exc_info))
            record(kind, message[:MAX_MESSAGE], stack=stack,
                   source=f"{name} {rec.module}:{rec.lineno}", severity=severity,
                   forward=forward)
        except Exception:                  # noqa: BLE001 -- a handler must never raise
            try:
                self.handleError(rec)      # writes to stderr instead of vanishing
            except Exception:              # noqa: BLE001
                pass
        finally:
            _reentry.on = False


def install_log_capture() -> None:
    """Attach _LogCapture to root AND to every django logger that stops propagation."""
    if _auto_state["logging"]:
        return
    try:
        root = logging.getLogger()
        handler = next((h for h in root.handlers if getattr(h, "name", "") == _HANDLER_NAME), None)
        if handler is None:
            handler = _LogCapture(level=_LOG_LEVEL)
            handler.set_name(_HANDLER_NAME)
            root.addHandler(handler)
        _attach_to_stoppers(handler)
        # LOGGING can be applied AGAIN after ready(): get_wsgi_application() calls
        # django.setup(), and runserver (or any server that builds the WSGI app after the
        # command already set Django up) therefore re-runs dictConfig, which strips every
        # handler from the loggers LOGGING names while ready() does not run a second time.
        # Observed: the serving process's django.request carried only its console handler, so
        # every RETURNED 5xx was dark. Re-checking at each request start heals that for good.
        from django.core.signals import request_started
        request_started.connect(_ensure_log_capture, dispatch_uid="app_errors.log_capture")
        _auto_state["logging"] = True
    except Exception as exc:               # noqa: BLE001
        _note_arm_failure("logging", exc)


def _attach_to_stoppers(handler) -> None:
    """A logger with propagate=False never reaches root. A project that gives django.request
    (or `django` itself) its own handlers with propagate=False would leave a view's RETURNED
    5xx -- the one case the request signal cannot see -- dark. Attach there too."""
    for name in ["django"] + sorted(n for n in list(logging.root.manager.loggerDict)
                                    if isinstance(n, str) and n.startswith("django.")):
        stopper = logging.getLogger(name)
        if not stopper.propagate and handler not in stopper.handlers:
            stopper.addHandler(handler)


def _ensure_log_capture(sender=None, **kwargs) -> None:
    """Re-attach after a logging reconfiguration. Cheap and idempotent; never raises."""
    try:
        root = logging.getLogger()
        handler = next((h for h in root.handlers if getattr(h, "name", "") == _HANDLER_NAME), None)
        if handler is None:
            handler = _LogCapture(level=_LOG_LEVEL)
            handler.set_name(_HANDLER_NAME)
            root.addHandler(handler)
        _attach_to_stoppers(handler)
    except Exception:                      # noqa: BLE001 -- a signal receiver must never raise
        pass


def _note_arm_failure(producer: str, exc: BaseException) -> None:
    """An installer that failed leaves its whole lane dark for the life of the process. The
    arming row reports it (and turns error); stderr covers a process that dies before that."""
    _auto_state["failed"] = dict(_auto_state.get("failed") or {},
                                 **{producer: "%s: %s" % (type(exc).__name__, exc)})
    try:
        sys.stderr.write("app_errors: could not arm the %s producer: %s: %s\n"
                         % (producer, type(exc).__name__, exc))
    except Exception:                      # noqa: BLE001
        pass


def install_excepthooks() -> None:
    """Capture an exception that kills a worker thread or the process. Idempotent."""
    if _auto_state["excepthooks"]:
        return
    try:
        previous_thread_hook = threading.excepthook

        def _thread_hook(args):
            try:
                if args.exc_value is not None and not issubclass(
                        args.exc_type, (SystemExit, KeyboardInterrupt)):
                    record("background", f"{args.exc_type.__name__}: {args.exc_value}",
                           stack="".join(traceback.format_exception(
                               args.exc_type, args.exc_value, args.exc_traceback)),
                           source=f"thread {getattr(args.thread, 'name', '?')}")
            except Exception:              # noqa: BLE001
                pass
            return previous_thread_hook(args)

        threading.excepthook = _thread_hook
        previous_sys_hook = sys.excepthook

        def _sys_hook(exc_type, exc_value, tb):
            try:
                if not issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
                    argv = sys.argv or []
                    where = " ".join([os.path.basename(argv[0] if argv else "python")] + argv[1:3])
                    record("background", f"{exc_type.__name__}: {exc_value}",
                           stack="".join(traceback.format_exception(exc_type, exc_value, tb)),
                           source=f"process {where}"[:500])
            except Exception:              # noqa: BLE001
                pass
            return previous_sys_hook(exc_type, exc_value, tb)

        sys.excepthook = _sys_hook
        _auto_state["excepthooks"] = True
    except Exception as exc:               # noqa: BLE001
        _note_arm_failure("excepthooks", exc)


# =============================================================================
# FORWARDING TO THE HUB
# =============================================================================
# EVERY CLASS FORWARDS -- server, background, django, data, other, agent and the browser's
# js/promise/http/stream. Noise is controlled by the burst cap here, the browser's own caps,
# and the hub's per-fingerprint throttle and read-time bar -- never by dropping a report,
# which destroys the one copy nobody else holds. Best-effort: a short timeout, its own
# thread, every exception swallowed but COUNTED (forwarding_status()).
#
# THE WIRE CONTRACT (POST <HUB_API_BASE>/api/app-error, X-Agent-Token with error:report):
#   {app, kind, severity, message, details, code, path, operation, host}
# and nothing else. No `agent` field: the scoped token already names the sender, and a
# field that disagrees with the credential's subject is exactly what a hub may refuse.
FORWARD_KINDS = {"js", "promise", "http", "stream", "server", "django", "data",
                 "background", "other", "agent"}

ARM_KIND = "forwarder"
ARM_CODE = "app_forwarder_armed"
_ARMED = {"sent": False}


def _is_serving_process() -> bool:
    """Only a SERVING process sends the arming row: every manage.py command boots the apps
    too (migrations, tests, scheduled jobs), and each would otherwise post one."""
    argv = list(sys.argv or [])
    argv0 = os.path.basename(argv[0] or "") if argv else ""
    if argv0 in ("manage.py", "django-admin", "django-admin.py", "django-admin.exe"):
        return argv[1:2] == ["runserver"]
    return True


def _hub_arm() -> None:
    """Send the startup arming row once per serving process. "No rows" from an app cannot
    tell "nothing failed" from "the forwarder is unconfigured"; one delivered row at boot
    separates them. It states WHAT armed, and is `error` rather than `info` when a producer
    failed to -- a boot with a dark lane is not a healthy boot."""
    if _ARMED["sent"] or not _is_serving_process():
        return
    _ARMED["sent"] = True
    deploy = (os.environ.get("APP_DEPLOY_ID") or "").strip()
    failed = _auto_state.get("failed") or {}
    producers = "log_capture=%s excepthooks=%s" % (
        _auto_state.get("logging"), _auto_state.get("excepthooks"))
    detail = "; ".join("%s -> %s" % (k, v) for k, v in sorted(failed.items()))
    slug = _forward_config()[2] or "app"
    _hub_forward(ARM_KIND,
                 "forwarder armed: %s on %s (%s)%s" % (
                     slug, socket.gethostname(), producers,
                     " PRODUCER FAILED TO ARM" if failed else ""),
                 detail, ("deploy %s" % deploy) if deploy else "startup", "",
                 severity=("error" if failed else "info"), code=ARM_CODE)


# A DIAGNOSTIC MUST NOT BECOME THE NOISE IT MEASURES. error_selftest holds this while it
# fabricates one failure per producer, and PROVES the switch is read before it fires.
_suppress_forward = {"on": False}


def _under_test() -> bool:
    """True in a TEST RUN, so a fixture's fabricated failure never reaches the live board.

    Every signal here is one that cannot be true in a serving process: a false positive
    would silence a real production error."""
    argv = list(sys.argv or [])
    argv0 = os.path.basename(argv[0] or "") if argv else ""
    if argv0 in ("manage.py", "django-admin", "django-admin.py", "django-admin.exe"):
        if argv[1:2] == ["test"]:
            return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    # setup_test_environment() creates mail.outbox and teardown deletes it.
    try:
        from django.core import mail
    except Exception:                                        # noqa: BLE001
        return False
    return hasattr(mail, "outbox")


def _forward_config() -> tuple[str, str, str]:
    """(hub api base, token, slug) -- the ONE resolution, shared by the forwarder and the
    selftest, so a probe can never report a phantom gap the forwarder does not have.
    Settings first, then the environment."""
    try:
        from django.conf import settings
    except Exception:                                        # noqa: BLE001
        settings = None

    def _pick(name: str) -> str:
        value = getattr(settings, name, "") if settings is not None else ""
        return str(value or os.environ.get(name) or "")

    base = _pick("HUB_API_BASE").strip().rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    return base, _pick("HUB_AGENT_TOKEN").strip(), _pick("APP_SLUG").strip().lower()


def _forward_under_test_allowed() -> bool:
    try:
        from django.conf import settings
        return bool(getattr(settings, "HUB_FORWARD_UNDER_TEST", False))
    except Exception:                                        # noqa: BLE001
        return False


# DID A FORWARD ACTUALLY ARRIVE? In memory, not a row, so it stays readable when recording
# itself is broken. `detail` may be published on a health endpoint, so its SHAPE is
# whitelisted: an HTTP status or an exception class name, nothing else.
_last_forward = {"ok": None, "detail": "", "at": 0.0, "sent": 0, "failed": 0}
_DETAIL_OK = re.compile(r"\AHTTP [1-5][0-9]{2}\Z|\A[A-Za-z_][A-Za-z0-9_]{0,39}\Z")


def _note_forward(ok: bool, detail: str) -> None:
    text = str(detail)[:80]
    if not _DETAIL_OK.match(text):
        text = "unpublishable_detail"
    _last_forward.update(ok=bool(ok), detail=text, at=time.time())
    _last_forward["sent" if ok else "failed"] += 1


def forwarding_status() -> dict:
    """Is forwarding ARMED in this process, and did the last forward ARRIVE? Names,
    booleans and counts only -- the token never appears. Safe on a health endpoint."""
    out = {"url": False, "token": False, "slug": "", "armed": False,
           "last_ok": None, "last_detail": "", "last_at": 0.0, "sent": 0, "failed": 0}
    try:
        base, token, slug = _forward_config()
        out.update(url=bool(base), token=bool(token), slug=slug,
                   armed=bool(base and token and slug))
        out.update(last_ok=_last_forward["ok"], last_detail=_last_forward["detail"],
                   last_at=_last_forward["at"], sent=_last_forward["sent"],
                   failed=_last_forward["failed"])
    except Exception:                                        # noqa: BLE001
        pass
    return out


def build_payload(kind, message, stack, source, page_url, *, severity="error", code=None,
                  slug="") -> dict:
    """The app-error body. Exactly the hub's documented fields; see THE WIRE CONTRACT."""
    return {"app": slug, "kind": kind, "severity": severity,
            "message": str(message or "")[:800], "details": fit_stack(stack),
            "code": code or ("app_" + kind), "path": str(page_url or "")[:240],
            "operation": str(source or "")[:120], "host": socket.gethostname()}


def post_to_hub(payload: dict, *, timeout: float = 5) -> tuple[bool, str]:
    """POST one app-error body and READ the answer. Returns (accepted, detail)."""
    import json as _json
    import ssl as _ssl
    import urllib.request as _req
    base, token, _slug = _forward_config()
    request = _req.Request(base + "/api/app-error",
                           data=_json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("X-Agent-Token", token)
    context = None
    if base.startswith("https://"):
        # Verification stays ON. A private CA is named, never disabled.
        try:
            from django.conf import settings
            bundle = getattr(settings, "HUB_CA_BUNDLE", "") or os.environ.get("HUB_CA_BUNDLE", "")
        except Exception:                                    # noqa: BLE001
            bundle = os.environ.get("HUB_CA_BUNDLE", "")
        context = _ssl.create_default_context(cafile=bundle or None)
    with _req.urlopen(request, timeout=timeout, context=context) as response:
        response.read()
        status = getattr(response, "status", 200)
        return 200 <= status < 300, "HTTP %s" % status


def _hub_forward(kind, message, stack, source, page_url, *, severity="error", code=None,
                 automatic=False):
    if _suppress_forward["on"]:
        return
    # A TEST RUN NEVER SPEAKS TO THE BOARD on the automatic path (record -> forward). A
    # direct call is a test deliberately exercising the forwarder with the network stubbed.
    if automatic and _under_test() and not _forward_under_test_allowed():
        return
    if kind not in FORWARD_KINDS and kind != ARM_KIND:
        return
    base, token, slug = _forward_config()
    if not (base and token and slug):
        return          # not configured is not an error; the app is simply local-only
    payload = build_payload(kind, message, stack, source, page_url, severity=severity,
                            code=code, slug=slug)

    def _send():
        try:
            ok, detail = post_to_hub(payload)
            _note_forward(ok, detail)
        except Exception as exc:                             # noqa: BLE001
            # Never a second failure inside the handler for the first -- but COUNTED, or a
            # forwarder that fails every time looks like one with nothing to send.
            try:
                http_status = getattr(exc, "code", None)
                _note_forward(False, ("HTTP %s" % http_status) if http_status
                              else type(exc).__name__)
            except Exception:                                # noqa: BLE001
                pass

    try:
        threading.Thread(target=_send, name="app-errors-forward", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        pass


# =============================================================================
# AGENTIC CHATS: a failed turn reaches the board, not just the person watching it
# =============================================================================
# kind `agent`, carrying thread / turn / tool so it can be found -- NEVER the prompt. From
# `async def` code always `await record_agent_async(...)`; the plain record_agent() is for
# sync call sites (record() hops off a running loop anyway, but the async form awaits the
# write, so a turn that ends right after still leaves its row).
AGENT_KIND = "agent"


def record_agent(message: str, *, area: str = "assistant", thread="", turn="",
                 tool: str = "", actor: str = "", stack: str = "") -> None:
    """One error inside an agentic chat. Never raises."""
    try:
        record(AGENT_KIND, (message or "")[:MAX_MESSAGE], stack=stack,
               source=f"{area}:{tool or 'turn'}",
               page_url=f"chat/{thread or '-'}/{turn or '-'}", actor=actor)
    except Exception:                  # noqa: BLE001
        log.exception("app_errors: could not record an agent failure")


async def record_agent_async(message: str, *, area: str = "assistant", thread="", turn="",
                             tool: str = "", actor: str = "", stack: str = "") -> None:
    """record_agent() from inside a running event loop, awaited on a worker thread."""
    def _write():
        try:
            record_agent(message, area=area, thread=thread, turn=turn, tool=tool,
                         actor=actor, stack=stack)
        finally:
            _close_thread_connection()

    try:
        from asgiref.sync import sync_to_async
        await sync_to_async(_write, thread_sensitive=False)()
    except Exception:                  # noqa: BLE001
        log.exception("app_errors: could not record an agent failure")


def _field(event, name, default=None):
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def is_fault(event) -> tuple:
    """(is_fault, message, tool) for one agent-loop event, duck-typed (object or dict).

    A DESIGNED refusal -- a denied write, a scope guard, a tool asking for a missing input --
    is NOT a fault. A loop-level error event (the model rail, the planner) is, and so is a
    tool observation that raised, timed out, or reported its source unavailable. Events are
    recognised by `type`/class name "error" / "ErrorEvent" and "observation" /
    "ObservationEvent" with ok=False and meta {fault | source_unavailable | denied}."""
    kind = str(_field(event, "type", "") or type(event).__name__).lower()
    if kind in ("error", "errorevent"):
        return True, ("%s: %s" % (_field(event, "code", "") or "error",
                                  _field(event, "message", "") or ""))[:MAX_MESSAGE], ""
    if kind in ("observation", "observationevent") and not _field(event, "ok", True):
        meta = _field(event, "meta", None) or {}
        if meta.get("denied"):
            return False, "", ""
        if meta.get("fault") or meta.get("source_unavailable"):
            tool = str(_field(event, "tool", "") or meta.get("tool") or "")
            body = str(_field(event, "content", "") or "")[:MAX_MESSAGE]
            return True, "%s %s: %s" % (tool, meta.get("fault") or "source unavailable", body), tool
    return False, "", ""


async def agent_faults(events, *, area: str = "assistant", thread="", turn="", actor: str = ""):
    """Pass an agent loop's event stream through untouched; record every fault on the way.

    The stream is the contract with the person watching; nothing here changes what they see
    or when, and a recorder problem is never a second failure inside the first."""
    async for event in events:
        try:
            fault, message, tool = is_fault(event)
            if fault:
                await record_agent_async(message, area=area, thread=thread, turn=turn,
                                         tool=tool, actor=actor)
        except Exception:              # noqa: BLE001
            log.exception("app_errors: agent fault check failed")
        yield event
