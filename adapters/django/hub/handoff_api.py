"""The publish hand-off lane's routes (hub_core.handoff holds the rules; read it first).

* ``POST /hub/api/handoff`` (``handoff:submit``) -- the author's upload: a git bundle of the
  commits its machine could not push. Token-gated but deliberately NOT through ``@writer``:
  ``@writer`` reads ``request.body``, which Django refuses past ``DATA_UPLOAD_MAX_MEMORY_SIZE``
  (2.5 MB by default), and a bundle may be up to 20 MB. The same credential, scope, agent
  binding and secret-shape refusal (over everything but the bundle) are applied here, and the
  body is read from the stream under its own ceiling.
* ``POST /hub/api/handoff/claim`` / ``bundle`` / ``result`` (``handoff:publish``) -- a
  publisher's fenced lease, the download under it, and the outcome.
* ``GET /hub/handoffs.json`` -- the queue, open oldest first.

A terminal result puts a checkpoint on the task through the ordinary validated append (a
``pushed`` row with the sha, which the deploy close reads) and messages the author's machine.
Both are fail-soft: the result is recorded already, and a notice that could not be written must
never turn a landed publish into a reported failure.

``HUB_HANDOFF_GIT_HOSTS`` (a list, or a comma-separated string) names the git hosts a hand-off
may point at; unset refuses every hand-off.
"""
from __future__ import annotations

import base64
import json
import logging

from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from hub_core import handoff as _handoff
from hub_core import ids, secretscan

from . import hub_app
from .held import _project_key
from .hub_write import (_append, _authenticate, _record_refusal, _valid_agent_name, board_link,
                        writer)

log = logging.getLogger(__name__)

SUBMIT_SCOPE = "handoff:submit"
PUBLISH_SCOPE = "handoff:publish"


def allowed_hosts() -> tuple:
    raw = hub_app._dj_setting("HUB_HANDOFF_GIT_HOSTS", None)
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(str(h).strip().lower() for h in (raw or ()) if str(h).strip())


def _full_task(task) -> str:
    task = str(task or "").strip()
    if task and ":" not in task:
        try:
            return ids.make_id(hub_app.PROJECT_KEY, "task", task)
        except ids.InvalidId:
            return task
    return task


def _task_exists(task_id: str) -> bool:
    ent = (hub_app.current_state().get("entities") or {}).get(task_id)
    return bool(ent) and ent.get("type") == "task"


def _resolver(project: str, sha: str):
    """True / False / None from the Hub's commit resolver; its "could not ask" stays None."""
    try:
        resolver = hub_app.commit_resolver()
        verdict, _searched = resolver.fetchable(sha, _project_key(resolver, project))
        return verdict
    except Exception:                                        # noqa: BLE001 - unknown is not "no"
        return None


def _refuse(code: str, msg: str, status: int = 422):
    return JsonResponse({"errors": [{"code": code, "msg": msg}]}, status=status)


