"""Hub HUMAN VIEW — emits the shared client-rendered app shell (hub_core.shell.render +
hub_core/frontend/). A tabbed single-file app: the kit + snapshot (#hub-data island) are inlined and
hub.js renders the Overview + per-type tables + modals client-side (UI == API; page never scrolls).
Doctrine section 8. No base.html, no heavy bundle."""
from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie

from hub_core import shell

from . import histories_api, hub_app
from .hub_api import _snapshot, _wire_snapshot, hub_json


@ensure_csrf_cookie
def hub(request):
    if request.GET.get("format") == "json":
        return hub_json(request)
    _state, snap = _snapshot(request.GET.get("served"))
    from . import veil
    view = veil.veil_for(request)
    if not view.open:
        snap = view.scrub(snap)        # the inlined island is a payload like any other
    # The page embeds exactly what hub.json serves: large collections as heads, with their exact
    # counts, hydrated by the board from /hub/<type>.json after first paint.
    # Gated surfaces this viewer may open, decided HERE per request — the page never carries a
    # credential, only the fact that the server will answer this person.
    caps = ("history",) if histories_api.can_view(request) else ()
    # The cookie NAME comes from settings -- never a guess -- and is empty when the token is
    # not in a readable cookie, so the board falls back to the rendered token.
    readable = not (getattr(settings, "CSRF_USE_SESSIONS", False)
                    or getattr(settings, "CSRF_COOKIE_HTTPONLY", False))
    cookie = getattr(settings, "CSRF_COOKIE_NAME", "csrftoken") if readable else ""
    return HttpResponse(shell.render(_wire_snapshot(snap), f"{hub_app.BRAND} · Hub",
                                     csrf_token=get_token(request), viewer_caps=caps,
                                     csrf_cookie=cookie))
