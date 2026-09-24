"""The read boundary: every Hub read is authenticated unless the operator declares it public.

The board projects everything on the ledger — who is working on what, every question, every
operational error — and "unauthenticated reads" turned out to be the posture a new mount ended
up with by accident rather than by decision. So the default is REQUIRED, and a public board is
something an operator writes down (``HUB_READ_AUTH = "public"``), where the computed audit and
the deploy verification can both see it.

A read is admitted when the request carries any authenticated principal the Hub already
understands, checked in this order:

1. a Django-authenticated user (the host site's own sign-in, when the auth middleware is
   installed) — this is how a person's browser reads the board and its live stream;
2. a valid scoped agent credential (``X-Agent-Token``) — any live credential may read; the
   write scopes still gate every mutation separately;
3. the shared-root token (``X-Write-Token``), while shared-root compatibility is enabled.

A refused read answers 401 with a JSON body (or, for the HTML board when the host has a sign-in
page — ``HUB_LOGIN_URL``, or ``LOGIN_URL`` with Django's auth app installed — a redirect to it
with ``next`` set), and it is recorded in the operational
stream so a worker whose credential expired shows up as a refusal rather than a quiet seat.
"""
import hmac
import os
from functools import wraps
from urllib.parse import quote

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse

from hub_core import agent_auth

from . import hub_app

PUBLIC = "public"
REQUIRED = "required"


def mode() -> str:
    """The configured read posture. Anything other than an explicit ``public`` is required:
    a typo must never open the board."""
    raw = hub_app._dj_setting("HUB_READ_AUTH", None)
    if raw is None:
        raw = os.environ.get("HUB_READ_AUTH")
    return PUBLIC if str(raw or "").strip().lower() == PUBLIC else REQUIRED


def principal(request):
    """``(kind, subject)`` of the authenticated reader, or ``(None, reason)``."""
    user = getattr(request, "user", None)
    try:
        if user is not None and user.is_authenticated:
            return "session", str(user.get_username() if hasattr(user, "get_username") else user)
    except Exception:                                        # noqa: BLE001 - an auth backend
        pass                                                 # fault falls through to tokens
    agent_token = request.headers.get("X-Agent-Token") or ""
    if agent_token:
        try:
            auth = agent_auth.CredentialRegistry(hub_app.HUB_DIR).authenticate(agent_token)
            return "agent", auth.subject
        except agent_auth.CredentialError as exc:
            return None, "agent credential refused: %s" % exc
    want = hub_app._dj_setting("HUB_WRITE_TOKEN") or os.environ.get("HUB_WRITE_TOKEN")
    got = request.headers.get("X-Write-Token") or ""
    compat = hub_app._dj_setting("HUB_SHARED_TOKEN_COMPAT", None)
    if compat is None:
        compat = os.environ.get("HUB_SHARED_TOKEN_COMPAT")
    compat_on = compat is None or str(compat).strip().lower() not in ("0", "false", "no", "off")
    if compat_on and want and got and hmac.compare_digest(got, want):
        return "root", agent_auth.shared_root_context().subject
    return None, "no authenticated principal on a read (HUB_READ_AUTH=required)"


def _login_url():
    """Where a person's browser signs in: ``HUB_LOGIN_URL``, else the host's ``LOGIN_URL`` —
    but only when the host actually installs Django's auth app. Django ships a LOGIN_URL
    default, and redirecting to a sign-in page nobody mounted is a 404 dressed as a gate."""
    explicit = hub_app._dj_setting("HUB_LOGIN_URL", None)
    if explicit:
        return explicit
    try:
        from django.apps import apps
        if apps.is_installed("django.contrib.auth"):
            return hub_app._dj_setting("LOGIN_URL", None)
    except Exception:                                        # noqa: BLE001
        pass
    return None


def _refuse(request, reason):
    try:
        hub_app.record_error(
            "hub.auth", "a read was refused: " + reason, severity="warning",
            code="read_auth_refused",
            context={"component": "hub-read", "path": request.path_info,
                     "method": request.method,
                     "agent": (request.headers.get("X-Hub-Agent") or "")[:120]})
    except Exception:                                        # noqa: BLE001 - telemetry never
        pass                                                 # turns a 401 into a 500
    wants_html = "text/html" in (request.headers.get("Accept") or "")
    login_url = _login_url()
    if wants_html and login_url and request.method == "GET":
        return HttpResponseRedirect("%s?next=%s" % (login_url, quote(request.get_full_path(), safe="/")))
    if wants_html:
        response = HttpResponse(
            "<!doctype html><title>Sign-in required</title><p>This board requires an "
            "authenticated reader.</p>", status=401, content_type="text/html; charset=utf-8")
    else:
        response = JsonResponse({"errors": [{"code": "read_auth_required", "msg": reason}]},
                                status=401)
    response["WWW-Authenticate"] = 'Hub realm="hub"'
    response["Cache-Control"] = "no-store"
    return response


def reader(view):
    """Gate one read view on the configured read posture. Marked for the route audit."""
    @wraps(view)
    def gated(request, *args, **kwargs):
        if mode() == REQUIRED:
            kind, subject = principal(request)
            if not kind:
                return _refuse(request, subject)
            request.hub_reader = (kind, subject)
        return view(request, *args, **kwargs)
    gated._hub_read_gated = True
    return gated
