"""Knowledge candidates: what an observer distilled, held for a PERSON before it is shared.

A session observer (any tool that reads a finished session and distils what would cause real
damage or waste if forgotten) produces candidate lessons on the machine that had the session.
Filing them straight into the knowledge base would put an unreviewed model's summary into every
prompt on every machine; dropping them loses what the session learned. So they queue HERE,
review-first:

    POST /hub/api/knowledge-candidates          {items: [{id, text, kind?, importance?, ...}]}
    GET  /hub/knowledge-candidates.json?status=open|adopted|declined|all
    POST /hub/api/knowledge-candidate/decide    {id, decision: adopt|decline, as: lesson|finding, note}

NOTHING HERE IS KNOWLEDGE YET. The queue is a sidecar (HUB_DIR/knowledge_candidates.jsonl), never
the ledger, so no prompt index, feed, search or recall can serve a candidate. A candidate becomes
a lesson or a finding only when a DECIDER adopts it -- a scoped credential whose subject is named in
HUB_DECIDERS (default the operator), the same gate as a decision task; the shared-root token and
every agent are refused, because an adopted candidate rides every prompt. Nothing auto-promotes.
A decline needs a reason and stays declined: re-sending an outbox never reopens a decision.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import threading

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from hub_core import atomic as _atomic
from hub_core import ids
from hub_core.process_lock import ProcessFileLock

from . import hub_app
from . import knowledge_write as _kw
from .hub_write import _append, _deciders, writer

MAX_ITEMS = 100
MAX_TEXT = 2000
KEEP = 5000
_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,95}$")
_LOCK = threading.Lock()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path():
    return hub_app.HUB_DIR / "knowledge_candidates.jsonl"


def _read() -> list:
    try:
        text = _path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue                          # a torn line is skipped, never fatal
        if isinstance(row, dict) and row.get("id"):
            out.append(row)
    return out


def _write(rows: list) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_text(_path(), "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                        for r in rows[-KEEP:]))


def _locked():
    hub_app.HUB_DIR.mkdir(parents=True, exist_ok=True)
    return ProcessFileLock(hub_app.HUB_DIR, name=".knowledge-candidates.lock", timeout=15)


@writer(scope="note:write")
def submit(request, b):
    """File candidates. Idempotent by id: a candidate already queued is left exactly as it is,
    whatever its status, so a machine re-sending its outbox never reopens a decision."""
    items = b.get("items")
    if not isinstance(items, list) or not items:
        return JsonResponse({"errors": [{"code": "need_items"}]}, status=422)
    if len(items) > MAX_ITEMS:
        return JsonResponse({"errors": [{"code": "too_many", "max": MAX_ITEMS}]}, status=413)
    agent = str(b.get("agent") or "")[:64]
    machine = str(request.headers.get("X-Hub-Machine") or "").strip().lower()[:64]
    added, known, refused = [], 0, []
    with _LOCK, _locked():
        rows = _read()
        have = {r["id"] for r in rows}
        for it in items:
            if not isinstance(it, dict):
                refused.append({"code": "not_an_object"})
                continue
            cid = str(it.get("id") or "").strip().lower()
            text = " ".join(str(it.get("text") or "").split())
            if not _ID.match(cid) or len(text) < 20:
                refused.append({"id": cid[:80], "code": "bad_id_or_text",
                                "msg": "id is 3-96 of [a-z0-9._:-]; text is at least 20 characters"})
                continue
            if cid in have:
                known += 1
                continue
            try:
                importance = int(it.get("importance") or 0)
            except (TypeError, ValueError):
                importance = 0
            rows.append({"id": cid, "status": "open", "text": text[:MAX_TEXT],
                         "kind": str(it.get("kind") or "")[:20], "importance": importance,
                         "project": str(it.get("project") or "")[:80],
                         "occurred_on": str(it.get("occurred_on") or "")[:25],
                         "machine": str(it.get("machine") or machine)[:64], "agent": agent,
                         "model": str(it.get("model") or "")[:64], "queued_at": _now()})
            have.add(cid)
            added.append(cid)
        if added:
            _write(rows)
    return JsonResponse({"data": {"added": len(added), "already_queued": known,
                                  "refused": refused, "ids": added}},
                        status=201 if added else 200)


@require_GET
def candidates_json(request):
    """The queue, oldest first. ``?status=`` open (default) | adopted | declined | all."""
    status = (request.GET.get("status") or "open").strip().lower()
    every = _read()
    rows = every if status == "all" else [r for r in every if r.get("status") == status]
    counts = {}
    for r in every:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    return JsonResponse({"data": rows, "metadata": {"count": len(rows), "by_status": counts}})


def _adopt(row, as_, note, person):
    """Write the adopted candidate as a lesson or a finding. (resp, status)."""
    prov = "Adopted from %s's session observer (%s, %s, importance %s) by %s on %s." % (
        row.get("machine") or "a machine", row.get("kind") or "observation",
        row.get("occurred_on") or "undated", row.get("importance"), person, _now()[:10])
    tags = ["from-observer"] + ([re.sub(r"[^a-z0-9._-]+", "-", row["kind"].lower())[:40]]
                                if row.get("kind") else [])
    text = row["text"]
    if as_ == "lesson":
        state = hub_app.current_state()
        dup = _kw._identical_live_rule(state, text)
        if dup is not None:
            return {"data": {"id": dup.get("id"), "duplicate_of": dup.get("id"), "written": False}}, 200
        eid = _kw._lesson_id(text)
        payload = {"type": "note", "category": "gotcha", "title": text,
                   "body_md": ((note + "\n\n") if note else "") + prov, "status": "standing",
                   "tier": "normal", "tags": ["lesson", "memory"] + tags,
                   "verified_as_of": _now()[:10]}
        payload.update(_kw.overlap_tag(state, text, eid))
    else:
        local = "finding-obs-" + hashlib.sha1(row["id"].encode()).hexdigest()[:12]
        eid = ids.make_id(hub_app.PROJECT_KEY, "note", local)
        payload = {"type": "note", "category": "discovery", "title": text[:300],
                   "body_md": text + ("\n\n" + note if note else "") + "\n\n" + prov,
                   "status": "standing", "tags": ["finding"] + tags,
                   "verified_as_of": _now()[:10]}
    return _append("note", eid, payload, expected_version=None, agent=person,
                   idem="adopt-candidate:%s" % row["id"], etype="note.created")


@writer(scope="note:write")
def decide(request, b):
    """Adopt a candidate as shared knowledge, or decline it -- a person's call, never a rule's."""
    auth = request.hub_auth
    if auth.mode != "scoped-agent" or auth.subject.lower() not in _deciders():
        return JsonResponse({"errors": [{"code": "decision_needs_a_person",
            "msg": "a candidate becomes shared knowledge only on a decider's call: decide with a "
                   "credential issued to a named decider (HUB_DECIDERS)"}]}, status=403)
    person = auth.subject
    cid = str(b.get("id") or "").strip().lower()
    decision = str(b.get("decision") or "").strip().lower()
    as_ = str(b.get("as") or "lesson").strip().lower()
    note = " ".join(str(b.get("note") or "").split())[:600]
    if decision not in ("adopt", "decline") or as_ not in ("lesson", "finding"):
        return JsonResponse({"errors": [{"code": "bad_decision",
            "msg": "decision is adopt|decline; as is lesson|finding"}]}, status=422)
    if decision == "decline" and not note:
        return JsonResponse({"errors": [{"code": "need_note",
            "msg": "a decline needs a note saying why, so whoever tunes the observer learns "
                   "something"}]}, status=422)
    with _LOCK, _locked():
        rows = _read()
        row = next((r for r in rows if r.get("id") == cid), None)
        if row is None:
            return JsonResponse({"errors": [{"code": "not_found", "msg": cid}]}, status=404)
        if row.get("status") != "open":
            return JsonResponse({"errors": [{"code": "already_decided", "status": row.get("status"),
                                             "record": row.get("record")}]}, status=409)
        if decision == "adopt":
            resp, status = _adopt(row, as_, note, person)
            if status not in (200, 201):
                return JsonResponse(resp, status=status)
            data = resp.get("data") or {}
            row["record"] = data.get("duplicate_of") or data.get("id")
            if data.get("duplicate_of"):
                row["duplicate_of"] = data["duplicate_of"]
            row["as"] = as_
        row.update(status="adopted" if decision == "adopt" else "declined", decided_by=person,
                   decided_at=_now(), note=note)
        _write(rows)
    return JsonResponse({"data": row})
