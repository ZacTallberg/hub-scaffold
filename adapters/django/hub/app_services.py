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
* /hub/api/profile                Per-person state, named exactly two ways. (1) An app's SERVER
                                  with its credential (GET profile:read, POST profile:write) and
                                  ?person= for someone it has signed in: the hub authenticates the
                                  app, the app vouches for the person. (2) The person's OWN
                                  same-origin browser, when the adopter names a HUB_PERSON
                                  resolver: the person is whoever the resolver says, a ?person=
                                  naming anyone else answers 404, and a POST must pass Django's
                                  CSRF check. Everyone else gets the plain 404 of an unknown route.
* /hub/api/agent/{ask,history,conversation}
                                  scopes agent:ask / agent:history. The hub holds the ONE agent
                                  key; an app's bridge holds only its hub credential.
"""
from __future__ import annotations

import os

from django.conf import settings
import json

from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.utils.module_loading import import_string
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from hub_core import agent_broker, app_feed, components, profiles, reach

from . import hub_app, viewers
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

#: The largest body the browser path reads: a mark at its cap plus room for every other key.
_PROFILE_MAX_BODY = profiles.MARK_MAX_CHARS + 16_384


def _person_or_400(request):
    who = (request.GET.get("person") or "").strip().lower()
    if not profiles.valid_person(who):
        return None, JsonResponse({"errors": [{"code": "need_person",
            "msg": "name the person as the app's own username for them: ?person=<name>"}]},
            status=400)
    return who, None


def _grants(person: str) -> dict:
    """{slug: role} from the adopter's HUB_REACH seam. Unset or failing: no grants -- the
    failure mode of an access question is "nothing listed", never "everything listed"."""
    path = getattr(settings, "HUB_REACH", None)
    if not path:
        return {}
    try:
        resolve = import_string(path) if isinstance(path, str) else path
        out = resolve(person)
    except Exception:                                        # noqa: BLE001 -- fail closed
        return {}
    return out if isinstance(out, dict) else {}


def _profile_answer(request, who, b):
    """The one answer both paths give. GET adds what the person can reach (the adopter's
    HUB_APPS directory joined with its HUB_REACH grants); a write does not re-read grants, so
    starring an app never costs an access lookup. ?app=<slug> adds `resolved`: the
    everywhere-choices with that app's override laid on top."""
    if request.method == "GET":
        prefs = profiles.get(hub_app.HUB_DIR, who)
        data = {"person": who, "prefs": prefs,
                **reach.apps_for(_grants(who), getattr(settings, "HUB_APPS", None))}
    else:
        prefs, ignored = profiles.update(hub_app.HUB_DIR, who, b.get("prefs"))
        # What was IGNORED is reported, so a caller sending a key this hub does not know finds
        # out instead of wondering why it never sticks.
        data = {"person": who, "prefs": prefs, "ignored": ignored}
    slug = (request.GET.get("app") or "").strip().lower()
    if slug and profiles.SLUG_RE.fullmatch(slug):
        data["app"] = slug
        data["resolved"] = profiles.resolve(prefs, slug)
    return _no_store(JsonResponse({"data": data}))


@writer(scope={"GET": "profile:read", "POST": "profile:write"}, methods=("GET", "POST"),
        presence=False)
def _profile_for_app(request, b):
    who, refused = _person_or_400(request)
    if refused:
        return refused
    return _profile_answer(request, who, b)


@csrf_protect
def _profile_for_browser(request, who):
    b = {}
    if request.method == "POST":
        if len(request.body or b"") > _PROFILE_MAX_BODY:
            return JsonResponse({"errors": [{"code": "too_large", "max": _PROFILE_MAX_BODY}]},
                                status=413)
        try:
            b = json.loads((request.body or b"").decode("utf-8") or "{}")
        except ValueError:
            b = None
        if not isinstance(b, dict):
            return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
    return _profile_answer(request, who, b)


@csrf_exempt            # the browser half re-applies CSRF itself; the app half is token-gated
def profile(request):
    """GET /hub/api/profile[?person=<name>][&app=<slug>] -- a person's preferences, the apps
    they can reach, and (with app) what that app should look like for them.
    POST /hub/api/profile[?person=<name>] {"prefs": {...}} -- merge a change (per key; per app
    under prefs.apps, where {} removes an app and null resets one key)."""
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.headers.get("X-Agent-Token") or request.headers.get("X-Write-Token"):
        return _profile_for_app(request)
    who = viewers.person(request)
    asked = (request.GET.get("person") or "").strip().lower()
    if not profiles.valid_person(who) or (asked and asked != who):
        # A request the hub cannot name is nobody; a browser naming somebody else is refused
        # as if the route were not there.
        return HttpResponse("Not Found", status=404, content_type="text/plain")
    return _profile_for_browser(request, who)


profile._hub_token_gated = True
profile._hub_origin_gated = True           # the browser half is @csrf_protect
profile._hub_required_scope = {"GET": "profile:read", "POST": "profile:write"}


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
