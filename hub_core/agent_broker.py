"""The hub as the ONE credential broker between the apps around it and an agent service.
Framework-free (stdlib urllib); the Django adapter serves it at /hub/api/agent/*.

    reader's browser                  holds nothing
      -> the APP's own gated endpoint  (its bridge; holds the app's hub credential)
      -> POST /hub/api/agent/ask       (this module; holds the AGENT SERVICE KEY)
      -> POST <HUB_AGENT_URL>/ask

One agent credential, in one place. The alternative -- every adopting app issued its own key
to the agent service -- means N copies to rotate, N apps to audit, and a key that outlives
its app. Here an app that already has a hub credential gets the agent with two tags, a
mount point and a small server-side bridge, and the agent key never leaves the hub.

WHAT THIS DOES NOT DO. It does not decide who the reader is: it cannot see them. It
authenticates the APP; the app's bridge vouches for the person (`person`), exactly as it
does for the profile and feed routes. So the agent component must only be linked from a page
that is already behind the app's own sign-in.

THE UPSTREAM CONTRACT (implement it in front of whichever agent service you run):

    POST {HUB_AGENT_URL}/ask
         {"query": <prompt incl. recent turns>, "question": <as typed>, "context": <corpus>,
          "app": <slug>, "on_behalf_of"?: <person>, "conversation_id"?: <uuid>}
      -> {"answer": str, "citations"?: [{"label"|"title", "href"?, "page"?}],
          "conversation_id"?: <uuid>}
      error -> non-2xx with {"code"?: str, "error"?: str}
    GET  {HUB_AGENT_URL}/conversations?on_behalf_of=<person>[&app=<slug>]&limit=50
      -> {"conversations": [{"id", "title", "app", "updated_at"}], "truncated"?: bool}
    GET  {HUB_AGENT_URL}/conversations/<uuid>?on_behalf_of=<person>
      -> {"conversation": {"id", "title", "turns": [{"role": "you"|"agent", "text"}]}}

Two refusal codes are understood: `conversation_not_found` (the thread was deleted -- the
question is answered as a new conversation) and `delegation_forbidden` (the key may not act
for people -- the question is still answered, as the service itself, with history reported
off and why). History is the extra; the answer is the job.

EVERY FAILURE IS NAMED, NEVER SMOOTHED. Unconfigured says which setting is missing; an
unreachable, slow or refusing service says so; an EMPTY answer is a fault, never a blank
bubble a reader would read as "the agent knows nothing". Settings (Django setting or env):
HUB_AGENT_URL, HUB_AGENT_KEY, HUB_AGENT_CONTEXT, HUB_AGENT_LABEL, HUB_AGENT_TIMEOUT_S (90),
HUB_AGENT_TLS_VERIFY (true; set false only for an internal self-signed service you trust).
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

THREAD_TURNS = 6
MAX_QUESTION_CHARS = 4000
MAX_RESPONSE_BYTES = 4_000_000

_PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,99}$")
_APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Lane:
    """The configured upstream, read at call time so a settings change needs no code change."""

    def __init__(self, url="", key="", context="", label="Agent", timeout_s=90,
                 tls_verify=True):
        self.url = str(url or "").strip().rstrip("/")
        self.key = str(key or "").strip()
        self.context = str(context or "").strip()
        self.label = str(label or "Agent").strip() or "Agent"
        try:
            self.timeout_s = max(5, min(int(timeout_s), 600))
        except (TypeError, ValueError):
            self.timeout_s = 90
        self.tls_verify = str(tls_verify).strip().lower() not in ("0", "false", "no", "off")

    def missing(self, need_context=True) -> str:
        """The one setting that is absent, named -- or "" when the lane is configured."""
        if not self.url:
            return ("The hub has no HUB_AGENT_URL, so it does not know where the agent is. "
                    "It cannot answer until that is set.")
        if not self.key:
            return "The hub holds no agent service key (HUB_AGENT_KEY), so it cannot ask yet."
        if need_context and not self.context:
            return ("No corpus is set for the agent (HUB_AGENT_CONTEXT), so there is nothing "
                    "for it to answer from yet.")
        return ""


def person(raw) -> str:
    """The person an app names, normalised (DOMAIN\\user and user@domain both reduce to user)."""
    name = str(raw or "").strip().casefold().split("@", 1)[0].rsplit("\\", 1)[-1]
    return name if _PERSON_RE.fullmatch(name) else ""


def app(raw) -> str:
    slug = str(raw or "").strip().lower()
    return slug if _APP_RE.fullmatch(slug) else ""


def uuid(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if _UUID_RE.fullmatch(value) else ""


def compose(question: str, thread) -> str:
    """One prompt: the upstream takes a question, not a chat. Earlier turns ride as plainly
    labelled context (never pretending to be a history the far side had), question last."""
    turns = []
    for entry in (thread if isinstance(thread, list) else [])[-THREAD_TURNS:]:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if text:
            who = "Person" if str(entry.get("role")) == "you" else "You (the assistant)"
            turns.append("%s: %s" % (who, text))
    if not turns:
        return question
    return ("Earlier in this conversation:\n" + "\n".join(turns)
            + "\n\nThe person now asks: " + question)


def _context(lane: Lane):
    ctx = ssl.create_default_context()
    if not lane.tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _call(lane: Lane, method: str, path: str, *, body=None, params=None, timeout=None):
    """(payload | None, error_text, refusal_code)."""
    url = lane.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + lane.key, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {})})
    wait = timeout or lane.timeout_s
    try:
        with urllib.request.urlopen(request, context=_context(lane), timeout=wait) as response:
            return json.loads(response.read(MAX_RESPONSE_BYTES + 1) or b"{}"), "", ""
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read(20_000) or b"{}")
        except ValueError:
            said = {}
        said = said if isinstance(said, dict) else {}
        code, detail = str(said.get("code") or ""), str(said.get("error") or "")
        if exc.code == 401:
            return None, "The hub's agent service key was refused. It needs re-issuing.", code
        if exc.code == 429:
            return None, "The agent is at its request limit right now. Try again in a minute.", code
        return None, "The agent answered HTTP %d%s." % (exc.code, ": " + detail if detail else ""), code
    except urllib.error.URLError as exc:
        return None, "The agent could not be reached: %s." % (exc.reason,), ""
    except TimeoutError:
        return None, "The agent did not answer within %d seconds." % wait, ""
    except ValueError:
        return None, "The agent answered with something that was not JSON.", ""


def _citations(payload: dict) -> list[dict]:
    """Citations reduced to a label a person recognises and a link when one exists. A citation
    with no link is still SHOWN: dropping it makes an answer look less grounded than it is."""
    out = []
    for item in (payload.get("citations") or [])[:8]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("title") or item.get("document") or "").strip()
        if label and item.get("page"):
            label = "%s · p.%s" % (label, item["page"])
        if not label:
            continue
        href = str(item.get("href") or "")
        out.append({"label": label[:200],
                    "href": href if href.startswith(("https://", "http://", "/")) else ""})
    return out


def ask(lane: Lane, body: dict) -> tuple[int, dict]:
    """(http_status, response body) for one question an app asks on a reader's behalf."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "reason": "failed", "error": "the body must be a JSON object"}
    question = str(body.get("question") or "").strip()
    if not question:
        return 400, {"ok": False, "reason": "failed", "error": "ask a question"}
    if len(question) > MAX_QUESTION_CHARS:
        return 400, {"ok": False, "reason": "failed",
                     "error": "that question is longer than %d characters" % MAX_QUESTION_CHARS}
    missing = lane.missing()
    if missing:
        return 200, {"ok": False, "reason": "unconfigured", "error": missing}
    who, conversation, slug = person(body.get("person")), uuid(body.get("conversation_id")), app(body.get("app"))
    extra = {"question": question, "context": lane.context, "app": slug,
             "query": compose(question, body.get("thread"))}
    if who:
        extra["on_behalf_of"] = who
        if conversation:
            extra["conversation_id"] = conversation
    history = "on" if who else "off"
    payload, error, code = _call(lane, "POST", "/ask", body=extra)
    if payload is None and code == "conversation_not_found" and conversation:
        extra.pop("conversation_id", None)        # deleted thread: answer as a new one
        payload, error, code = _call(lane, "POST", "/ask", body=extra)
    if payload is None and code == "delegation_forbidden":
        extra.pop("on_behalf_of", None)           # the answer is the job; history is the extra
        extra.pop("conversation_id", None)
        payload, error, code = _call(lane, "POST", "/ask", body=extra)
        history = "unconfigured"
    if payload is None:
        return 200, {"ok": False, "reason": "failed", "error": error}
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return 200, {"ok": False, "reason": "failed",
                     "error": str((payload or {}).get("error") or "The agent declined to answer.")}
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        return 200, {"ok": False, "reason": "failed",
                     "error": "The agent returned an empty answer. That is a fault on its side."}
    return 200, {
        "ok": True, "answer": answer, "citations": _citations(payload),
        "meta": "%s · %s" % (lane.label, lane.context),
        # Only a PERSON's conversation can be reopened; a service-held one is nobody's.
        "conversation_id": uuid(payload.get("conversation_id")) if history == "on" else "",
        "history": history,
    }


