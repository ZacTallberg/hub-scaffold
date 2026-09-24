"""Who is asking, whether they may come in, and at which rung.

THE ORDER OF AUTHORITY, stated once because every reader depends on it:

1. **Owners** (``settings.APP_GATE_SUPERADMINS``) are allowed, and are superadmin, by NAME —
   whatever their roster row says or whether they have one. A roster sync once demoted an
   owner to admin and every owner-only path then refused the person the app belonged to; the
   roster stays the authority for everyone else, but it cannot lock out its owners.
2. **The roster** (``AccessUser`` rows) is the authority the moment it has ANY row. The
   migrations seed the owners into it, so after a first migrate it is never empty.
3. **The allowlist** (``settings.APP_GATE_ALLOWLIST``) is a bootstrap fallback consulted ONLY
   while the roster has no rows at all. It is not a way to add people later — use
   ``manage.py roster``. A wildcard is refused: an emptied roster must never fail open to every
   account the directory can authenticate.

HOW A PASSWORD IS CHECKED is a seam: ``settings.APP_GATE_AUTHENTICATOR`` names a
``module:callable`` taking ``(username, password)`` and returning a :class:`Actor` or raising
:class:`CredentialRejected` / :class:`DirectoryUnavailable`. The kit ships
``app_gate.backends:django_password`` (Django's own user table); a directory bind (LDAP, an
identity provider) is an adapter you supply with the same signature — see the kit README.
"""
from __future__ import annotations

import importlib
import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

logger = logging.getLogger(__name__)

ACTOR_SESSION_KEY = "app_gate.actor.v1"
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_WILDCARD_LOGGED = False


class GateError(Exception):
    pass


class CredentialRejected(GateError):
    """The directory answered, and the answer was no."""


class DirectoryUnavailable(GateError):
    """The directory could not be asked. Never reported to the person as a wrong password."""


@dataclass(frozen=True)
class Actor:
    username: str
    display_name: str = ""
    email: str = ""
    identifiers: frozenset = field(default_factory=frozenset)

    @classmethod
    def of(cls, username: str, display_name: str = "", email: str = "") -> "Actor":
        name = normalize_username(username)
        ids = {name}
        if email:
            ids.add(email.strip().casefold())
        return cls(name, display_name or name, email, frozenset(ids))


def normalize_username(value: str) -> str:
    """Casefolded, domain dropped: ``Alice@Example.com`` and ``alice`` are one person here."""
    raw = (value or "").strip()
    if "\\" in raw:
        raw = raw.rsplit("\\", 1)[1]
    return raw.split("@", 1)[0].casefold()


def owners() -> list[str]:
    """The app's owners, normalized. Must be a list: a raw string would iterate characters."""
    configured = getattr(settings, "APP_GATE_SUPERADMINS", [])
    if isinstance(configured, str):
        # checks.py reports this as app_gate.E001 at boot; never iterate a string here.
        return []
    return sorted({normalize_username(v) for v in configured if str(v).strip()})


def authenticator():
    spec = getattr(settings, "APP_GATE_AUTHENTICATOR", "app_gate.backends:django_password")
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def authenticate(username: str, password: str) -> Actor:
    if not USERNAME_PATTERN.fullmatch((username or "").strip()):
        raise CredentialRejected("username is not in a shape the gate accepts")
    if not password:
        raise CredentialRejected("empty password")
    return authenticator()(username.strip(), password)


def configured_superadmin(actor: Actor) -> bool:
    return bool(set(owners()) & {normalize_username(i) for i in actor.identifiers})


def access_record(actor: Actor):
    from .models import AccessUser
    names = {normalize_username(i) for i in actor.identifiers}
    return AccessUser.objects.filter(username__in=names, active=True).first()


def actor_role(actor: Actor) -> str | None:
    from .models import AccessUser
    if configured_superadmin(actor):
        return AccessUser.ROLE_SUPERADMIN
    record = access_record(actor)
    return record.role if record else None


