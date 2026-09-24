"""Browser viewer checks: who, in a BROWSER, may see a surface the public board does not show.

The board's reads are unauthenticated, and the general write token never reaches a page. A few
surfaces are neither: they are reads a person should see only after the adopter has decided who
that person is (console chat histories, for example). The scaffold ships no login, so it cannot
decide that itself — it asks a predicate the adopter names in settings:

    HUB_HISTORY_VIEWER = "myproject.auth.is_team_lead"     # a callable (request) -> bool

A predicate that is unset, fails to import, or raises answers NO. The failure mode of an access
check is "not shown", never "shown".

``debug_loopback`` is the one predicate shipped here, for a developer's own machine: it answers
yes only while Django runs with ``DEBUG=True`` AND the request arrives from the loopback address.
Never name it on a deployed hub.
"""
from django.conf import settings
from django.utils.module_loading import import_string

_LOOPBACK = {"127.0.0.1", "::1"}


def debug_loopback(request) -> bool:
    return bool(getattr(settings, "DEBUG", False)) and request.META.get("REMOTE_ADDR") in _LOOPBACK


def allowed(request, setting_name: str) -> bool:
    """True only when the predicate named by ``setting_name`` exists and says yes."""
    path = getattr(settings, setting_name, None)
    if not path:
        return False
    try:
        check = import_string(path) if isinstance(path, str) else path
        return bool(check(request))
    except Exception:                                        # noqa: BLE001 -- fail closed
        return False
