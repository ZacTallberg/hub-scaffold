"""URLconf for the hub example site: the app root plus the hub mounted at /hub/ (never at the
front door)."""
from django.http import JsonResponse
from django.urls import include, path


def index(request):
    return JsonResponse({"app": "example", "hub": "/hub/"})


urlpatterns = [
    path("", index),
    path("hub/", include("hub.urls")),
]
