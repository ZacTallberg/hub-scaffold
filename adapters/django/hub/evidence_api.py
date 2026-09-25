"""The shared evidence store's two routes (engine: hub_core.evidence).

    POST /hub/api/evidence   (evidence:write)  one reading: subject, kind, summary, body?, repo?, commit?
    GET  /hub/evidence.json  ?q=&repo=&commit=&limit=   members only (a reading can name internal
                                                         hosts and systems a narrowed reader must not see)

A reading goes through the same write seam as every board write -- credential, scope, the
secret-shape refusal at the door -- but lands in HUB_DIR/evidence.jsonl, never the ledger.
"""
from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from hub_core import evidence as core

from . import hub_app
from .hub_write import writer


@writer(scope="evidence:write")
def post(request, b):
    if len(request.body or b"") > core.MAX_REQUEST_BYTES:
        return JsonResponse({"errors": [{"code": "too_large",
                                         "max_body_chars": core.MAX_BODY_CHARS}]}, status=413)
    try:
        row = core.put(hub_app.HUB_DIR, b, by=str(b.get("agent") or request.hub_auth.subject or ""),
                       machine=request.headers.get("X-Hub-Machine") or "",
                       session=request.headers.get("X-Hub-Session") or "")
    except core.Refused as refusal:
        return JsonResponse(refusal.body(), status=refusal.status)
    except TimeoutError:
        return JsonResponse({"errors": [{"code": "busy", "msg": "the evidence store is busy; retry"}]},
                            status=503)
    return JsonResponse({"data": row}, status=201)


@require_GET
def evidence_json(request):
    """What the team already measured: best match first for `q`, else newest first."""
    try:
        limit = max(1, min(int(request.GET.get("limit") or 20), 200))
    except ValueError:
        limit = 20
    q = (request.GET.get("q") or "")[:300]
    repo = (request.GET.get("repo") or "")[:200]
    commit = (request.GET.get("commit") or "")[:40]
    tag = core.etag(hub_app.HUB_DIR, q, repo, commit, limit)
    sent = (request.headers.get("If-None-Match") or request.headers.get("X-Hub-ETag")
            or request.GET.get("etag") or "").strip().replace("W/", "").strip('"')
    if sent and sent == tag.strip('"'):
        resp = HttpResponse(status=304)
    else:
        rows, stored = core.find(hub_app.HUB_DIR, q, repo, commit, limit)
        resp = JsonResponse({"data": rows, "metadata": {
            "count": len(rows), "stored": stored, "keep": core.KEEP, "q": q,
            "repo": core.clean_repo(repo), "commit": commit}})
    resp["ETag"] = tag
    resp["X-Hub-ETag"] = tag
    return resp
