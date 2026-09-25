"""The browser's report endpoint, and -- the half apps forget -- a place to READ the rows.

Capturing failures into a table nobody opens is the same as not capturing them. `/errors/` is
a surface built to the bar (the heading states a finding, counts carry their denominator, the
stack is in the row, an empty table is a positive statement); `/errors/json/` is the same data
for an agent with HTTP but no shell.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import capture

# What the BROWSER may claim. `server`, `django`, `background` and `agent` are SERVER-ATTESTED:
# this endpoint is csrf-exempt by design, so a browser claiming `server` would put a
# fabricated server exception on the hub under this app's name.
VALID_KINDS = {"js", "promise", "http", "stream", "other", "data"}
MAX_REPORT_BYTES = 64 * 1024


@csrf_exempt          # a report ABOUT a broken page must not need a live token
@require_POST
def report(request):
    """Browser-side failure report. Always 204: a telemetry endpoint that can fail loudly
    gives the page a second thing to go wrong about."""
    if len(request.body or b"") > MAX_REPORT_BYTES:
        return HttpResponse(status=204)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)
    if not isinstance(payload, dict):
        return HttpResponse(status=204)
    kind = payload.get("kind")
    kind = kind if kind in VALID_KINDS else "js"
    message = str(payload.get("message") or "")[:capture.MAX_MESSAGE]
    if not message.strip():
        return HttpResponse(status=204)
    status = payload.get("status")
    status = int(status) if isinstance(status, int) or (
        isinstance(status, str) and status.isdigit()) else None
    if kind == "http" and status is not None and 100 <= status < 400:
        # An HTTP answer below 400 is the request WORKING, never a failure: a 304 is a
        # conditional poll finding nothing new, a 3xx is a redirect the page asked for.
        # report_errors.js no longer sends these, but a tab opened before the app picked up
        # that fix keeps running the old wrapper until it is reloaded. The floor belongs
        # server-side too, so no browser build can put a 3xx on the board.
        return HttpResponse(status=204)
    capture.record(
        kind, message,
        stack=capture.fit_stack(payload.get("stack")),
        source=str(payload.get("source") or "")[:500],
        page_url=str(payload.get("page_url") or request.META.get("HTTP_REFERER", ""))[:500],
        status=status,
        actor=capture._actor(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    return HttpResponse(status=204)


def _rows(request):
    qs = capture._model().objects.all()
    show = request.GET.get("show", "open")
    if show == "open":
        qs = qs.filter(resolved_at__isnull=True)
    elif show == "resolved":
        qs = qs.filter(resolved_at__isnull=False)
    kind = request.GET.get("kind")
    if kind:
        qs = qs.filter(kind=kind)
    return qs


def _context(request):
    Model = capture._model()
    rows = list(_rows(request)[:200])
    totals = Model.objects.aggregate(
        all=Count("id"),
        open=Count("id", filter=Q(resolved_at__isnull=True)),
        server=Count("id", filter=Q(kind__in=["server", "background"],
                                    resolved_at__isnull=True)),
        occurrences=Sum("count"),
    )
    since = Model.objects.order_by("first_seen").values_list("first_seen", flat=True).first()
    return {
        "rows": rows, "totals": totals, "since": since,
        "show": request.GET.get("show", "open"),
        "kind": request.GET.get("kind", ""),
        "kinds": Model.objects.values("kind").annotate(n=Count("id")).order_by("-n"),
        "shown": len(rows),
        "forwarding": capture.forwarding_status(),
        "recorder": capture.recorder_faults(),
        "base_template": getattr(settings, "APP_ERRORS_BASE_TEMPLATE", "app_errors/base.html"),
    }


@require_GET
def errors_page(request):
    """`/errors/` -- what is broken right now, most recent first."""
    ctx = _context(request)
    if request.headers.get("HX-Request"):
        return render(request, "app_errors/_errors_table.html", ctx)
    return render(request, "app_errors/errors.html", ctx)


@require_POST
def resolve(request, pk: int):
    """Close a row. A recurrence re-opens it, so this is a claim the app can falsify."""
    row = capture._model().objects.filter(pk=pk).first()
    if row is None:
        return JsonResponse({"error": "not found"}, status=404)
    row.resolved_at = timezone.now()
    row.resolved_by = (capture._actor(request) or "operator")[:150]
    row.save(update_fields=["resolved_at", "resolved_by"])
    if request.headers.get("HX-Request"):
        return render(request, "app_errors/_errors_table.html", _context(request))
    return redirect("errors_page")


@require_GET
def errors_json(request):
    """Machine-readable, for an agent that has HTTP but no shell on the host. Carries the
    forwarder's delivery status and the recorder's own fault count: an empty list from a
    dark forwarder or a broken recorder must not read as a healthy app."""
    rows = _rows(request)[:200]
    return JsonResponse({
        "errors": [
            {"id": r.pk, "kind": r.kind, "severity": r.severity, "message": r.message,
             "source": r.source, "page_url": r.page_url, "status": r.status,
             "count": r.count, "first_seen": r.first_seen.isoformat(),
             "last_seen": r.last_seen.isoformat(), "open": r.is_open, "stack": r.stack}
            for r in rows],
        "forwarding": capture.forwarding_status(),
        "recorder": capture.recorder_faults(),
    }, json_dumps_params={"indent": 2})
