from django.urls import path

from . import views

app_name = "budget"

urlpatterns = [
    path("", views.home, name="home"),
    path("lines/<str:code>/", views.line_detail, name="line"),
    path("rows/", views.rows, name="rows"),
    path("archive-over/", views.archive_over, name="archive_over"),
    path("lines/<str:code>/reopen/", views.reopen, name="reopen"),
    path("method/", views.method, name="method"),
    path("palette.json", views.palette, name="palette"),
    path("events/", views.events, name="events"),
]
