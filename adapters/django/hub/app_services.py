"""The hub's services to the APPS around it: hosted UI components and their per-app
properties, each app's slice of the board, a person's cross-app preferences, and the brokered
agent. Engines live in hub_core (components, app_feed, profiles, agent_broker); this module is
only the HTTP edge.

TRUST, route by route:

* /hub/components/…              OPEN reads. Presentation only (CSS/JS/manifests and the
                                  properties an app's pages are about to draw for anyone who can
                                  open them) -- no board record, no identity.
* /hub/app-feed.json              an ordinary board READ, under the same boundary as every other
                                  /hub read (public unless the adopter puts reads behind auth).
* /hub/api/component-props        WRITE, scope component:configure. Changes what EVERY person on
                                  one app sees, so it is an operator credential, not a browser.
* /hub/api/profile                GET scope profile:read, POST scope profile:write. Per-person
                                  state, asked for by an app's server on behalf of a person it has
                                  signed in; the hub authenticates the app, the app vouches for
                                  the person.
* /hub/api/agent/{ask,history,conversation}
                                  scopes agent:ask / agent:history. The hub holds the ONE agent
                                  key; an app's bridge holds only its hub credential.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse

from hub_core import agent_broker, app_feed, components, profiles

from . import hub_app
from .hub_write import writer


def _no_store(response):
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- hosted components

def component_index(request):
    """GET /hub/components/ -- every hosted component, its measured version, files, how to link
    it, and the apps observed loading it."""
    rows = components.index(components.adopters(hub_app.HUB_DIR))
    base = request.build_absolute_uri(request.path)
    for row in rows:
        row["base"] = base + row["name"] + "/"
    response = JsonResponse({"data": rows, "metadata": {
        "count": len(rows),
        "how_to_use": ("Link the files; never copy them. The copy served here is the master, so "
                       "a change reaches every app that links it on its next page load. Each "
                       "app's settings for these components are at props/<slug>.json beside "
                       "them, and are changed with POST /hub/api/component-props."),
        "props": "props/<slug>.json",
    }})
    response["Cache-Control"] = "public, max-age=60"
    return response


def component_file(request, name, filename):
    """GET /hub/components/<name>/<file> -- one file of one component; anything that is not a
    served file on disk is a 404."""
    found = components.read_file(name, filename)
    if found is None:
        raise Http404("no such component file")
    body, content_type, etag, version = found
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
    else:
        response = HttpResponse(body, content_type=content_type)
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=%d" % components.CACHE_SECONDS
    response["X-Component-Version"] = version
    response["X-Content-Type-Options"] = "nosniff"
    return response


def component_props(request, slug):
    """GET /hub/components/props/<slug>.json[?component=<name>] -- one app's component
    properties with the schema beside them. `component` names the caller, so adoption is
    OBSERVED from the page that actually linked it."""
    slug = (slug or "").strip().lower()
    if not components.SLUG_RE.fullmatch(slug):
        return JsonResponse({"ok": False, "reason": "not an app slug"}, status=400)
    caller = (request.GET.get("component") or "").strip().lower()
    if caller:
        components.note_adopter(hub_app.HUB_DIR, caller, slug)
    record = components.get_record(hub_app.HUB_DIR, slug)
    response = JsonResponse({"ok": True, "app": slug, "props": record["props"],
                             "schema": components.schema(),
                             "updated_at": record["updated_at"]})
    # Revalidated on every page load: a change must reach the app's next page.
    response["Cache-Control"] = "no-cache"
    return response


@writer(scope="component:configure")
def set_component_props(request, b):
    """POST /hub/api/component-props {app, props: {component: {key: value}}} -- REPLACE one
    app's component properties (a key left out returns to its default). Refused keys are
    reported, never silently dropped."""
    slug = str(b.get("app") or "").strip().lower()
    if not components.SLUG_RE.fullmatch(slug):
        return JsonResponse({"errors": [{"code": "need_app",
            "msg": "name the app by its slug ([a-z0-9-], starting with a letter or digit)"}]},
            status=400)
    if not isinstance(b.get("props"), dict):
        return JsonResponse({"errors": [{"code": "need_props",
            "msg": "send props as {component: {key: value}}; GET /hub/components/props/<slug>.json "
                   "shows the schema"}]}, status=400)
    actor = request.hub_auth.subject if request.hub_auth.mode == "scoped-agent" else \
        str(b.get("agent") or request.hub_auth.subject)
    record, refused = components.set_props(hub_app.HUB_DIR, slug, b["props"], actor)
    return JsonResponse({"data": {"app": slug, "props": record["props"],
                                  "updated_at": record["updated_at"],
                                  "updated_by": record["updated_by"],
                                  "history": record["history"][-5:], "refused": refused}})


# ---------------------------------------------------------------- one app's slice

def app_feed_json(request):
    """GET /hub/app-feed.json?app=<slug>[&name=<display name>] -- one app's checklist,
    announcements and (empty by design) what's-new, each row naming the field that matched."""
    slug = (request.GET.get("app") or "").strip().lower()
    if not app_feed.SLUG_RE.fullmatch(slug):
        return JsonResponse({"errors": [{"code": "need_app", "msg": "name the app: ?app=<slug>"}]},
                            status=400)
    state = hub_app.current_state()
    return _no_store(JsonResponse(app_feed.build(state, slug, request.GET.get("name") or "")))


