"""The request-path gate: every path is refused unless it is signed in or named as public.

Deny by DEFAULT, so a view added next year without a decorator fails closed. The only paths
that pass unsigned are the gate's own sign-in and sign-out, the health probes a deploy needs,
static assets, and whatever the app names ONE AT A TIME in ``APP_GATE_PUBLIC_PATHS`` (exact
paths) or ``APP_GATE_PUBLIC_PREFIXES`` (explicit prefixes). "This landing page is public" is
spelled there — never as ``APP_GATE_REQUIRED=false``, which would publish every surface,
the error views included, as a side effect.

A refused caller gets what it asked for: a machine caller (JSON Accept, X-Requested-With, an
/api/ path) gets ``401`` JSON naming the login URL; an htmx request gets ``401`` with
``HX-Redirect``; a navigating browser gets a redirect to the sign-in page carrying ``next``.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .auth import (actor_from_session, actor_is_allowed, actor_role, build_login_url,
                   clear_actor, wants_json)

GATE_OPEN_VIEWS = {("app_gate", "login"), ("app_gate", "logout")}
DEFAULT_HEALTH = ("/health/live/", "/health/ready/")


def _is_exempt(request) -> bool:
    path = request.path_info
    # path_info never carries the deployment prefix, so static assets are always /static/ here.
    if path.startswith("/static/") or path in getattr(settings, "APP_GATE_HEALTH_PATHS", DEFAULT_HEALTH):
        return True
    if path in tuple(getattr(settings, "APP_GATE_PUBLIC_PATHS", ())):
        return True
    prefixes = tuple(p for p in getattr(settings, "APP_GATE_PUBLIC_PREFIXES", ()) if p and p != "/")
    if prefixes and path.startswith(prefixes):
        return True
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return (match.namespace, match.url_name) in GATE_OPEN_VIEWS


def _harden(response):
    # A proxy may strip X-Frame-Options; ship the protection in a header proxies leave alone.
    response.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return response


class GateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.gate_actor = None
        request.gate_role = None
        if not settings.APP_GATE_REQUIRED:
            return _harden(self.get_response(request))
        actor = actor_from_session(request)
        if actor is not None and actor_is_allowed(actor):
            request.gate_actor = actor
            request.gate_role = actor_role(actor)
            return _harden(self.get_response(request))
        if _is_exempt(request):
            return _harden(self.get_response(request))
        if actor is not None:
            clear_actor(request)            # a revoked row takes effect on the next request

        login_url = build_login_url(request)
        if wants_json(request):
            response = JsonResponse({"error": "authentication required",
                                     "login_url": login_url}, status=401)
            response["X-Login-Redirect"] = login_url
            return _harden(response)
        if request.headers.get("HX-Request"):
            response = HttpResponse("Authentication required", status=401)
            response["HX-Redirect"] = login_url
            return _harden(response)
        return _harden(redirect(login_url))
