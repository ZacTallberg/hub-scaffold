"""The example's pages. Each one is a kit behaviour made concrete: a heading that states a
finding, counts with denominators, absence rendered as absence, a poll that answers 204 when
nothing changed, a reviewed bulk action that refuses when its scope moved, and an SSE stream
that ends when the client leaves."""
from __future__ import annotations

import hashlib
import json
import logging
import time

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from app_gate.auth import require_role

from .models import BudgetLine, LineEvent

log = logging.getLogger("budget")


def _actor(request) -> str:
    actor = getattr(request, "gate_actor", None)
    return actor.username if actor else "local-dev"


def _open_lines():
    return BudgetLine.objects.exclude(status="archived").order_by("-status", "code")


def _fingerprint() -> str:
    """The STATE a poll would re-render, hashed: row count, per-status counts, last change."""
    agg = BudgetLine.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    by = list(BudgetLine.objects.values_list("status").annotate(n=Count("pk")).order_by("status"))
    raw = json.dumps([agg["n"], str(agg["last"]), by], default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _summary() -> dict:
    agg = BudgetLine.objects.exclude(status="archived").aggregate(
        total=Count("pk"), over=Count("pk", filter=Q(status="over")),
        measured=Count("pk", filter=Q(actual__isnull=False)), planned=Sum("planned"))
    return {**agg, "archived": BudgetLine.objects.filter(status="archived").count(),
            "sample": LineEvent.objects.filter(action="sample-load").exists(),
            "as_of": timezone.now()}


@require_GET
def home(request):
    return render(request, "budget/home.html",
                  {"lines": _open_lines(), "s": _summary(), "fp": _fingerprint(),
                   "archived_now": request.GET.get("archived"),
                   "archived": BudgetLine.objects.filter(status="archived").order_by("code")})


@require_GET
def rows(request):
    """The poll target. 204 when the state is unchanged: an honest poll does not re-render
    identical markup every few seconds."""
    fp = _fingerprint()
    if request.GET.get("fp") == fp:
        return HttpResponse(status=204)
    response = render(request, "budget/_rows.html", {"lines": _open_lines(), "s": _summary()})
    response["X-FP"] = fp
    return response


@require_GET
def line_detail(request, code):
    line = get_object_or_404(BudgetLine, code=code)
    events = LineEvent.objects.filter(target=line.code)[:10]
    template = "budget/_line.html" if request.headers.get("HX-Request") else "budget/line.html"
    return render(request, template, {"line": line, "events": events})


@require_POST
@require_role("admin")
def archive_over(request):
    """Archive every over-budget line -- reviewed, then applied. The form carries the count the
    person saw; if it moved between the look and the click the apply refuses and records the
    refusal, because approving "3 lines" must never archive 40."""
    try:
        expected = int(request.POST.get("expected_count", ""))
    except ValueError:
        expected = -1
    with transaction.atomic():
        target = BudgetLine.objects.select_for_update().filter(status="over")
        found = target.count()
        if found != expected:
            LineEvent.objects.create(actor=_actor(request), action="archive-refused",
                                     target="*", detail={"expected": expected, "found": found})
            return render(request, "budget/refused.html",
                          {"expected": expected, "found": found}, status=409)
        codes = list(target.values_list("code", flat=True))
        target.update(status="archived", updated_at=timezone.now())
        for code in codes:
            LineEvent.objects.create(actor=_actor(request), action="archived", target=code,
                                     detail={"reason": "over budget", "revertable": True})
    log.info("archived %d over-budget line(s)", len(codes))
    return redirect(reverse("budget:home") + f"?archived={len(codes)}")


@require_POST
@require_role("contributor")
def reopen(request, code):
    """Undo an archive: the line returns to the open list, and the reopen is recorded."""
    line = get_object_or_404(BudgetLine, code=code, status="archived")
    line.status = "over" if (line.variance or 0) > 0 else "open"
    line.save(update_fields=["status", "updated_at"])
    LineEvent.objects.create(actor=_actor(request), action="reopened", target=line.code,
                             detail={"status": line.status})
    return redirect("budget:home")


@require_GET
def method(request):
    return render(request, "budget/method.html", {"s": _summary()})


@require_GET
def palette(request):
    lines = BudgetLine.objects.exclude(status="archived").order_by("code")[:200]
    return JsonResponse({"groups": [
        {"label": "Pages", "items": [
            {"label": "All budget lines", "url": reverse("budget:home")},
            {"label": "How each number is counted", "url": reverse("budget:method")}]},
        {"label": "Budget lines", "items": [
            {"label": f"{line.code} {line.title}", "sub": line.team,
             "url": reverse("budget:line", args=[line.code])} for line in lines]},
    ]})


@require_GET
def events(request):
    """SSE: the running build (so an open tab can offer a reload), then a heartbeat or a
    refresh when the state changes. Bounded to a minute -- the client reconnects -- so an
    abandoned tab never holds a server thread; GeneratorExit is the client leaving."""
    def stream():
        try:
            yield f"event: build\ndata: {settings.BUILD_ID}\n\n"
            last = _fingerprint()
            for _ in range(12):
                time.sleep(5)
                now = _fingerprint()
                if now != last:
                    last = now
                    yield f"event: refresh\ndata: {json.dumps({'fp': now})}\n\n"
                else:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            return
    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
