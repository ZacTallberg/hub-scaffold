"""Two tiny adopting apps for the example site, so the shared banner can be SEEN working.

``/demo/budget-app/`` and ``/demo/reporting/`` are what an adopting app's page looks like: it
links ``/hub/components/banner/*``, declares its pages once, and marks a few key things with
``data-ab-info``. ``/demo/reporting/`` also shows an app that HAD a header when it adopted the
banner: it links the hub-hosted ``header-takeover`` (the old header named in ``data-replaces``,
duplicate controls and drawer rows folded, ``hide_custom`` read from the hub's component
properties) and ``search-bar`` (answered by ``/demo/<slug>/suggest.json``, the app's own
endpoint). ``/demo/access.json`` is a reference implementation of the People and
Permissions contract the banner speaks (patterns/app-banner.md), with its guards on the SERVER:
nobody demotes, deactivates or removes themselves, and the last admin cannot be removed.

Example code, not the hub: it answers only under DEBUG (404 otherwise), keeps its roster in a
JSON file beside the hub's own sidecars, and names the signed-in person with the hub's own
``viewers.person`` resolver (``HUB_PERSON``). A real app answers these from its own sign-in and
its own roster table.
"""
import html
import json
import threading
import time
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie

from hub import hub_app, viewers

APPS = {
    "budget-app": {"name": "Budget", "sub": "Forecasts and actuals", "version": "2.3.1"},
    "reporting": {"name": "Reporting", "sub": "Monthly packs", "version": "v0.9.0"},
}
ROLES = [
    {"value": "viewer", "label": "Viewer", "what": "Reads every page; changes nothing."},
    {"value": "member", "label": "Member", "what": "Edits forecasts and comments on packs."},
    {"value": "admin", "label": "Admin", "what": "Everything a member can, plus who can sign in."},
    {"value": "super-admin", "label": "Super admin", "what": "Everything, including other admins."},
]
DIRECTORY = [
    {"username": "alice", "display": "Alice Example", "subtitle": "Finance"},
    {"username": "bob", "display": "Bob Example", "subtitle": "Operations"},
    {"username": "carol", "display": "Carol Example", "subtitle": "Finance"},
    {"username": "dave", "display": "Dave Example", "subtitle": "Engineering"},
    {"username": "eve", "display": "Eve Example", "subtitle": "Engineering"},
    {"username": "frank", "display": "Frank Example", "subtitle": "Sales"},
]
_LOCK = threading.Lock()


def _debug_only():
    if not getattr(settings, "DEBUG", False):
        raise Http404("demo pages exist only under DEBUG")


def _roster_path() -> Path:
    return Path(hub_app.HUB_DIR) / "demo-access.json"


def _load() -> dict:
    try:
        return json.loads(_roster_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seed = [("alice", "Alice Example", "super-admin"), ("carol", "Carol Example", "admin"),
                ("bob", "Bob Example", "member")]
        return {"next": 4, "events": [],
                "people": [{"id": i + 1, "username": u, "display": d, "role": r, "active": True,
                            "added_by": "seed"} for i, (u, d, r) in enumerate(seed)]}


def _save(doc: dict) -> None:
    path = _roster_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)


def _role_of(doc: dict, person: str) -> str:
    for p in doc["people"]:
        if p["username"] == person and p.get("active", True):
            return p["role"]
    return ""


def reach(person: str) -> dict:
    """The ``HUB_REACH`` seam for the example: which demo apps this person holds, and how.
    "timesheets" is granted but has no address in HUB_APPS, so the drawer counts it."""
    role = _role_of(_load(), person)
    if not role:
        return {}
    return {"budget-app": role, "reporting": "viewer", "timesheets": "member"}


def _label(role: str) -> str:
    return next((r["label"] for r in ROLES if r["value"] == role), role)


