import appkit_settings
from django.urls import include, path

urlpatterns = [
    path("auth/", include("app_gate.urls")),
    path("health/", include("app_health.urls")),
    path("", include("budget.urls")),
    *appkit_settings.static_urlpatterns(),
]
