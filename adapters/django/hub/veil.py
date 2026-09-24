"""The visibility veil, wired into the Django adapter: who the reader is, which routes it may
see, and the scrub every veiled JSON response passes through. The engine is hub_core.veil.

Every /hub route DECLARES its visibility (urls.py wraps each view):

    open     served to every tier unchanged (writes, whose credential is the boundary)
    veiled   served to every tier; for a narrowed reader the payload passes through the veil
    member   a narrowed reader gets 404 — exactly as an unknown route does (omission)

A route with no declaration does not serve a narrowed reader at all, and the hub audit flags it
(``routes:undeclared-visibility``) — a new route is private until someone decides otherwise.

Who the reader is (``request.hub_tier``), resolved once by VeilMiddleware:

    X-Agent-Token          the credential's subject, looked up in tiers.json
    X-Write-Token (root)   operator
    a signed-in site user  HUB_VEIL_USER_TIER (default member) — the host's own auth boundary
    anyone else            HUB_VEIL_ANONYMOUS_TIER (default contributor: fail closed)

An agent with no tiers.json entry is a contributor WHEN FACETS ARE DECLARED. With no
PROJECT/facets.json the veil is open for everybody and none of this changes a byte.
"""
from __future__ import annotations

import json

from django.http import Http404

from hub_core import veil as core

from . import hub_app

VISIBILITIES = ("open", "veiled", "member")


def registry_path():
    return hub_app.PROJECT / "facets.json"


def active() -> bool:
    """True when the adopter declared facets (or the registry is broken)."""
    doc = core.registry(registry_path())
    return bool(doc.get("facets") or doc.get("broken"))


def tier_of(agent) -> str:
    """The tier of a named agent: tiers.json when set; otherwise contributor while facets are
    declared (fail closed), and "" — no veil in force — when none are."""
    agent = str(agent or "").strip().lower()
    explicit = core.read_tiers(hub_app.HUB_DIR).get(agent)
    if explicit:
        return explicit
    return core.DEFAULT_TIER if active() else ""


def Veil(tier) -> core.Veil:                                  # noqa: N802 - factory mirrors the class
    return core.Veil(tier or "member", core.registry(registry_path()))


def resolve_tier(request) -> str:
    """The reader's tier, from the identity the request carries. Never from a header that
    NAMES a tier: a tier is a fact about a credential, not a claim a caller makes."""
    if not active():
        return "member"
    token = request.headers.get("X-Agent-Token") or ""
    if token:
        try:
            from hub_core import agent_auth
            auth = agent_auth.CredentialRegistry(hub_app.HUB_DIR).authenticate(token)
            return tier_of(auth.subject)
        except Exception:                                    # noqa: BLE001 - a bad token is no one
            return core.DEFAULT_TIER
    root = hub_app._dj_setting("HUB_WRITE_TOKEN") or ""
    supplied = request.headers.get("X-Write-Token") or ""
    if root and supplied:
        import hmac
        if hmac.compare_digest(root, supplied):
            return "operator"
        return core.DEFAULT_TIER
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(hub_app._dj_setting("HUB_VEIL_USER_TIER", "member") or "member").lower()
    return str(hub_app._dj_setting("HUB_VEIL_ANONYMOUS_TIER", core.DEFAULT_TIER)
               or core.DEFAULT_TIER).lower()


def veil_for(request) -> core.Veil:
    tier = getattr(request, "hub_tier", None)
    if tier is None:
        tier = resolve_tier(request)
        request.hub_tier = tier
    return Veil(tier)


# ── route declarations ─────────────────────────────────────────────────────────────────────

def declare(visibility, view):
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be one of %s" % (VISIBILITIES,))
    view._hub_visibility = visibility
    return view


def open_route(view):
    return declare("open", view)


def veiled(view):
    return declare("veiled", view)


def member_only(view):
    return declare("member", view)


def scrub_response(veil, response):
    """Pass one JSON response through the veil (omission; structure kept). A body the veil
    cannot parse is refused rather than served unfiltered — fail closed."""
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        raise Http404()
    response.content = json.dumps(veil.scrub(payload), ensure_ascii=False).encode("utf-8")
    response["Content-Length"] = str(len(response.content))
    response["Vary"] = "X-Agent-Token, X-Write-Token, Cookie"
    if response.has_header("ETag"):
        del response["ETag"]
    return response


class VeilMiddleware:
    """Resolve the reader's tier once, refuse routes it may not see (404 — omission, never a
    403 that confirms the route exists), and scrub every veiled JSON response. A no-op for any
    reader whose veil is open, which is every reader when no facets are declared."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        from asgiref.sync import iscoroutinefunction, markcoroutinefunction
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

    def process_view(self, request, view_func, view_args, view_kwargs):
        if "/hub" not in (request.path_info or ""):
            return None
        veil = veil_for(request)
        if veil.open:
            return None
        visibility = getattr(view_func, "_hub_visibility", None)
        name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
        if visibility not in ("open", "veiled") or not veil.route_visible(name):
            raise Http404()
        if visibility == "veiled":
            request._hub_scrub = veil
        return None

    def _finish(self, request, response):
        veil = getattr(request, "_hub_scrub", None)
        if veil is None or getattr(response, "streaming", False):
            return response
        if not str(response.get("Content-Type", "")).startswith("application/json"):
            return response
        return scrub_response(veil, response)
