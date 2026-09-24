"""A tiny ADOPTING APP beside the hub: how an app uses the hub's app services.

It is deliberately the smallest honest shape of the pattern, not a product:

* its page LINKS the hub-hosted agent component (never copies it);
* its server-side BRIDGE forwards the component's calls to the hub with the APP's hub
  credential -- the browser holds nothing, and the agent key stays at the hub;
* it shows its own slice of the board (/hub/app-feed.json) and the signed-in person's
  cross-app preferences (/hub/api/profile).

THIS DEMO SIGNS IN NOBODY. It names one fixed demo person (DEMO_PERSON) where a real app names
the person its own sign-in resolved. A real app also puts these pages behind that sign-in and
gives its bridge a SCOPED credential (agent:ask, agent:history, profile:read) via
HUB_APP_AGENT_TOKEN instead of the shared root token this example falls back to.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from django.http import HttpResponse, JsonResponse

SLUG = "budget-app"
DEMO_PERSON = os.environ.get("HUB_DEMO_PERSON", "alice")


def _hub(request, path, *, method="GET", body=None, params=None):
    """Call the hub over HTTP, exactly as a separately deployed app would."""
    base = os.environ.get("HUB_BASE_URL") or request.build_absolute_uri("/hub")
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Accept": "application/json"}
    agent_token = os.environ.get("HUB_APP_AGENT_TOKEN", "")
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    else:
        headers["X-Write-Token"] = os.environ.get("HUB_WRITE_TOKEN", "")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"ok": False, "reason": "failed", "error": "hub answered HTTP %d" % exc.code}
    except (urllib.error.URLError, TimeoutError) as exc:
        return 502, {"ok": False, "reason": "failed", "error": "the hub could not be reached: %s" % exc}


def _json(status_body):
    status, body = status_body
    response = JsonResponse(body, status=status if status >= 400 else 200, safe=False)
    response["Cache-Control"] = "no-store"
    return response


def agent_ask(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False, "reason": "failed", "error": "the body is not JSON"}, status=400)
    body = body if isinstance(body, dict) else {}
    body.update(app=SLUG, person=DEMO_PERSON)        # the app vouches for the person
    return _json(_hub(request, "/api/agent/ask", method="POST", body=body))


def agent_history(request):
    return _json(_hub(request, "/api/agent/history", params={
        "person": DEMO_PERSON, "app": SLUG, "scope": request.GET.get("scope") or "app"}))


def agent_conversation(request):
    return _json(_hub(request, "/api/agent/conversation", params={
        "person": DEMO_PERSON, "id": request.GET.get("id") or ""}))


def profile(request):
    return _json(_hub(request, "/api/profile", params={"person": DEMO_PERSON}))


def feed(request):
    return _json(_hub(request, "/app-feed.json", params={"app": SLUG, "name": "Budget app"}))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Budget app</title>
<link rel="stylesheet" href="/hub/components/agent/agent.css">
<script src="/hub/components/agent/agent.js" defer></script>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --mute:#5d6677; --line:#d9dde5; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#10141b; --card:#161b24; --ink:#e7ebf2; --mute:#9aa4b5; --line:#2d3542; } }
  :root[data-theme="dark"] { --bg:#10141b; --card:#161b24; --ink:#e7ebf2; --mute:#9aa4b5; --line:#2d3542; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 20px; background:var(--card); border-bottom:1px solid var(--line); }
  header .slot { display:flex; align-items:center; gap:10px; }
  .mark { width:32px; height:32px; border-radius:50%; display:grid; place-items:center; background:var(--line); font-weight:700; font-size:13px; overflow:hidden; }
  .mark img { width:100%; height:100%; object-fit:cover; }
  main { max-width:960px; margin:0 auto; padding:20px 16px; display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); }
  section { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  h1 { font-size:17px; margin:0; } h2 { font-size:14px; margin:0 0 8px; }
  ul { margin:0; padding-left:18px; } li { margin:4px 0; } .meta { color:var(--mute); font-size:12.5px; display:block; }
  .empty { color:var(--mute); }
</style></head>
<body>
<header><h1>Budget app</h1><div class="slot"><span data-hub-agent-header></span><span class="mark" id="mark" title="">?</span></div></header>
<main>
  <section><h2>Being built for this app</h2><div id="checklist" class="empty">Loading&hellip;</div></section>
  <section><h2>Built because you asked</h2><div id="announcements" class="empty">Loading&hellip;</div></section>
</main>
<div data-hub-agent data-app="budget-app" data-title="Budget assistant"
     data-ask="/demo-app/agent/ask" data-history="/demo-app/agent/history"
     data-conversation="/demo-app/agent/conversation" data-profile="/demo-app/profile.json"
     data-props="/hub/components/props/budget-app.json"></div>
<script>
(function () {
  function list(id, rows, empty, matched) {
    var box = document.getElementById(id); box.textContent = "";
    if (!rows.length) { box.textContent = empty; return; }
    if (matched > rows.length) {
      var head = document.createElement("span"); head.className = "meta";
      head.textContent = "Showing " + rows.length + " of " + matched; box.appendChild(head);
    }
    box.className = ""; var ul = document.createElement("ul");
    rows.forEach(function (r) {
      var li = document.createElement("li"); li.textContent = r.title;
      var m = document.createElement("span"); m.className = "meta"; m.textContent = r.meta || r.date || "";
      li.appendChild(m); ul.appendChild(li);
    });
    box.appendChild(ul);
  }
  fetch("/demo-app/feed.json").then(function (r) { return r.json(); }).then(function (f) {
    var m = (f.metadata || {}).matched || {};
    list("checklist", f.checklist || [], "Nothing on the board names this app.", m.checklist || 0);
    list("announcements", f.announcements || [], "Nothing has been built on request for this app yet.", m.announcements || 0);
  }).catch(function (e) { document.getElementById("checklist").textContent = "Could not load the feed: " + e; });
  fetch("/demo-app/profile.json").then(function (r) { return r.json(); }).then(function (b) {
    var d = b.data || {}, p = d.prefs || {}, mark = document.getElementById("mark");
    mark.title = d.person || "";
    if (p.kind === "image" && p.image) { var img = document.createElement("img"); img.alt = ""; img.src = p.image; mark.textContent = ""; mark.appendChild(img); }
    else mark.textContent = String(d.person || "?").slice(0, 2).toUpperCase();
    if (p.theme) document.documentElement.setAttribute("data-theme", p.theme);
  }).catch(function () {});
})();
</script>
</body></html>"""


def page(request):
    return HttpResponse(PAGE, content_type="text/html; charset=utf-8")