@ensure_csrf_cookie
def page(request, slug="budget-app"):
    _debug_only()
    app = APPS.get(slug)
    if not app:
        raise Http404("no such demo app")
    person = viewers.person(request)
    role = _role_of(_load(), person) if person else ""
    nav_style = "sidebar" if request.GET.get("nav") == "sidebar" else "strip"
    here = request.path
    nav = [
        {"label": "Overview", "href": here, "icon": "home", "current": True,
         "hint": "Where the month stands: spend against forecast, and what changed since last week."},
        {"label": "Forecasts", "href": here + "#forecasts", "icon": "chart", "count": 3, "countId": "fc-count",
         "hint": "Every open forecast, the ones waiting on you first."},
        {"label": "Reports", "icon": "file", "hint": "The packs this app produces.", "children": [
            {"label": "Monthly pack", "href": here + "#monthly", "hint": "The pack that goes to the leadership review."},
            {"label": "Variance", "href": here + "#variance", "hint": "Where actuals left the forecast, and by how much."},
            {"label": "Exports", "href": here + "#exports"}]},
        {"label": "Settings", "href": here + "#settings", "icon": "settings", "sep": True,
         "hint": "This app's own settings: cost centres, fiscal calendar, thresholds."},
    ]
    admin = [{"label": "Cost centres", "href": here + "#cost-centres", "icon": "list",
              "info": "The cost centres this app knows, and who owns each."}]
    profile = [{"label": "My forecasts", "href": here + "#mine", "icon": "user", "sub": "3 open"}]
    legacy = slug == "reporting"
    help_rows = []
    if legacy:
        # What an app that already had its own menus tends to declare: rows the banner also draws.
        admin += [{"label": "People and Permissions", "href": here + "#people", "icon": "users"},
                  {"label": "People and permissions", "href": here + "#people-old", "icon": "users"}]
        help_rows = [{"label": "Guided tour", "id": "legacy-tour"},
                     {"label": "Read me", "href": here + "#readme"}]
    attrs = {
        "data-app": slug, "data-app-name": app["name"], "data-app-sub": app["sub"],
        "data-version": app["version"], "data-home": here, "data-hub": "/hub",
        "data-actor": next((d["display"] for d in DIRECTORY if d["username"] == person), person) if person else "",
        "data-actor-sub": person, "data-role": _label(role), "data-nav-style": nav_style,
        "data-access-url": "/demo/access.json" if slug == "budget-app" else "",
        "data-signout": "/demo/signout/",
    }
    if legacy:
        attrs["data-replaces"] = ".legacy-head"
        attrs["data-props"] = "/hub/components/props/%s.json" % slug
    mount_attrs = " ".join('%s="%s"' % (k, html.escape(str(v), quote=True)) for k, v in attrs.items() if v)

    def island(name, value):
        return '<script type="application/json" data-ab="%s">%s</script>' % (
            name, json.dumps(value).replace("<", "\\u003c"))

    help_island = island("help", help_rows) if help_rows else ""
    if legacy:
        search = (
            '<div id="app-search" data-action="%s" data-app="%s" data-app-name="%s" data-value="%s" '
            'data-suggest-url="/demo/%s/suggest.json" data-sources="packs, variances, exports">'
            '<form data-search-fallback action="%s" method="get"><input type="search" name="q" '
            'aria-label="Search"></form></div>'
            '<script src="/hub/components/search-bar/search-bar.js"></script>'
            % (here, slug, html.escape(app["name"], quote=True),
               html.escape(request.GET.get("q", ""), quote=True), slug, here))
        slot = (search + '<button class="app-btn" type="button">New pack</button>'
                '<button class="app-btn" type="button">Your apps</button>'
                '<button class="app-btn" type="button" id="legacy-export">Export</button>')
        legacy_head = ('<header class="legacy-head" style="display:flex;gap:10px;align-items:center;'
                       'padding:8px 20px;border-bottom:1px solid rgba(127,127,127,.3)">'
                       '<b>Reporting (old header)</b><a href="#settings">Settings</a>'
                       '<button class="app-btn" type="button">Refresh packs</button>'
                       '<a href="/demo/signout/">Sign out</a></header>')
        search_css = '<link rel="stylesheet" href="/hub/components/search-bar/search-bar.css">'
        takeover_js = '<script src="/hub/components/header-takeover/header-takeover.js" defer></script>'
    else:
        slot = ('<button class="app-btn" type="button" data-ab-info="Start a new forecast for next '
                'month from this month&#39;s actuals." data-ab-info-title="New forecast">New forecast</button>')
        legacy_head = search_css = takeover_js = ""

    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(app['name'])} - example</title>
