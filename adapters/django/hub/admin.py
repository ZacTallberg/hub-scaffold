"""The hub app has no relational models to administer: all operational state is event-sourced in
PROJECT/.hub/ and surfaced at /hub. This module only brands the admin when contrib.admin is
installed in the host project."""
from django.contrib import admin

from . import hub_app

admin.site.site_header = hub_app.BRAND
admin.site.site_title = f"{hub_app.BRAND} admin"
admin.site.index_title = "Command center"
