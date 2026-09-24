"""URLconf for the hub example site: the app root plus the hub mounted at /hub/ (never at the
front door).

`/.well-known/agent-card.json` is mounted at the ROOT for conventional agent discovery. It is a
read-only, signed description of what this Hub can do and how to authenticate to its real MCP
endpoint; it advertises no A2A task transport, and the token VALUE never appears in it.
"""
from django.http import JsonResponse
from django.urls import include, path

from hub.agent_card import agent_card_view

from . import demo, demo_app


def index(request):
    return JsonResponse({"app": "example", "hub": "/hub/", "demo_app": "/demo-app/",
                         "banner_demo": "/demo/budget-app/"})


urlpatterns = [
    path("", index),
    path(".well-known/agent-card.json", agent_card_view),
    path("hub/", include("hub.urls")),
    # A tiny adopting app (demo_app.py): links the hub-hosted agent component and bridges its
    # calls server-side with the app's hub credential.
    path("demo-app/", demo_app.page),
    path("demo-app/agent/ask", demo_app.agent_ask),
    path("demo-app/agent/history", demo_app.agent_history),
    path("demo-app/agent/conversation", demo_app.agent_conversation),
    path("demo-app/profile.json", demo_app.profile),
    path("demo-app/feed.json", demo_app.feed),
    # Two example adopting apps wearing the shared banner (DEBUG only; see demo.py).
    path("demo/access.json", demo.access_json),
    path("demo/signout/", demo.signout),
    path("demo/<slug:slug>/", demo.page),
]