def actor_is_allowed(actor: Actor) -> bool:
    global _WILDCARD_LOGGED
    from .models import AccessUser
    if configured_superadmin(actor):
        return True
    if AccessUser.objects.exists():
        return access_record(actor) is not None
    allowed = {normalize_username(v) for v in getattr(settings, "APP_GATE_ALLOWLIST", [])}
    if "*" in allowed:
        if not _WILDCARD_LOGGED:
            logger.warning("APP_GATE_ALLOWLIST wildcard is refused — name accounts explicitly")
            _WILDCARD_LOGGED = True
        allowed.discard("*")
    return bool(allowed & {normalize_username(i) for i in actor.identifiers})


def role_at_least(request, minimum: str) -> bool:
    from .models import AccessUser
    have = AccessUser.ROLE_ORDER.get(getattr(request, "gate_role", None) or "", 0)
    return have >= AccessUser.ROLE_ORDER.get(minimum, 99)


def require_role(minimum: str):
    """View decorator: refuse below ``minimum``. The ladder is visitor < member < contributor <
    admin < superadmin. Anonymous requests never reach here while the gate is on (the
    middleware refuses first). While the gate is OFF, rungs up to the ungated baseline stay open
    and everything above fails CLOSED unless APP_GATE_UNGATED_DEV is set — an unarmed gate in
    production must never expose an admin surface. Stack traces and roster edits are ``admin``:
    ``require_role("visitor")`` on them would leave an ungated app publishing its tracebacks."""
    from functools import wraps

    from django.http import HttpResponse, JsonResponse

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            from .models import AccessUser
            need = AccessUser.ROLE_ORDER.get(minimum, 99)
            if not settings.APP_GATE_REQUIRED:
                if need <= AccessUser.ROLE_ORDER[AccessUser.ROLE_UNGATED_BASELINE] \
                        or getattr(settings, "APP_GATE_UNGATED_DEV", False):
                    return view(request, *args, **kwargs)
                return HttpResponse("This surface requires the identity gate "
                                    "(APP_GATE_REQUIRED).", status=403)
            if role_at_least(request, minimum):
                return view(request, *args, **kwargs)
            if wants_json(request):
                return JsonResponse({"error": "insufficient role", "needs": minimum}, status=403)
            return HttpResponse(f"This needs the {minimum} role.", status=403)
        return wrapped
    return decorator


def wants_json(request) -> bool:
    """Decide by what the CALLER asked for, not by where the URL sits. A fetch() poller outside
    /api/ is still a machine caller; handing it an HTML login page makes JSON.parse report
    "Unexpected token '<'" instead of the truth, which is that the session ended."""
    if request.path_info.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With"):
        return True                      # any value: a navigating browser never sends it
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def actor_from_session(request) -> Actor | None:
    data = request.session.get(ACTOR_SESSION_KEY)
    if not isinstance(data, dict) or not data.get("username"):
        return None
    return Actor(data["username"], data.get("display_name", ""), data.get("email", ""),
                 frozenset(data.get("identifiers") or [data["username"]]))


def save_actor(request, actor: Actor) -> None:
    user, created = get_user_model().objects.get_or_create(username=actor.username)
    if created:
        user.set_unusable_password()
        user.save()
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[ACTOR_SESSION_KEY] = {**asdict(actor), "identifiers": sorted(actor.identifiers)}
    request.session.cycle_key()


def clear_actor(request) -> None:
    request.session.pop(ACTOR_SESSION_KEY, None)
    django_logout(request)


def safe_next(request, value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate or not url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return ""
    return candidate


def build_login_url(request) -> str:
    return f"{reverse('app_gate:login')}?{urlencode({'next': request.get_full_path()})}"


def audit(request, *, action: str, actor: str, outcome: str, detail: dict | None = None) -> None:
    """Append one gate event. A failure to record never blocks the sign-in path, but it is
    logged at ERROR so the error-visibility kit carries it to the board."""
    from .models import GateEvent
    payload = dict(detail or {})
    if request is not None:
        payload.setdefault("path", request.path_info[:200])
    try:
        GateEvent.objects.create(actor=(actor or "")[:128], action=action[:48],
                                 outcome=outcome[:24], detail=payload)
    except Exception:                                   # noqa: BLE001
        logger.exception("app_gate could not append a gate event (%s %s)", action, outcome)
