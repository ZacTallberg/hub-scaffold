"""The shell's declared capabilities, read from ``settings.APP_SHELL``.

    APP_SHELL = {
        "toasts": True,                        # transient confirmations
        "palette": {"url_name": "app:palette"},  # Ctrl/Cmd-K over an index THIS app serves
        "live": {"url_name": "app:events"},     # SSE: status in words + new-build offer
        "drawer": True,                        # right-hand record detail
        "confirm": True,                       # data-confirm="<the consequence>"
        "transitions": True,                   # view transitions on htmx swaps
        "prefetch": True,                      # warm drill targets on INTENT only
    }

Everything not named is INERT: no markup rendered, no listener bound, no fetch issued. An app
with no realtime ships no EventSource; an app with nothing to jump to ships no palette. A shell
that carries every capability as always-on code decays into furniture nobody can justify; a
shell whose capabilities are declared is one an audit can read.
"""
from __future__ import annotations

from django.conf import settings

KNOWN = ("toasts", "palette", "live", "drawer", "confirm", "transitions", "prefetch")


def declared() -> dict:
    """The declared capabilities. ``{"url_name": "app:view"}`` is reversed here, so an endpoint
    follows the deployment prefix (FORCE_SCRIPT_NAME) instead of a hard-coded path that is right
    on a laptop and wrong behind the proxy."""
    from django.urls import NoReverseMatch, reverse

    raw = getattr(settings, "APP_SHELL", {}) or {}
    out = {}
    for key in KNOWN:
        value = raw.get(key)
        if not value:
            continue
        if isinstance(value, dict) and value.get("url_name") and not value.get("url"):
            try:
                value = {**value, "url": reverse(value["url_name"])}
            except NoReverseMatch:
                continue            # app_shell.W002 names it at boot; never ship a dead URL
        out[key] = value
    return out


def shell(request):
    return {
        "shell": declared(),
        "app_name": getattr(settings, "APP_NAME", ""),
        "build_id": getattr(settings, "BUILD_ID", ""),
    }