@csrf_exempt
def submit(request):
    """POST /hub/api/handoff -- {task, project, remote, branch?, base, head, bundle_b64,
    commits?, subject?, note?, idem_key?}. Idempotent on idem_key; the same head already
    waiting answers that record (``duplicate``)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    auth, problem = _authenticate(request)
    if auth is None:
        _record_refusal(request, "auth_refused", "a hand-off was refused: " + str(problem))
        return _refuse("forbidden", problem, 403)
    if not auth.allows(SUBMIT_SCOPE):
        return JsonResponse({"errors": [{"code": "insufficient_scope", "required": SUBMIT_SCOPE,
                                         "subject": auth.subject}]}, status=403)
    try:
        length = int(request.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length > _handoff.MAX_BODY_BYTES:
        return _refuse("bundle_too_large", "the request is %d bytes; the ceiling is %d"
                       % (length, _handoff.MAX_BODY_BYTES), 413)
    raw = request.read(_handoff.MAX_BODY_BYTES + 1)
    if len(raw) > _handoff.MAX_BODY_BYTES:
        return _refuse("bundle_too_large", "the request is over %d bytes"
                       % _handoff.MAX_BODY_BYTES, 413)
    try:
        b = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return _refuse("bad_json", "the body is not JSON", 400)
    if not isinstance(b, dict):
        return _refuse("bad_json", "the body is not a JSON object", 400)
    shape = secretscan.secret_problem({k: v for k, v in b.items() if k != "bundle_b64"})
    if shape:
        return _refuse("secret_shaped_payload", "refused: the payload looks like it contains %s"
                       % shape)
    declared = b.get("agent")
    if auth.mode == "scoped-agent":
        if declared not in (None, "", auth.subject):
            return _refuse("identity", "this credential is bound to %r, the payload declares %r"
                           % (auth.subject, declared), 403)
        agent = auth.subject
    else:
        if not _valid_agent_name(str(declared or "")):
            return _refuse("need_agent", "the shared credential requires a valid 'agent' name")
        agent = str(declared)
    b["task"] = _full_task(b.get("task"))
    b.setdefault("machine", request.headers.get("X-Hub-Machine") or "")
    b.setdefault("session", request.headers.get("X-Hub-Session") or "")
    hub_app.observe_presence(agent, request.headers)
    try:
        fields, data = _handoff.validate(b, agent, hosts=allowed_hosts(),
                                         task_exists=_task_exists, resolver=_resolver)
        return JsonResponse(_handoff.submit(hub_app.HUB_DIR, fields, data))
    except _handoff.Refusal as refusal:
        return JsonResponse(refusal.body(), status=refusal.status)


submit._hub_token_gated = True             # the route audit's marker: gated, just not by @writer
submit._hub_required_scope = SUBMIT_SCOPE


def handoffs_json(request):
    """GET /hub/handoffs.json -- open hand-offs oldest first (?status=all|published|failed,
    ?task=<id>)."""
    task = (request.GET.get("task") or "").strip()
    return JsonResponse(_handoff.listing(hub_app.HUB_DIR, request.GET.get("status") or "open",
                                         _full_task(task) if task else ""))


def _machine(request, b) -> str:
    return str(b.get("machine") or request.headers.get("X-Hub-Machine") or "").strip().lower()[:120]


@writer(scope=PUBLISH_SCOPE)
def claim(request, b):
    """POST /hub/api/handoff/claim {machine, ttl_s?, id?} -> a lease (token + fence), or
    data: null when nothing is waiting."""
    machine = _machine(request, b)
    if not machine:
        return _refuse("need_machine", "machine is the publisher's machine name")
    want = str(b.get("id") or "").strip()
    if want and not _handoff.HANDOFF_RE.match(want):
        return _refuse("bad_id", "id is h-<12 hex>")
    agent = b.get("agent") or request.hub_auth.subject
    return JsonResponse(_handoff.claim_next(hub_app.HUB_DIR, agent, machine,
                                            b.get("ttl_s") or _handoff.LEASE_TTL_S, want))


@writer(scope=PUBLISH_SCOPE)
def bundle(request, b):
    """POST /hub/api/handoff/bundle {id, token, fence} -> {data: {id, bundle_b64, bytes,
    sha256}}, for the holder of the newest claim only. JSON, so every client (the CLI and the
    MCP seam alike) reads it the same way."""
    hid = str(b.get("id") or "").strip()
    try:
        data = _handoff.bundle_bytes(hub_app.HUB_DIR, hid, b.get("token"), b.get("fence"))
    except _handoff.Refusal as refusal:
        return JsonResponse(refusal.body(), status=refusal.status)
    import hashlib
    return JsonResponse({"data": {"id": hid, "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest(),
                                  "bundle_b64": base64.b64encode(data).decode("ascii")}})


@writer(scope=PUBLISH_SCOPE)
def result(request, b):
    """POST /hub/api/handoff/result {id, token, fence, outcome: published|failed|released,
    pushed_sha | reason, conflicts?, note?}."""
    agent = b.get("agent") or request.hub_auth.subject
    machine = _machine(request, b)
    try:
        out = _handoff.record_result(
            hub_app.HUB_DIR, str(b.get("id") or "").strip(), agent, machine, b.get("token"),
            b.get("fence"), b.get("outcome"), b.get("pushed_sha") or "", b.get("reason") or "",
            b.get("conflicts") or [], b.get("note") or "", resolver=_resolver)
    except _handoff.Refusal as refusal:
        return JsonResponse(refusal.body(), status=refusal.status)
    body = {"data": out["data"]}
    if out.get("terminal"):
        body["data"]["notified"] = _tell_the_board(out["record"], agent, machine)
    return JsonResponse(body)


# ---------------------------------------------------------------------------- side effects

def _checkpoint(rec: dict, agent: str, machine: str) -> str:
    step = _handoff.checkpoint_step(rec, agent, machine)
    for _attempt in range(3):
        ent = (hub_app.current_state().get("entities") or {}).get(rec["task"])
        if not ent:
            return "task gone"
        plan = [dict(s) for s in (ent.get("plan") or []) if isinstance(s, dict)]
        plan.append(step)
        _resp, status = _append("task", rec["task"], {"type": "task", "plan": plan},
                                expected_version=ent.get("version"), agent=agent,
                                idem="handoff-step:%s:%s" % (rec["id"], rec["status"]),
                                etype="task.updated")
        if status in (200, 201):
            return "ok"
        if status != 409:
            return "refused %s" % status
    return "conflict"


def _message(rec: dict, agent: str, machine: str) -> str:
    """Tell the author, addressed to the author's MACHINE, not a console: the run that handed
    off has usually ended, and a message pinned to a dead console never lands."""
    author = str(rec.get("agent") or "").strip().lower()
    if not _valid_agent_name(author):
        return "no author"
    msg = _handoff.author_message(rec, agent, machine, board_link(rec["task"]))
    local = "m-handoff-%s-%s" % (rec["id"][2:], rec["status"][:4])
    note = {"type": "note", "title": msg["title"], "category": "context", "status": "standing",
            "tags": ["message", "open", "handoff"], "to": author, "from_agent": "hub",
            "body_md": msg["body"]}
    if rec.get("machine"):
        note["machine"] = str(rec["machine"])[:60]
    _resp, status = _append("note", ids.make_id(hub_app.PROJECT_KEY, "note", local), note,
                            expected_version=None, agent=agent, idem="handoff-msg:" + local,
                            etype="note.created")
    return "ok" if status in (200, 201) else "refused %s" % status


def _tell_the_board(rec: dict, agent: str, machine: str) -> dict:
    out = {}
    for name, fn in (("checkpoint", _checkpoint), ("message", _message)):
        try:
            out[name] = fn(rec, agent, machine)
        except Exception as exc:                             # noqa: BLE001
            log.exception("handoff: %s for %s failed", name, rec.get("id"))
            out[name] = "error %s" % type(exc).__name__
    return out
