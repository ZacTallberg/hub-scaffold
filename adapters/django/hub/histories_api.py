"""Console chat histories over HTTP — upload from workstations, read by the operator.

The store and its privacy contract live in ``hub_core.histories``; read that first. This module
adds the two gates:

* ``POST /hub/api/history`` — a workstation's batch of NEW console turns. Token-gated with the
  ``history:write`` operation scope, but deliberately NOT through ``@writer``: ``@writer`` refuses
  a whole payload that holds a secret shape, and a transcript containing one must still arrive,
  stored with the secret replaced by ``[REDACTED]``. A scoped credential uploads only as its own
  subject; the shared root credential names the uploading agent in the body.
* ``GET /hub/history.json`` — one console's conversation (``?agent=&session=[&machine=]``), or the
  list of stored consoles when no session is named. Readable by a credential holding
  ``history:read`` (the CLI and MCP path) or by a browser request the adopter's
  ``HUB_HISTORY_VIEWER`` predicate accepts (the board's path). Anyone else gets the plain 404 an
  unknown route gets: a hidden resource does not exist.

Both routes answer 404 while ``HUB_HISTORIES_ENABLED`` is off, which is the default.
"""
import re
import time

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from hub_core import histories

from . import hub_app, viewers
from .hub_write import _authenticate, _body, _record_refusal

_AGENT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


def enabled() -> bool:
    return bool(hub_app._dj_setting("HUB_HISTORIES_ENABLED", False))


def _retention_days() -> int:
    try:
        return max(1, int(hub_app._dj_setting("HUB_HISTORY_RETENTION_DAYS",
                                              histories.DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        return histories.DEFAULT_RETENTION_DAYS


def _not_found():
    return HttpResponse("Not Found", status=404, content_type="text/plain")


def can_view(request) -> bool:
    """May THIS request read histories? A credential with history:read, or the adopter's viewer
    predicate. Never true while the feature is off."""
    if not enabled():
        return False
    if request.headers.get("X-Agent-Token") or request.headers.get("X-Write-Token"):
        auth, _problem = _authenticate(request)
        if auth is not None and auth.allows("history:read"):
            return True
    return viewers.allowed(request, "HUB_HISTORY_VIEWER")


def _slug(value) -> str:
    return str(value or "").strip().lower()


@csrf_exempt
def upload(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not enabled():
        return _not_found()
    if len(request.body or b"") > histories.MAX_BODY_BYTES:
        return JsonResponse({"errors": [{"code": "too_large", "max": histories.MAX_BODY_BYTES}]},
                            status=413)
    auth, problem = _authenticate(request)
    if auth is None:
        _record_refusal(request, "auth_refused", "a history upload was refused: " + str(problem))
        return JsonResponse({"errors": [{"code": "forbidden", "msg": problem}]}, status=403)
    if not auth.allows("history:write"):
        return JsonResponse({"errors": [{"code": "insufficient_scope", "required": "history:write",
                                          "subject": auth.subject}]}, status=403)
    b = _body(request)
    if not isinstance(b, dict):
        return JsonResponse({"errors": [{"code": "bad_json"}]}, status=400)
    # A scoped credential IS the uploader; the shared root credential names it, as every other
    # write does, and the name must still look like a seat label.
    agent = _slug(auth.subject if auth.mode == "scoped-agent" else b.get("agent"))
    if not histories.SLUG.match(agent):
        return JsonResponse({"errors": [{"code": "need_agent",
            "msg": "name the uploading agent (a seat label)"}]}, status=422)
    machine = _slug(request.headers.get("X-Hub-Machine"))
    if machine and not histories.SLUG.match(machine):
        machine = ""
    sessions = b.get("sessions")
    if not isinstance(sessions, list):
        return JsonResponse({"errors": [{"code": "need_sessions"}]}, status=422)
    hub_app.observe_presence(agent, request.headers)
    result = histories.store(hub_app.HUB_DIR, agent, machine, sessions,
                             retention_days=_retention_days())
    return JsonResponse({"data": result})


upload._hub_token_gated = True
upload._hub_required_scope = "history:write"


def history_json(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    if not can_view(request):
        return _not_found()
    agent = _slug(request.GET.get("agent"))
    machine = _slug(request.GET.get("machine"))
    session = (request.GET.get("session") or "").strip()
    if not session:
        if agent and not histories.SLUG.match(agent):
            return JsonResponse({"errors": [{"code": "bad_agent"}]}, status=400)
        rows = histories.consoles(hub_app.HUB_DIR, agent)
        now = time.time()
        for row in rows:
            row["age_s"] = int(max(0, now - row["updated"]))
        resp = JsonResponse({"data": rows[:200], "metadata": {"count": len(rows)}})
        resp["Cache-Control"] = "no-store"
        return resp
    if not histories.SLUG.match(agent) or (machine and not histories.SLUG.match(machine)) \
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,79}$", session):
        return JsonResponse({"errors": [{"code": "need_agent_session",
            "msg": "pass agent and session (machine optional)"}]}, status=400)
    try:
        limit = max(1, min(int(request.GET.get("limit") or histories.DEFAULT_LIMIT), 3000))
    except ValueError:
        limit = histories.DEFAULT_LIMIT
    path = histories.find(hub_app.HUB_DIR, agent, machine, session)
    if path is None:
        return JsonResponse({"data": {"agent": agent, "machine": machine, "session": session,
                                      "turns": [], "turn_count": 0, "served": 0,
                                      "state": "not_uploaded"}})
    # The cheap half first: the tag is the file's size and mtime, so an unchanged console costs
    # a stat and a 304, never a read.
    st = path.stat()
    etag = '"h%x-%x-%d"' % (st.st_size, st.st_mtime_ns, limit)
    if request.headers.get("If-None-Match") == etag:
        resp = HttpResponse(status=304)
    else:
        doc = histories.load(path, limit)
        if doc is None:
            return JsonResponse({"errors": [{"code": "unreadable"}]}, status=503)
        resp = JsonResponse({"data": doc})
    resp["ETag"] = etag
    resp["Cache-Control"] = "no-store"
    return resp