<link rel="stylesheet" href="/hub/components/banner/banner.css">
{search_css}
<style>
  body {{ margin: 0; font: 15px/1.5 system-ui, sans-serif; background: #f6f7f9; color: #1d2230; }}
  html[data-theme="dark"] body {{ background: #111521; color: #e3e7ef; }}
  @media (prefers-color-scheme: dark) {{ html:not([data-theme="light"]) body {{ background: #111521; color: #e3e7ef; }} }}
  main {{ max-width: 980px; margin: 0 auto; padding: 24px 20px 80px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-top: 16px; }}
  .card {{ padding: 16px; border-radius: 12px; background: rgba(127,127,127,.08); border: 1px solid rgba(127,127,127,.2); }}
  .card b {{ display: block; font-size: 26px; }}
  .app-btn {{ font: inherit; padding: 5px 10px; border-radius: 8px; border: 1px solid rgba(127,127,127,.35); background: transparent; color: inherit; cursor: pointer; }}
</style></head>
<body data-csrf="{html.escape(get_token(request), quote=True)}">
<header id="app-banner" {mount_attrs}>
  {island("nav", nav)}{island("admin", admin)}{island("profile", profile)}{help_island}
  <div data-ab-slot>{slot}</div>
</header>
{legacy_head}
<main>
  <h1 style="margin:0">Spend is 4% under forecast this month</h1>
  <p style="margin:4px 0 0;opacity:.75">An example page for the shared banner. Signed in as <b>{html.escape(person or 'nobody')}</b>.</p>
  <div class="cards">
    <div class="card" data-ab-info="Actual spend so far this month, against the forecast for the same days." data-ab-info-title="Spend to date"><b>41,200 of 43,000</b>spend to date</div>
    <div class="card" data-ab-info="Forecasts that still need an owner's sign-off before the monthly pack." data-ab-info-title="Awaiting sign-off"><b>3 of 12</b>forecasts awaiting sign-off</div>
    <div class="card" data-ab-info="Lines where actuals moved more than the variance threshold." data-ab-info-title="Variances"><b>2</b>variances over threshold</div>
  </div>
</main>
<script src="/hub/components/banner/banner.js" defer></script>
{takeover_js}
</body></html>"""
    return HttpResponse(body)


#: What the demo apps can be searched for: the app's own index, which a real app would query.
_SEARCHABLE = {
    "reporting": [
        ("Packs", "Monthly pack", "September, awaiting sign-off", "#monthly"),
        ("Packs", "Quarterly pack", "Q3, published", "#quarterly"),
        ("Variances", "Travel variance", "12% over forecast", "#variance-travel"),
        ("Variances", "Software variance", "4% under forecast", "#variance-software"),
        ("Exports", "Export to spreadsheet", "every pack, one sheet per section", "#exports"),
    ],
}


def suggest(request, slug):
    """The search-bar suggest contract (hub_core/components/search-bar/manifest.json), answered
    from the demo app's own index. An app with no index says `unconfigured`, never "no results"."""
    _debug_only()
    rows = _SEARCHABLE.get(slug)
    if rows is None:
        return JsonResponse({"ok": False, "reason": "unconfigured",
                             "error": "this demo app has no search index"})
    q = (request.GET.get("q") or "").strip().lower()
    groups: dict = {}
    for group, title, meta, anchor in rows:
        if q and q not in (title + " " + meta + " " + group).lower():
            continue
        groups.setdefault(group, []).append({"title": title, "meta": meta,
                                             "url": "/demo/%s/%s" % (slug, anchor),
                                             "ref": "%s:%s" % (slug, anchor.lstrip("#"))})
    return JsonResponse({"ok": True, "total": sum(len(v) for v in groups.values()),
                         "groups": [{"key": k.lower(), "title": k, "rows": v} for k, v in groups.items()],
                         "popular": ["monthly pack", "travel variance"],
                         "placeholder": "Search packs, variances and exports", "smart": False})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@csrf_protect
def access_json(request):
    """The data-access-url contract, reference implementation. Admins only."""
    _debug_only()
    person = viewers.person(request)
    with _LOCK:
        doc = _load()
        mine = _role_of(doc, person) if person else ""
        if mine not in ("admin", "super-admin"):
            return JsonResponse({"reason": "Only admins manage who can sign in here."}, status=403)
        if request.method == "GET":
            q = (request.GET.get("q") or "").strip().lower()
            if q:
                known = {p["username"] for p in doc["people"]}
                hits = [dict(d, known=d["username"] in known) for d in DIRECTORY
                        if q in d["username"] or q in d["display"].lower()]
                return JsonResponse({"people": hits})
            people = [dict(p, self=p["username"] == person) for p in doc["people"]]
            return JsonResponse({"roles": ROLES, "default_role": "member", "people": people,
                                 "events": list(reversed(doc["events"]))[:20]})
        if request.method != "POST":
            return JsonResponse({"reason": "GET or POST"}, status=405)
        try:
            b = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"reason": "the body is not JSON"}, status=400)
        action, valid = b.get("action"), {r["value"] for r in ROLES}

        def event(act, target):
            doc["events"].append({"when": _now(), "actor": person, "action": act, "target": target})

        def admins_left(excluding):
            return sum(1 for p in doc["people"] if p is not excluding and p.get("active", True)
                       and p["role"] in ("admin", "super-admin"))

        if action == "add":
            role = b.get("role")
            if role not in valid:
                return JsonResponse({"reason": "unknown role"}, status=400)
            if role == "super-admin" and mine != "super-admin":
                return JsonResponse({"reason": "Only a super admin can make a super admin."}, status=403)
            out = {"added": [], "updated": [], "unchanged": [], "refused": []}
            for who in b.get("people") or []:
                name = str((who or {}).get("username") or "").strip().lower()
                if not name or len(name) > 64 or " " in name:
                    out["refused"].append("%r is not a username" % name)
                    continue
                cur = next((p for p in doc["people"] if p["username"] == name), None)
                if cur is None:
                    doc["people"].append({"id": doc["next"], "username": name,
                                          "display": str(who.get("display") or name)[:80], "role": role,
                                          "active": True, "added_by": person})
                    doc["next"] += 1
                    out["added"].append(name)
                    event("added", "%s as %s" % (name, _label(role)))
                elif cur["username"] == person:
                    out["refused"].append("%s is you -- change your own role through another admin" % name)
                elif cur["role"] == role and cur.get("active", True):
                    out["unchanged"].append(name)
                else:
                    cur["role"], cur["active"] = role, True
                    out["updated"].append(name)
                    event("changed", "%s to %s" % (name, _label(role)))
            _save(doc)
            return JsonResponse({"ok": True, "outcomes": out})
        target = next((p for p in doc["people"] if p["id"] == b.get("id")), None)
        if target is None:
            return JsonResponse({"reason": "no such person on this roster"}, status=404)
        if target["username"] == person:
            return JsonResponse({"reason": "You cannot change your own access; ask another admin."}, status=409)
        if target["role"] == "super-admin" and mine != "super-admin":
            return JsonResponse({"reason": "Only a super admin can change a super admin."}, status=403)
        if action == "set":
            if b.get("role") not in valid:
                return JsonResponse({"reason": "unknown role"}, status=400)
            if target["role"] in ("admin", "super-admin") and b["role"] not in ("admin", "super-admin") \
                    and admins_left(target) == 0:
                return JsonResponse({"reason": "That would leave nobody who can manage access."}, status=409)
            target["role"] = b["role"]
            event("changed", "%s to %s" % (target["username"], _label(b["role"])))
            detail = "%s is now %s." % (target["display"], _label(b["role"]))
        elif action == "active":
            want = bool(b.get("active"))
            if not want and admins_left(target) == 0 and target["role"] in ("admin", "super-admin"):
                return JsonResponse({"reason": "That would leave nobody who can manage access."}, status=409)
            target["active"] = want
            event("reactivated" if want else "deactivated", target["username"])
            detail = "%s is %s." % (target["display"], "active again" if want else "deactivated")
        elif action == "delete":
            if target["role"] in ("admin", "super-admin") and admins_left(target) == 0:
                return JsonResponse({"reason": "That would leave nobody who can manage access."}, status=409)
            doc["people"].remove(target)
            event("removed", target["username"])
            detail = "%s was removed." % target["display"]
        else:
            return JsonResponse({"reason": "unknown action"}, status=400)
        _save(doc)
        return JsonResponse({"ok": True, "detail": detail})


@csrf_protect
def signout(request):
    _debug_only()
    return HttpResponse("The example has no sign-in; a real app ends the session here (POST only).",
                        status=200 if request.method == "POST" else 405, content_type="text/plain")
