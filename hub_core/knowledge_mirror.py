"""A LOCAL MIRROR of the board's knowledge, written where a machine's own memory engine indexes it,
and the ONE rule that decides whether that engine or the hub delivers knowledge on a prompt.

The per-prompt knowledge block (`client prompt-context`) ships a ranked slice of the board's
memory on every prompt, from the hub, over the network, re-ranked each time. A mirror asks once
for everything and afterwards only for what changed (GET /hub/knowledge/since), and appends it
as an operation log a local memory engine replays:

    <feed>.jsonl          one JSON op per line: put | revoke | reset (the feed's own items)
    <feed>.state.json     the SIDECAR: cursor, etag, more, ok_at, error, local_owns, written_at

A put becomes one searchable record, a revoke deletes it (a retired lesson really leaves the
index), a reset drops one record type before its re-send. Ranking then happens at home, at
zero network cost and with the hub down. The FILES are the contract: nothing here imports a
memory engine, and the engine never needs to know the hub.

Discipline:
  * at most one poll per `min_interval` (the caller's rate; a background daemon passes minutes,
    a person running the verb by hand passes 0); a caught-up poll sends its ETag on every
    carrier (If-None-Match, X-Hub-ETag) and gets a 304 with no body;
  * the cursor is OPAQUE -- stored verbatim, sent back verbatim;
  * the cursor is committed only AFTER the page is on disk (flushed + fsynced), so a crash
    re-fetches a page rather than losing one; every put is idempotent by id + text_sha;
  * the bootstrap pages until `more` is false, at most `max_pages` pages per pass;
  * a feed file that vanished (cleared by a person, a new profile) is a fresh bootstrap --
    keeping the old cursor would leave the index holding only what changed since;
  * past `compact_bytes` the file is rewritten as its live set (latest put per id, revoked ids
    dropped), written aside and swapped in, so the reader sees a new file and replays it.

THE HAND-OFF. When local memory serves knowledge, the hub block must not ALSO be printed, or
every record arrives twice; when local memory is not healthy, the hub block must be printed, or
the prompt gets nothing. Both sides therefore decide on the same three facts, read from files:

  1. the switch is on (HUB_LOCAL_KNOWLEDGE=1) -- default OFF;
  2. the mirror is fresh: its last pass came back caught up (`more` false) with no error,
     within FRESH_S;
  3. local recall is healthy: the engine's health file (HUB_LOCAL_MEMORY_HEALTH, JSON with
     `retrieval_mode` and `ts`) says its last recall ran in a healthy mode within HEALTHY_S.

Any doubt -- a missing file, an unreadable one, an error -- fails CLOSED to the hub block.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

MIN_INTERVAL_S = 300             # what a background daemon passes: knowledge is not live state
BOOTSTRAP_INTERVAL_S = 5         # while `more` is true, keep paging on the next pass
PAGE_LIMIT = 500                 # the hub's own cap (knowledge.FEED_MAX)
MAX_PAGES_PER_PASS = 12
COMPACT_BYTES = 24 * 1024 * 1024
#: "Fresh" = a caught-up, error-free pass within two of a daemon's five-minute polls.
FRESH_S = 600
#: Local recall is healthy when its last recall ran a healthy mode within this window.
HEALTHY_S = 900
#: A machine whose recent recalls fell back to a degraded mode is not current; "recent" so a
#: machine nobody has prompted today is not graded on an old line.
DEGRADED_WINDOW_S = 3600
ROUTE = "knowledge/since"


def default_feed() -> Path:
    state = os.environ.get("HUB_CLIENT_STATE_DIR") or os.path.join("~", ".hub-client")
    return Path(os.path.expanduser(os.environ.get("HUB_KNOWLEDGE_FEED")
                                   or os.path.join(state, "feeds", "knowledge.jsonl")))


def sidecar_path(feed: Path) -> Path:
    return feed.with_name(feed.stem + ".state.json")


def healthy_modes() -> set:
    raw = os.environ.get("HUB_LOCAL_MEMORY_MODES") or "hybrid"
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def read_state(feed: Path) -> dict:
    try:
        doc = json.loads(sidecar_path(feed).read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def switch_on() -> bool:
    return os.environ.get("HUB_LOCAL_KNOWLEDGE", "").strip().lower() in ("1", "true", "yes")


def _write_state(feed: Path, state: dict, now: float) -> None:
    """The sidecar, rewritten on EVERY pass (not only on a poll) so a flipped switch reaches the
    local engine within one pass. Atomic: written aside and swapped in."""
    doc = dict(state, feed=feed.stem, local_owns=switch_on(), written_at=now)
    side = sidecar_path(feed)
    try:
        side.parent.mkdir(parents=True, exist_ok=True)
        tmp = side.with_name(side.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, side)
    except OSError:
        pass                                            # the engine then treats the feed as not owned


def _append(feed: Path, items: list) -> int:
    """Append one JSON line per op and make it durable before the cursor moves."""
    if not items:
        return 0
    feed.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(i, ensure_ascii=False, separators=(",", ":")) + "\n" for i in items)
    with open(feed, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return len(items)


def compact(feed: Path) -> dict:
    """Rewrite the feed as its live set: the latest put per id, with every revoked id and every
    record a reset dropped left out. A torn final line (a crash mid-append) is not kept."""
    live: dict = {}
    lines = 0
    with open(feed, "r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.endswith("\n"):
                break
            try:
                op = json.loads(raw)
            except ValueError:
                continue
            lines += 1
            kind = op.get("op")
            if kind == "put" and op.get("id"):
                live.pop(op["id"], None)                # re-insert: latest put, latest position
                live[op["id"]] = op
            elif kind == "revoke":
                live.pop(op.get("id"), None)
            elif kind == "reset":
                source = op.get("source")
                for rid in [k for k, v in live.items() if v.get("type") == source]:
                    del live[rid]
    tmp = feed.with_name(feed.name + ".compact")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for op in live.values():
            fh.write(json.dumps(op, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, feed)
    return {"lines_before": lines, "records": len(live)}


class NotModified(Exception):
    """The hub answered 304: the mirror is current."""


def tick(get, feed: Path, *, now: float | None = None, min_interval: float = MIN_INTERVAL_S,
         max_pages: int = MAX_PAGES_PER_PASS, compact_bytes: int = COMPACT_BYTES) -> dict:
    """One mirror pass. `get(route, headers)` returns the decoded JSON page or raises
    NotModified. Returns what happened; the sidecar records it for the local engine."""
    now = time.time() if now is None else now
    state = read_state(feed)
    due = BOOTSTRAP_INTERVAL_S if state.get("more") else min_interval
    polled_at = state.get("polled_at")
    if polled_at is not None and now - float(polled_at or 0) < due:
        _write_state(feed, state, now)
        return {"status": "skipped", "next_in_s": int(due - (now - float(polled_at or 0)))}
    cursor = str(state.get("cursor") or "")
    etag = str(state.get("etag") or "")
    if cursor and not feed.exists():
        cursor, etag = "", ""
    written, pages, more = 0, 0, False
    try:
        while pages < max(1, int(max_pages)):
            # Every carrier: a proxy on the path can drop If-None-Match.
            headers = {"If-None-Match": etag, "X-Hub-ETag": etag} if (etag and cursor) else {}
            route = "%s?cursor=%s&limit=%d" % (ROUTE, _quote(cursor), PAGE_LIMIT)
            try:
                body = get(route, headers) or {}
            except NotModified:
                break
            pages += 1
            items = list(body.get("items") or [])
            written += _append(feed, items)
            # Durable first, cursor second: see the module docstring.
            cursor = str(body.get("cursor") or cursor)
            more = bool(body.get("more"))
            # A caught-up page names its ETag in the body too; sent back next pass, it turns
            # "nothing new" into a 304 for a transport that returns bodies only.
            etag = "" if items else str(body.get("etag") or etag)
            if not more:
                break
        status = "bootstrapping" if more else ("updated" if written else "current")
        state.update(cursor=cursor, etag=etag, more=more, polled_at=now, ok_at=now, error="",
                     written=int(state.get("written") or 0) + written, pages=pages)
    except Exception as exc:                            # noqa: BLE001 - a mirror never stops its caller
        state.update(cursor=cursor, polled_at=now, more=False,
                     error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        _write_state(feed, state, now)
        return {"status": "error", "error": state["error"], "feed": str(feed)}
    try:
        if feed.exists() and feed.stat().st_size > compact_bytes:
            state["compacted"] = dict(compact(feed), at=now)
    except OSError as exc:
        state["compact_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    _write_state(feed, state, now)
    return {"status": status, "cursor": cursor, "written": written, "pages": pages,
            "more": more, "feed": str(feed)}


def _quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


# ── the hand-off decision, and the health line a machine reports ──

def feed_state(feed: Path | None = None, *, now: float | None = None) -> str:
    """fresh | stale | absent -- the ONE definition of "the local mirror is fresh"."""
    feed = feed or default_feed()
    st = read_state(feed)
    if not st:
        return "absent"
    now = time.time() if now is None else now
    ok = (not st.get("error")) and st.get("more") is False \
        and st.get("ok_at") is not None and now - float(st.get("ok_at") or 0) <= FRESH_S
    return "fresh" if ok else "stale"


def _health_doc() -> dict | None:
    path = os.environ.get("HUB_LOCAL_MEMORY_HEALTH", "").strip()
    if not path:
        return None
    try:
        doc = json.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def local_owner(feed: Path | None = None, *, now: float | None = None) -> bool:
    """True when LOCAL memory, not the hub block, serves this prompt's knowledge: the switch is
    on, the mirror is fresh, and local recall is healthy. Fails CLOSED: any doubt is False."""
    try:
        if not switch_on() or feed_state(feed, now=now) != "fresh":
            return False
        doc = _health_doc()
        if not doc or doc.get("ts") is None:
            return False
        now = time.time() if now is None else now
        return (str(doc.get("retrieval_mode") or "").lower() in healthy_modes()
                and now - float(doc["ts"]) <= HEALTHY_S)
    except Exception:                                   # noqa: BLE001
        return False


def health_line(feed: Path | None = None, *, now: float | None = None) -> str:
    """`mode=<last recall's retrieval mode>;age=<s since it>;feed=fresh|stale|absent[;hw_*]` for
    the X-Hub-Memory-Health header, or "" when this machine reports no local memory at all
    (neither a health file nor a mirror) -- an optional layer that is absent is not a fault.
    The optional `hw` block ({cores, ram_gb, gpu}) the engine may record says what a model
    choice for local retrieval has to run on, so that choice rests on the machines it will
    run on, not the one it was measured on."""
    now = time.time() if now is None else now
    doc = _health_doc()
    fs = feed_state(feed, now=now)
    if doc is None and fs == "absent":
        return ""
    parts = []
    if doc and doc.get("ts") is not None:
        parts.append("mode=%s" % re.sub(r"[^a-z_-]", "",
                                        str(doc.get("retrieval_mode") or "unknown").lower())[:20])
        try:
            parts.append("age=%d" % max(0, int(now - float(doc["ts"]))))
        except (TypeError, ValueError):
            pass
        hw = doc.get("hw") if isinstance(doc.get("hw"), dict) else {}
        if hw:
            try:
                parts.append("hw_cores=%d" % int(hw.get("cores") or 0))
                parts.append("hw_ram_gb=%d" % int(hw.get("ram_gb") or 0))
            except (TypeError, ValueError):
                pass
            gpu = re.sub(r"[^A-Za-z0-9 ._-]", "", str(hw.get("gpu") or "none"))[:48]
            parts.append("hw_gpu=%s" % (gpu.replace(" ", "_") or "none"))
    else:
        parts.append("mode=none")
    parts.append("feed=%s" % fs)
    return ";".join(parts)


def parse_health(raw: str) -> dict:
    out = {}
    for part in str(raw or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip():
            out[k.strip()[:20]] = v.strip()[:60]
    return out


def grade_health(raw: str, *, modes: set | None = None) -> tuple:
    """(grade, detail) for one machine's reported line: `current`, or `stale` when its recent
    recalls ran a degraded mode. None when nothing was reported -- local memory is optional, so
    silence is not graded."""
    health = parse_health(raw)
    if not health:
        return None, {}
    mode = health.get("mode", "")
    try:
        age = int(health.get("age") or 0)
    except ValueError:
        age = 0
    if mode and mode not in ("none", "unknown") and mode not in (modes or healthy_modes()) \
            and age <= DEGRADED_WINDOW_S:
        return "stale", dict(health, reason="local recall is running %s" % mode)
    return "current", health
