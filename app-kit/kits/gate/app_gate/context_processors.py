from django.conf import settings


def actor(request):
    """The signed-in person and their rung, for every template (the shell's account menu)."""
    return {
        "gate_actor": getattr(request, "gate_actor", None),
        "gate_role": getattr(request, "gate_role", None),
        "gate_required": getattr(settings, "APP_GATE_REQUIRED", True),
    }