def _history_refusal(error: str, code: str) -> tuple[int, dict]:
    if code == "delegation_forbidden":
        return 200, {"ok": False, "reason": "unconfigured",
                     "error": "Past chats are not switched on: the agent service has not been told "
                              "this hub may read conversations on a person's behalf."}
    if code == "conversation_not_found":
        return 404, {"ok": False, "reason": "failed", "error": "That conversation is not there any more."}
    return 200, {"ok": False, "reason": "failed", "error": error}


def history(lane: Lane, raw_person, raw_app, scope: str = "") -> tuple[int, dict]:
    """A person's past conversations: this app's (scope=app, the default when an app is
    named) or every one they have had (scope=all). Newest first, as the service orders them."""
    who = person(raw_person)
    if not who:
        return 400, {"ok": False, "reason": "failed", "error": "name the person whose history this is"}
    missing = lane.missing(need_context=False)
    if missing:
        return 200, {"ok": False, "reason": "unconfigured", "error": missing}
    slug = app(raw_app)
    scope = "all" if (scope == "all" or not slug) else "app"
    params = {"on_behalf_of": who, "limit": "50"}
    if scope == "app":
        params["app"] = slug
    payload, error, code = _call(lane, "GET", "/conversations", params=params, timeout=30)
    if payload is None:
        return _history_refusal(error, code)
    rows = [c for c in (payload.get("conversations") or []) if isinstance(c, dict)]
    return 200, {"ok": True, "scope": scope, "app": slug, "conversations": rows,
                 "truncated": bool(payload.get("truncated"))}


def conversation(lane: Lane, raw_person, raw_id) -> tuple[int, dict]:
    who, cid = person(raw_person), uuid(raw_id)
    if not who:
        return 400, {"ok": False, "reason": "failed", "error": "name the person whose conversation this is"}
    if not cid:
        return 400, {"ok": False, "reason": "failed", "error": "name the conversation"}
    missing = lane.missing(need_context=False)
    if missing:
        return 200, {"ok": False, "reason": "unconfigured", "error": missing}
    payload, error, code = _call(lane, "GET", "/conversations/" + cid,
                                 params={"on_behalf_of": who}, timeout=30)
    if payload is None:
        return _history_refusal(error, code)
    return 200, {"ok": True, "conversation": payload.get("conversation") or {}}
