"""URLconf for the hub example site: the app root plus the hub mounted at /hub/ (never at the
front door).

`/.well-known/agent-card.json` is mounted at the ROOT for conventional agent discovery. It is a
read-only, signed description of what this Hub can do and how to authenticate to its real MCP
endpoint; it advertises no A2A task transport, and the token VALUE never appears in it.
"""
from django.http import JsonResponse
from django.urls import include, path

from hub.agent_card import agent_card_view


def index(request):
    return JsonResponse({"app": "example", "hub": "/hub/"})


urlpatterns = [
    path("", index),
    path(".well-known/agent-card.json", agent_card_view),
    path("hub/", include("hub.urls")),
]
