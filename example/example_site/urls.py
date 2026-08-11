"""URLconf for the hub example site: the app root plus the hub mounted at /hub/ (never at the
front door).

`/.well-known/agent-card.json` is mounted at the ROOT because the A2A specification fixes that
path — a discovery document served anywhere else is one no standard client will look for. It is a
read-only, signed description of what this hub can do and how to authenticate to it; the token
VALUE never appears in it.
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
