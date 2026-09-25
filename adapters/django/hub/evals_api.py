"""The standing-eval trend over HTTP (hub_core.evals).

    POST /hub/api/eval   (eval:write)  record one run of one suite
    GET  /hub/eval.json?suite=&limit=  the trend, newest last; ETag/304 on all three carriers

A measurement is not a ledger fact: runs live in HUB_DIR/evals.jsonl beside the ledger, so the
fold every read pays for never carries them.
"""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from hub_core import evals
from hub_core.process_lock import LockBusy

from . import hub_app
from .hub_api import _conditional
from .hub_write import writer

MAX_BODY_BYTES = 64 * 1024


@writer(scope="eval:write")
def post(request, b):
    """One scored run from any credential holding `eval:write`. The author is the
    authenticated writer (a scoped credential's subject), never a field the caller chose."""
    if len(request.body or b"") > MAX_BODY_BYTES:
        return JsonResponse({"errors": [{"code": "too_large", "max": MAX_BODY_BYTES}]}, status=413)
    by = str(b.get("agent") or getattr(request.hub_auth, "subject", "") or "")
    try:
        row = evals.append(hub_app.HUB_DIR, b, by=by,
                           machine=request.headers.get("X-Hub-Machine") or "")
    except evals.EvalRefused as refused:
        return JsonResponse({"errors": [{"code": refused.code, "msg": str(refused)}]}, status=422)
    except LockBusy as busy:
        return JsonResponse({"errors": [{"code": "busy", "msg": busy.public}]}, status=503)
    return JsonResponse({"data": row}, status=201)


@require_GET
def eval_json(request):
    """GET /hub/eval.json?suite=<name>&limit=N -> {data: [runs, newest last], metadata}."""
    try:
        limit = int(request.GET.get("limit") or 60)
    except ValueError:
        limit = 60
    rows, meta = evals.trend(hub_app.HUB_DIR, request.GET.get("suite") or "", limit)
    return _conditional(request, '"%s"' % meta["etag"], lambda: {"data": rows, "metadata": meta})
