"""The context menu's smart pass: a model ORGANISES a menu an app declared.

The hosted component (hub_core/components/context-menu/) draws whatever an app's catalog
declares. This module answers ONE question about that catalog: for this app, this page, this
kind of thing and this reader's role, how should the declared items be organised -- which to
pin at the top, how to split them into tabs, which optional ones to drop, and what a person
looking at this thing would most likely want to ask the page's agent about it.

    reader's browser              holds nothing
      -> the APP's own gated bridge (holds its hub credential; vouches for the person)
      -> POST /hub/api/menu/rank   the adapter's view, scope menu:rank
      -> rank()                    THIS module
      -> the configured model      HUB_MENU_MODEL_URL (OpenAI-compatible /chat/completions),
                                   falling back to the HUB_JUDGE_* model the hub already uses

WHAT THE MODEL MAY DO, AND WHAT IT MAY NOT. It may only NAME ids the app declared. Every id in
its answer is checked against the catalog it was given; an unknown id is dropped and counted,
never matched to a neighbour. It may hide ONLY an item the app marked `maybe`; a core action
cannot be taken off the menu by a model, and a destructive one is never pinned. It cannot add
an action, change a URL, or change what an action does -- the component executes only what the
catalog carries. The questions it suggests are drawn as questions for the agent, and nothing
runs until a person presses one. So the worst a wrong answer can do is a less useful order:
the failure mode of a heuristic is "do nothing".

UNCONFIGURED AND FAILED ARE REPORTED, NEVER SMOOTHED. No model, a model that did not answer,
and a reply that did not parse each come back `ok: false` with the reason and the specific
error; the component then draws the catalog's own order with no star.

CACHED, because the same app/page/kind/role asks the same question all day and a model is a
shared queue: one answer per signature for six hours, in memory. The status read never names
the model's address -- an inference host is infrastructure, not something a reader needs.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

#: The menu is drawn before this answers and never waits on it; the bound exists so a stuck
#: model does not hold a hub worker. A real catalog of about twenty items was measured at 21 s
#: on a mid-size model under shared load, and a 25 s bound failed the first ask of the day; the
#: answer is cached, so the cost is paid once per menu shape. The app's bridge should wait a
#: little longer than this, so the hub's verdict is the one a reader sees.
DEFAULT_TIMEOUT_S = 60.0
CACHE_SECONDS = 6 * 3600
CACHE_MAX = 400

MAX_ITEMS = 60
MAX_TABS = 4
MAX_PINNED = 3
MAX_ASKS = 3

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

_CACHE: dict = {}
_LOCK = threading.Lock()
_STATS = {"asked": 0, "cached": 0, "failed": 0, "dropped_ids": 0}


def config() -> dict:
    """The model this pass calls. Its own settings win; else the hub's judge model."""
    url = (os.environ.get("HUB_MENU_MODEL_URL") or os.environ.get("HUB_JUDGE_URL") or "").strip()
    model = (os.environ.get("HUB_MENU_MODEL") or os.environ.get("HUB_JUDGE_MODEL") or "").strip()
    token = (os.environ.get("HUB_MENU_MODEL_TOKEN") or os.environ.get("HUB_JUDGE_TOKEN") or "").strip()
    try:
        timeout = float(os.environ.get("HUB_MENU_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return {"url": url.rstrip("/"), "model": model, "token": token,
            "timeout": max(1.0, timeout)}


def _norm(value, cap: int) -> str:
    return " ".join(str(value or "").split())[:cap]


def _failed(reason: str, error: str) -> dict:
    with _LOCK:
        _STATS["failed"] += 1
    return {"ok": False, "reason": reason, "error": error}


def clean_request(body: dict) -> tuple:
    """(request, why_not). What the model is shown: only ids, labels and hints from the
    catalog, plus the shape of the target. Never a record's content -- the component sends
    fact values of 40 characters or fewer and this caps them again."""
    app = str(body.get("app") or "").strip().lower()
    if not _SLUG_RE.fullmatch(app):
        return None, "app must be the app's slug"
    kind = _norm(body.get("kind"), 40)
    if not kind:
        return None, "kind is required"
    items_in = body.get("items")
    if not isinstance(items_in, list) or not items_in:
        return None, "items must be a non-empty list"
    items, seen = [], set()
    for raw in items_in[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        if not _ID_RE.fullmatch(item_id) or item_id in seen or item_id.startswith("__"):
            continue
        seen.add(item_id)
        items.append({"id": item_id, "label": _norm(raw.get("label") or item_id, 60),
                      "hint": _norm(raw.get("hint"), 90), "group": _norm(raw.get("group"), 30),
                      "submenu": bool(raw.get("sub")), "optional": bool(raw.get("maybe")),
                      "destructive": bool(raw.get("danger"))})
    if not items:
        return None, "no usable item ids in the catalog"
    facts = body.get("facts") if isinstance(body.get("facts"), dict) else {}
    facts = {_norm(k, 30): _norm(v, 40) for k, v in list(facts.items())[:24] if _norm(k, 30)}
    recent = [str(r["id"]) for r in (body.get("recent") or [])[:5]
              if isinstance(r, dict) and str(r.get("id") or "") in seen]
    return {"app": app, "app_name": _norm(body.get("app_name") or app, 60),
            "page": _norm(body.get("page"), 120), "kind": kind,
            "kind_label": _norm(body.get("kind_label") or kind, 40),
            "role": _norm(body.get("role"), 30), "items": items, "facts": facts,
            "recent": recent, "has_agent": bool(body.get("has_agent"))}, ""


def signature(req: dict) -> str:
    """The cache key: everything that changes the answer, and nothing per record. The fact
    KEYS describe what kind of thing this is; their values are one record's and would make
    every card its own cache entry."""
    shape = {k: req[k] for k in ("app", "page", "kind", "role", "has_agent")}
    shape["items"] = [(i["id"], i["label"], i["optional"]) for i in req["items"]]
    shape["fact_keys"] = sorted(req["facts"])
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:32]


PROMPT = """You organise a right-click menu inside a business web app. You never invent actions: you only arrange the ones listed.

App: {app_name} ({app})
Page: {page}
The person right-clicked: a {kind_label} (kind "{kind}")
What the page knows about it (field names and short values): {facts}
Their role in this app: {role}
Actions they used most recently here, most used first: {recent}
The page {agent_line}

The actions this app offers for a {kind_label}, as JSON (id, label, hint, group, whether it opens a submenu, whether it is optional, whether it is destructive):
{items}

Decide, for a person doing this app's everyday work:
1. "pinned": up to 3 action ids that belong at the very top, most likely first. Never pin a destructive action.
2. "tabs": if there are more than 7 actions, split ALL of them into 2 to 4 tabs, each {{"label": "<1-2 words>", "items": [ids]}}, every id in exactly one tab, the most-used tab first. With 7 or fewer actions, return [].
3. "hidden": ids of OPTIONAL actions that make no sense for this kind of thing on this page. Only optional ids may appear here. Usually [].
4. "asks": {asks_rule}
5. "note": one short sentence (under 100 characters) saying what you optimised the menu for.

Reply with JSON only, no prose around it:
{{"pinned": [], "tabs": [], "hidden": [], "asks": [], "note": ""}}"""


def prompt(req: dict) -> str:
    agent_line = ("has a question-answering agent." if req["has_agent"]
                  else "has no question-answering agent.")
    asks_rule = ('up to 3 short questions a person looking at this ' + req["kind_label"] +
                 ' would genuinely want the agent to answer, each {"label": "<under 40 '
                 'chars>", "question": "<the full question, under 160 chars>"}. Generic '
                 'questions only; do not repeat the values above.'
                 if req["has_agent"] else "return [] (there is no agent on this page).")
    return PROMPT.format(
        app_name=req["app_name"], app=req["app"], page=req["page"] or "(unknown)",
        kind_label=req["kind_label"], kind=req["kind"],
        facts=json.dumps(req["facts"]) if req["facts"] else "(nothing)",
        role=req["role"] or "(not stated)",
        recent=", ".join(req["recent"]) if req["recent"] else "(none yet)",
        agent_line=agent_line, asks_rule=asks_rule, items=json.dumps(req["items"], indent=0))


def _chat(text: str, cfg: dict) -> str:
    body = {"max_tokens": 700, "temperature": 0.1, "messages": [{"role": "user", "content": text}]}
    if cfg["model"]:
        body["model"] = cfg["model"]
    headers = {"Content-Type": "application/json"}
    if cfg["token"]:
        headers["Authorization"] = "Bearer " + cfg["token"]
    request = urllib.request.Request(cfg["url"] + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=cfg["timeout"]) as response:
        payload = json.load(response)
    return str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def validate(reply: str, req: dict, model: str = "") -> tuple:
    """(ranking, why_not, dropped). Shape-tolerant about the WRAPPING (fences, a sentence
    before the JSON) and strict about the CONTENT: an id the catalog did not declare is
    dropped and counted, never matched to a neighbour."""
    match = re.search(r"\{.*\}", str(reply or ""), re.S)
    if not match:
        return None, "the model's reply held no JSON object", 0
    try:
        obj = json.loads(match.group(0))
    except ValueError as exc:
        return None, "the model's JSON did not parse: %s" % exc, 0
    if not isinstance(obj, dict):
        return None, "the model's JSON was not an object", 0
    known = {i["id"]: i for i in req["items"]}
    dropped = 0

    def ids(values, allow=lambda i: True):
        nonlocal dropped
        out = []
        for v in values if isinstance(values, list) else []:
            v = str(v or "").strip()
            if v in known and v not in out and allow(known[v]):
                out.append(v)
            elif v:
                dropped += 1
        return out

    pinned = ids(obj.get("pinned"), lambda i: not i["destructive"])[:MAX_PINNED]
    hidden = ids(obj.get("hidden"), lambda i: i["optional"])
    tabs, placed = [], set()
    for tab in (obj.get("tabs") if isinstance(obj.get("tabs"), list) else [])[:MAX_TABS]:
        if not isinstance(tab, dict):
            continue
        label = _norm(tab.get("label"), 20)
        members = [i for i in ids(tab.get("items")) if i not in placed]
        if label and members:
            placed.update(members)
            tabs.append({"label": label, "items": members})
    if len(tabs) < 2:
        tabs = []
    asks = []
    if req["has_agent"]:
        for a in (obj.get("asks") if isinstance(obj.get("asks"), list) else [])[:MAX_ASKS]:
            if not isinstance(a, dict):
                continue
            question = _norm(a.get("question"), 160)
            if len(question) >= 8:
                asks.append({"label": _norm(a.get("label") or question, 40), "question": question})
    return ({"pinned": pinned, "tabs": tabs, "hidden": hidden, "asks": asks,
             "note": _norm(obj.get("note"), 120), "model": model}, "", dropped)


def status() -> dict:
    """The lane's own state: whether a model is configured, its name, the bound, and what it
    has done since this process started. Never the model's address."""
    cfg = config()
    with _LOCK:
        stats = dict(_STATS)
        cached = len(_CACHE)
    return {"ok": True, "configured": bool(cfg["url"]), "model": cfg["model"],
            "timeout_s": cfg["timeout"], "cache_entries": cached, "stats": stats}


def rank(body) -> tuple:
    """(http_status, response body) for one POST."""
    if not isinstance(body, dict):
        return 400, _failed("failed", "the request body must be a JSON object")
    req, why = clean_request(body)
    if req is None:
        return 400, _failed("failed", why)
    cfg = config()
    if not cfg["url"]:
        return 200, _failed("unconfigured", "The hub has no model configured for the menu "
                                            "(set HUB_MENU_MODEL_URL, or HUB_JUDGE_URL).")
    key = signature(req)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            _STATS["cached"] += 1
            return 200, {"ok": True, "ranking": hit[1], "cached": True}
    started = time.time()
    try:
        reply = _chat(prompt(req), cfg)
    except urllib.error.HTTPError as exc:
        return 200, _failed("failed", "the model answered HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        return 200, _failed("failed", "the model could not be reached: %s" % (exc.reason,))
    except TimeoutError:
        return 200, _failed("failed", "the model did not answer within %ss" % cfg["timeout"])
    except (ValueError, OSError) as exc:
        return 200, _failed("failed", "the model call failed: %s: %s" % (type(exc).__name__, exc))
    ranking, why, dropped = validate(reply, req, cfg["model"])
    if ranking is None:
        return 200, _failed("failed", "%s; reply began %r" % (why, reply[:120]))
    ranking["ms"] = int((time.time() - started) * 1000)
    with _LOCK:
        _STATS["asked"] += 1
        _STATS["dropped_ids"] += dropped
        if len(_CACHE) >= CACHE_MAX:
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: CACHE_MAX // 4]:
                _CACHE.pop(stale, None)
        _CACHE[key] = (now, ranking)
    return 200, {"ok": True, "ranking": ranking, "cached": False, "dropped": dropped}
