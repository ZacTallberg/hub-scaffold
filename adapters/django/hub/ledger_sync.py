"""Ledger sync over HTTPS: a worker client's one write path to a REMOTE board.

A project whose canonical ledger is written on a workstation first (the local board is the record,
every worker write reaches the served board as a DELTA, and a failed push stays durable locally as
sync debt) needs a seam that extends the served chain without ever forking it.

DELTA is the steady state and keeps the body small no matter how large the ledger grows:
{"delta": "<jsonl of events past the remote head>"}. The first delta event must be seq head+1 with
prev_hash == the current head hash, so a gapped or forked tail is refused (409) instead of
corrupting the chain; on success the tail is appended atomically and every appended event wakes
the live streams exactly as a local append would.

FULL is the recovery path only, for an empty or diverged remote: {"ledger": "<whole jsonl>"}. It
must parse, link (seq 1..N) and strictly extend the current head (never shorter, never a fork),
then the file is replaced atomically.

Scope ``ledger:sync``. The route is a writer like any other: the credential is the boundary.
"""
import json

from django.http import JsonResponse

from hub_core.canonical import canonical, sha256_hex
from hub_core.store import _HASH_FIELDS, LedgerLock, durable_replace, jsonl_tail_hash

from . import hub_app
from .hub_write import writer


def _recomputed_hash(ev, prev):
    return sha256_hex(prev + canonical({k: ev.get(k) for k in _HASH_FIELDS}))


def _parse(lines):
    events = []
    for i, ln in enumerate(lines):
        try:
            events.append(json.loads(ln))
        except Exception:
            return None, JsonResponse({"errors": [{"code": "parse", "line": i + 1}]}, status=422)
    return events, None


def _head():
    s = hub_app.store()
    try:
        cur = s.latest_cursor()
    finally:
        s.close()
    return cur.get("seq", 0), cur.get("hash", "")


def _write(new_text):
    """Replace the ledger file crash-durably, then reconcile through a fresh store and return the
    new cursor. An unfsynced replace here could zero the whole served board on a crash."""
    ledger_path = hub_app.HUB_DIR / "events.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    durable_replace(ledger_path, new_text)
    s2 = hub_app.store()
    try:
        return s2.latest_cursor()
    finally:
        s2.close()


def _chain_errors(events, first_seq, prev):
    for k, ev in enumerate(events):
        at = first_seq + k
        if ev.get("seq") != at:
            return JsonResponse({"errors": [{"code": "seq_gap", "at": at, "got": ev.get("seq")}]},
                                status=422)
        if ev.get("prev_hash", "") != prev:
            return JsonResponse({"errors": [{"code": "chain_break", "at": at}]}, status=422)
        if ev.get("hash") != _recomputed_hash(ev, prev):
            return JsonResponse({"errors": [{"code": "forged_hash", "at": at,
                                 "msg": "event hash != sha256(prev_hash + canonical(fields))"}]},
                                status=422)
        prev = ev.get("hash", "")
    return None


@writer(scope="ledger:sync")
def sync(request, b):
    if b.get("delta") is not None:
        return _delta(b)
    if b.get("ledger") is not None:
        return _full(b)
    return JsonResponse({"errors": [{"code": "need_delta_or_ledger"}]}, status=400)


def _delta(b):
    lines = [ln for ln in str(b.get("delta") or "").split("\n") if ln.strip()]
    # The whole cursor-read -> validate -> file-replace span holds the ledger lock: validated-then-
    # written against a head an appender moved mid-window is how an acked event can vanish.
    with LedgerLock(hub_app.HUB_DIR):
        head_seq, head_hash = _head()
        if not lines:
            return JsonResponse({"data": {"ok": True, "seq": head_seq, "count": 0,
                                          "mode": "delta", "noop": True}}, status=200)
        events, err = _parse(lines)
        if err:
            return err
        if events[0].get("seq") != head_seq + 1 or events[0].get("prev_hash", "") != head_hash:
            return JsonResponse({"errors": [{"code": "stale_base",
                                 "msg": "delta does not extend the current head",
                                 "current_seq": head_seq, "current_hash": head_hash}]}, status=409)
        err = _chain_errors(events, head_seq + 1, head_hash)
        if err:
            return err
        ledger_path = hub_app.HUB_DIR / "events.jsonl"
        # The file's own last line must equal the head this delta was validated against; an
        # un-indexed line past it (a crashed writer's residue) would be forked over by an append.
        if jsonl_tail_hash(ledger_path) != head_hash:
            return JsonResponse({"errors": [{"code": "stale_file_tail",
                                 "msg": "the ledger file's tail no longer matches the validated head",
                                 "current_seq": head_seq, "current_hash": head_hash}]}, status=409)
        existing = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        newcur = _write(existing + "\n".join(lines) + "\n")
    # Durable first, then the wake-up: the same order every local append keeps.
    for ev in events:
        hub_app.publish_event(ev)
    return JsonResponse({"data": {"ok": True, "seq": newcur.get("seq"), "count": len(events),
                                  "hash": newcur.get("hash"), "mode": "delta"}}, status=200)


def _full(b):
    ledger = b.get("ledger")
    if not isinstance(ledger, str) or not ledger.strip():
        return JsonResponse({"errors": [{"code": "need_ledger"}]}, status=400)
    lines = [ln for ln in ledger.split("\n") if ln.strip()]
    events, err = _parse(lines)
    if err:
        return err
    err = _chain_errors(events, 1, "")
    if err:
        return err
    incoming = events[-1]["seq"] if events else 0
    with LedgerLock(hub_app.HUB_DIR):
        cur_seq, cur_hash = _head()
        if incoming < cur_seq:
            return JsonResponse({"errors": [{"code": "regression", "incoming": incoming,
                                             "current": cur_seq}]}, status=409)
        if cur_seq > 0 and (cur_seq > len(events) or events[cur_seq - 1].get("hash", "") != cur_hash):
            return JsonResponse({"errors": [{"code": "divergent",
                                 "msg": "incoming chain does not extend the current head",
                                 "current": cur_seq}]}, status=409)
        if jsonl_tail_hash(hub_app.HUB_DIR / "events.jsonl") != cur_hash:
            return JsonResponse({"errors": [{"code": "stale_file_tail",
                                 "msg": "the ledger file's tail no longer matches the validated head",
                                 "current_seq": cur_seq, "current_hash": cur_hash}]}, status=409)
        newcur = _write("\n".join(lines) + "\n")
    hub_app._publish_realtime("ledger.replaced", seq=newcur.get("seq"), hash=newcur.get("hash"))
    for ev in events[cur_seq:]:
        hub_app.publish_event(ev)
    return JsonResponse({"data": {"ok": True, "seq": newcur.get("seq"), "count": len(events),
                                  "hash": newcur.get("hash"), "mode": "full"}}, status=200)
