"""Include with ``path("errors/", include("app_errors.urls"))``."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.errors_page, name="errors_page"),
    path("report/", views.report, name="errors_report"),
    path("json/", views.errors_json, name="errors_json"),
    path("<int:pk>/resolve/", views.resolve, name="errors_resolve"),
]