# ---------------------------------------------------------------- a person's preferences

def _person_or_400(request):
    who = (request.GET.get("person") or "").strip().lower()
    if not profiles.valid_person(who):
        return None, JsonResponse({"errors": [{"code": "need_person",
            "msg": "name the person as the app's own username for them: ?person=<name>"}]},
            status=400)
    return who, None


@writer(scope={"GET": "profile:read", "POST": "profile:write"}, methods=("GET", "POST"),
        presence=False)
def profile(request, b):
    """GET /hub/api/profile?person=<name> (profile:read) -- that person's preferences.
    POST /hub/api/profile?person=<name> {"prefs": {...}} (profile:write) -- merge them."""
    who, refused = _person_or_400(request)
    if refused:
        return refused
    if request.method == "GET":
        return _no_store(JsonResponse({"data": {"person": who,
                                                "prefs": profiles.get(hub_app.HUB_DIR, who)}}))
    prefs, ignored = profiles.update(hub_app.HUB_DIR, who, b.get("prefs"))
    # What was IGNORED is reported, so a caller sending a key this hub does not know finds out
    # instead of wondering why it never sticks.
    return _no_store(JsonResponse({"data": {"person": who, "prefs": prefs, "ignored": ignored}}))


# ---------------------------------------------------------------- the brokered agent

def _setting(name, default=""):
    value = getattr(settings, name, None)
    if value in (None, ""):
        value = os.environ.get(name, default)
    return value


def _lane():
    return agent_broker.Lane(
        url=_setting("HUB_AGENT_URL"), key=_setting("HUB_AGENT_KEY"),
        context=_setting("HUB_AGENT_CONTEXT"), label=_setting("HUB_AGENT_LABEL", "Agent"),
        timeout_s=_setting("HUB_AGENT_TIMEOUT_S", "90"),
        tls_verify=_setting("HUB_AGENT_TLS_VERIFY", "true"))


@writer(scope="agent:ask", presence=False)
def agent_ask(request, b):
    """POST /hub/api/agent/ask {question, thread?, person?, conversation_id?, app?}."""
    status, body = agent_broker.ask(_lane(), b)
    return _no_store(JsonResponse(body, status=status))


@writer(scope="agent:history", methods=("GET",), presence=False)
def agent_history(request, b):
    """GET /hub/api/agent/history?person=&app=&scope=app|all."""
    status, body = agent_broker.history(_lane(), request.GET.get("person"),
                                        request.GET.get("app"), request.GET.get("scope") or "")
    return _no_store(JsonResponse(body, status=status))


@writer(scope="agent:history", methods=("GET",), presence=False)
def agent_conversation(request, b):
    """GET /hub/api/agent/conversation?person=&id=."""
    status, body = agent_broker.conversation(_lane(), request.GET.get("person"),
                                             request.GET.get("id"))
    return _no_store(JsonResponse(body, status=status))


# Presentation for other apps' pages (CSS/JS/manifests and the properties they draw): declared
# public, exactly as the route table says, so the read gate never sits between a page and the
# files it links. Nothing here is a board record or an identity.
for _view in (component_index, component_file, component_props):
    _view._hub_public_discovery = True
