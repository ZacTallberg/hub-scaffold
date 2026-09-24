"""Render the CSRF cookie NAME the page must read -- from settings, never re-derived.

    TEMPLATES[0]["OPTIONS"]["context_processors"] += ["app_csrf.context_processors.csrf_cookie"]

    <body data-csrf-cookie="{{ csrf_cookie_name }}" data-csrf="{{ csrf_token }}">

Why the name is READ here: a page that BUILDS the cookie name (from the app slug, the package
name, a wildcard) is right only until its inputs disagree. Rename the app, or run several apps
on one host where every app's cookies arrive together, and a built name points at a cookie that
does not exist -- or at a sibling app's token. Django always defines CSRF_COOKIE_NAME (default
"csrftoken"), so reading it is correct in every deployment.

An empty name means the page cannot read the token from a cookie at all (the token lives in the
session, or the cookie is HttpOnly); csrf.js then falls back to the rendered token.
"""
from django.conf import settings


def csrf_cookie(request):
    readable = not (getattr(settings, "CSRF_USE_SESSIONS", False)
                    or getattr(settings, "CSRF_COOKIE_HTTPONLY", False))
    return {"csrf_cookie_name": settings.CSRF_COOKIE_NAME if readable else ""}
