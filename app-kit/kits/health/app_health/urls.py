from django.urls import path

from . import views

app_name = "app_health"

urlpatterns = [
    path("live/", views.live, name="live"),
    path("ready/", views.ready, name="ready"),
]
