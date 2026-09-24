"""Sign-in, sign-out, and "who am I" — the only views the gate owns."""
from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import auth


@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request):
    nxt = auth.safe_next(request, request.POST.get("next") or request.GET.get("next")) or "/"
    context = {"next": nxt, "app_name": getattr(settings, "APP_NAME", "")}
    if request.method == "GET":
        return render(request, "app_gate/login.html", context)

    username = (request.POST.get("username") or "").strip()
    password = request.POST.get("password") or ""
    try:
        actor = auth.authenticate(username, password)
    except auth.DirectoryUnavailable:
        auth.audit(request, action="sign-in", actor=username, outcome="unavailable")
        context.update(error="The sign-in service could not be reached. Nothing is wrong with "
                             "your password — try again in a minute.", username=username)
        return render(request, "app_gate/login.html", context, status=503)
    except auth.CredentialRejected:
        auth.audit(request, action="sign-in", actor=username, outcome="rejected")
        context.update(error="That username and password were not accepted.", username=username)
        return render(request, "app_gate/login.html", context, status=401)

    if not auth.actor_is_allowed(actor):
        auth.audit(request, action="sign-in", actor=actor.username, outcome="not-on-roster")
        return render(request, "app_gate/denied.html",
                      {**context, "username": actor.username}, status=403)
    auth.save_actor(request, actor)
    auth.audit(request, action="sign-in", actor=actor.username, outcome="allowed",
               detail={"role": auth.actor_role(actor)})
    return redirect(nxt)


@csrf_protect
@require_POST
def logout_view(request):
    actor = auth.actor_from_session(request)
    auth.clear_actor(request)
    auth.audit(request, action="sign-out", actor=actor.username if actor else "",
               outcome="ok")
    return redirect("app_gate:login")


@require_GET
def me(request):
    actor = getattr(request, "gate_actor", None)
    return JsonResponse({
        "gate_required": settings.APP_GATE_REQUIRED,
        "username": actor.username if actor else None,
        "role": getattr(request, "gate_role", None),
        "owner": bool(actor and auth.configured_superadmin(actor)),
    })
