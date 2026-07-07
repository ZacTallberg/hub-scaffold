"""The agent-operable hub at /hub — public-safe reads, token-gated writes (X-Write-Token).
Rendered entirely by hub_core (shell.render); no Django templates. NEVER mount at the front door."""
from django.urls import path

from . import hub_api, hub_write, hubsite

app_name = "hub"
urlpatterns = [
    path("", hubsite.hub, name="hub"),
    path("hub.json", hub_api.hub_json),
    path("audit.json", hub_api.audit_json),
    path("graph.json", hub_api.graph_json),
    path("next.json", hub_api.next_json),
    path("schema/<str:type>.schema.json", hub_api.schema_json),
    path("<str:type>.json", hub_api.type_json),
    path("<str:type>/<str:local>.json", hub_api.entity_json),
    path("api/task", hub_write.task),
    path("api/complete", hub_write.complete),
    path("api/adr", hub_write.adr),
    path("api/capability", hub_write.capability),
    path("api/decision", hub_write.decision),
    path("api/claim", hub_write.claim),
    path("api/heartbeat", hub_write.heartbeat),
]
