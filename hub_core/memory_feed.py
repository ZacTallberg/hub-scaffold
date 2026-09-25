"""The hand-off between the board's knowledge and a machine's own memory engine — the CONSUMER half.

A board serves everything it knows as an append-only feed (``GET /hub/knowledge/since``). A
workstation may keep a local copy and index it with its own memory engine (hybrid search, a
reranker, whatever it runs), so ranking happens at home, at zero network cost and with the board
down. ``patterns/agent-memory.md`` is the write-up; this module is the part of the contract both
sides must compute identically, so neither re-derives it:

  * WHAT A MIRROR HOLDS. ``live_records()`` applies the ops the way every consumer must: newest op
    per id wins, a ``revoke`` DELETES the record (an index that keeps serving a retired rule scores
    below having no memory at all), a ``reset`` drops one record type before its re-send, and a
    torn final line (a write in flight) waits for the next read. It reads either shape a mirror
    takes here: a JSONL op log (``<feeds dir>/<feed>.jsonl``) or the single JSON document
    ``client knowledge-sync --out`` writes.
  * WHO DELIVERS IT BEFORE EACH PROMPT. Two channels (the board's per-prompt block and local
    recall) sending the same knowledge double the tokens; neither sending it starves the prompt.
    ONE decider writes ``owns`` + ``decided_at`` into ``<feed>.state.json`` beside the feed and
    both sides obey that bit (``owner()``). Measured on the system this was lifted from: when
    each side ran its own freshness test, 36 of 218 prompts got the knowledge twice and 24 got
    it from neither. A decision older than DECISION_FRESH_S means the decider stopped writing,
    and both sides then fall back to the board's block — disagreement costs duplication, never a
    gap.
  * WHEN THE MIRROR COUNTS AS CURRENT. ``mirror_fresh()``: its last CAUGHT-UP poll (``more``
    false) within OWNER_FRESH_S. Knowledge is not live state — a lesson written an hour ago is as
    true now — so a later FAILED poll does not disqualify the mirror. Requiring every poll to
    succeed handed delivery back to the board exactly while the board was slow, which is when
    local delivery matters most.
  * WHAT TEXT IS INDEXED. ``index_text()``: the title only when it adds to the rule (most titles
    are the rule cut short, and indexing both counts the same words twice), then WHY, date and
    check — REDACTED. Agents paste credentials into records (a token in a remote URL, a service
    password in a diagnosis); a mirror is a second copy on disk that a backup's secret scan will
    refuse. The typed marker stays, so a reader still sees that a secret was there. The board's
    ``text_sha`` keys the record, so redaction never causes a re-put.
  * WHAT CHANGED. ``keys()``: the text hash AND a metadata hash (date, check, status). A record
    whose text is unchanged but whose check moved must update its label in place — re-embedding
    it wastes work, and skipping it means a new check never reaches recall.
  * HOW IT IS SHOWN. ``rule_form()``: the statement alone, cut at RULE_CHARS, the id kept so the
    reader can pull the story. Measured on 396 real answered questions: six rule-only records
    answered 57.3% where three full records (title, rule and story) answered 48.7%, in the same
    characters.

Standard library only. Nothing here fetches: the producer (``client knowledge-sync``, or a
workstation daemon) writes the files, and this module only reads them.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import secretscan

#: The agent-neutral tree a producer writes and any memory engine reads (one feed per file).
DEFAULT_FEEDS_DIR = "~/.agent-memory/feeds"
STATE_SUFFIX = ".state.json"
#: The mirror counts as current for this long after its last caught-up poll.
OWNER_FRESH_S = 6 * 3600
#: The decider's own decision stays valid this long; the producer rewrites the sidecar every
#: heartbeat (~5 min), so this is three missed ticks.
DECISION_FRESH_S = 15 * 60
#: Local recall counts as healthy when its last recall ran with its full pipeline this recently.
RECALL_HEALTHY_S = 15 * 60
RULE_CHARS = 300


def feeds_dir() -> Path:
    """``HUB_MEMORY_FEEDS_DIR``, else ``~/.agent-memory/feeds`` — resolved by Python, never through
    a shell variable that may point at a network share."""
    raw = os.environ.get("HUB_MEMORY_FEEDS_DIR", "").strip()
    return Path(os.path.expanduser(raw or DEFAULT_FEEDS_DIR))


# ── what a mirror holds ──

def _ops_from_lines(lines):
    for raw in lines:
        if isinstance(raw, bytes):
            if not raw.endswith(b"\n"):
                break                                  # a torn tail is a write in flight
            raw = raw.decode("utf-8", "replace")
        elif not raw.endswith("\n"):
            break
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


def apply_ops(ops, live: dict | None = None) -> dict:
    """Fold put / revoke / reset ops into ``{id: record}`` — newest op per id wins."""
    live = {} if live is None else live
    for item in ops:
        op = item.get("op")
        if op == "reset":
            source = str(item.get("source") or "")
            for rid in [k for k, v in live.items() if not source or v.get("type") == source]:
                live.pop(rid, None)
            continue
        rid = str(item.get("id") or "")
        if not rid:
            continue
        if op == "revoke":
            live.pop(rid, None)
        elif op == "put":
            live.pop(rid, None)                        # re-insert: latest put, latest position
            live[rid] = {k: v for k, v in item.items() if k != "op"}
    return live


def live_records(path) -> dict:
    """The live records of one mirror file, either shape. A missing file is an empty mirror."""
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(1)
            fh.seek(0)
            if head == b"{":
                # A JSON mirror is one document; a JSONL feed's first line is also an object, so
                # tell them apart by whether the WHOLE file parses as one.
                raw = fh.read()
                try:
                    doc = json.loads(raw.decode("utf-8-sig"))
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and isinstance(doc.get("records"), dict):
                    return {str(k): v for k, v in doc["records"].items() if isinstance(v, dict)}
                if isinstance(doc, dict) and doc.get("op"):
                    return apply_ops([doc])
                return apply_ops(_ops_from_lines(raw.splitlines(keepends=True)))
            return apply_ops(_ops_from_lines(fh))
    except OSError:
        return {}


# ── who delivers it ──

def read_sidecar(feed_file) -> dict | None:
    side = Path(feed_file)
    if not side.name.endswith(STATE_SUFFIX):
        side = side.with_name(side.name.rsplit(".", 1)[0] + STATE_SUFFIX)
    try:
        doc = json.loads(side.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def mirror_fresh(state: dict | None, now: float | None = None) -> bool:
    """The ONE definition of a current mirror: caught up (``more`` false) at ``ok_at`` within
    OWNER_FRESH_S. ``ok_at`` is written only by a successful, caught-up poll."""
    now = time.time() if now is None else now
    if not isinstance(state, dict) or state.get("more") is not False:
        return False
    try:
        age = now - float(state.get("ok_at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= OWNER_FRESH_S


def decide(*, switch_on: bool, mirror_state: dict | None, recall_healthy: bool,
           now: float | None = None) -> bool:
    """The decision the ONE decider writes as ``owns``: the operator's switch is on AND the mirror
    is current AND local recall is working. Fails CLOSED — any doubt keeps the board's block."""
    try:
        return bool(switch_on) and mirror_fresh(mirror_state, now) and bool(recall_healthy)
    except Exception:                                   # noqa: BLE001
        return False


def recall_healthy(health: dict | None, last_full_at: float = 0.0, now: float | None = None) -> bool:
    """Did local recall answer with its full pipeline within RECALL_HEALTHY_S?

    ``health`` is the small status record a memory engine rewrites after each hook run
    (``{"retrieval_mode": "hybrid"|"keyword"|..., "ts": epoch}``). A record WITHOUT a mode (a
    session-start map, a skipped short prompt) is not a failed recall: only a recall that ran and
    was not full is. The caller keeps ``last_full_at`` (the newest full-pipeline time it has seen)
    so a mode-less write in between does not hand delivery back."""
    now = time.time() if now is None else now
    h = health if isinstance(health, dict) else {}
    mode = str(h.get("retrieval_mode") or "")
    try:
        ts = float(h.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if mode == "hybrid":
        return 0 <= now - ts <= RECALL_HEALTHY_S
    if mode and mode not in ("skipped", "unknown"):
        return False
    return last_full_at > 0 and 0 <= now - last_full_at <= RECALL_HEALTHY_S


def owner(directory=None, now: float | None = None) -> dict | None:
    """The feed whose delivery belongs to local recall right now, or None (the board delivers).

    Obeys the decider's ``owns`` bit while ``decided_at`` is within DECISION_FRESH_S and runs no
    test of its own. A sidecar from a producer that does not stamp a decision falls back to the
    older rule (``local_owns`` switch AND a current mirror), so the two halves can be upgraded
    in either order. Missing or unreadable = None."""
    now = time.time() if now is None else now
    d = Path(directory) if directory is not None else feeds_dir()
    try:
        sides = sorted(d.glob("*" + STATE_SUFFIX)) if d.exists() else []
    except OSError:
        return None
    for side in sides:
        st = read_sidecar(side)
        if st is None:
            continue
        try:
            ok_at = float(st.get("ok_at") or 0)
        except (TypeError, ValueError):
            ok_at = 0.0
        if "owns" in st:
            try:
                decided_at = float(st.get("decided_at") or 0)
            except (TypeError, ValueError):
                decided_at = 0.0
            owns = st.get("owns") is True and 0 <= now - decided_at <= DECISION_FRESH_S
            basis = "decider"
        else:
            owns = st.get("local_owns") is True and mirror_fresh(st, now)
            basis = "legacy switch + mirror age"
        if owns:
            return {"feed": side.name[: -len(STATE_SUFFIX)], "ok_at": ok_at,
                    "age_s": round(now - ok_at, 1) if ok_at else None, "basis": basis}
    return None


# ── what is indexed, what changed, how it is shown ──

def _redact(text) -> str:
    return secretscan.redact(str(text or ""))[0]


def _stem(title: str) -> str:
    return title.rstrip("…").rstrip(".").rstrip()


def index_text(item: dict) -> str:
    """The text a memory engine indexes for one record — and exactly what a hit shows, so it is
    self-contained. Redacted before it can reach an index or a vector."""
    title = str(item.get("title") or "").strip()
    rule = str(item.get("rule") or "").strip()
    stem = _stem(title)
    if rule and (rule == title or (stem and rule.startswith(stem))):
        parts = [rule]
    else:
        parts = [title] + (["RULE: " + rule] if rule else [])
    why = str(item.get("why") or "").strip()
    if why:
        parts.append("WHY: " + why)
    stamp = item.get("verified_as_of") or item.get("written_at")
    if stamp:
        parts.append("as of " + str(stamp)[:10])
    if item.get("verify"):
        parts.append("check: " + str(item["verify"]))
    return _redact("\n".join(p for p in parts if p))


def keys(item: dict) -> tuple[str, str]:
    """(text_sha, meta_sha). The text hash is the board's own when it sent one (so redacting the
    local copy never triggers a re-put); the metadata hash covers what moves a record's LABEL
    without touching its text — its dates, its check, its status."""
    sha = str(item.get("text_sha") or "")
    if not sha:
        raw = "\x1f".join(str(item.get(k) or "") for k in ("title", "rule", "why"))
        sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    meta = {k: item.get(k) for k in ("status", "verified_as_of", "updated_at", "tier")}
    meta["verify"] = _redact(item.get("verify"))
    meta["recheck"] = item.get("recheck") if isinstance(item.get("recheck"), dict) else None
    meta_sha = hashlib.sha1(json.dumps(meta, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return sha, meta_sha


def rule_form(item: dict, chars: int = RULE_CHARS) -> str:
    """The statement alone: what to do, not the story of how it was learned. The id stays, so
    ``client recall <id>`` fetches the WHY when the rule is not enough."""
    title = str(item.get("title") or "").strip()
    rule = str(item.get("rule") or "").strip()
    stem = _stem(title)
    body = rule if rule and (not title or rule == title or (stem and rule.startswith(stem))) \
        else (title + ((" — " + rule) if rule else ""))
    body = " ".join(_redact(body).split())
    if len(body) > chars:
        body = body[:chars - 1].rstrip() + "…"
    return "- [%s] %s" % (item.get("id") or "?", body)


def full_form(item: dict, chars: int = 2000) -> str:
    text = index_text(item)
    if len(text) > chars:
        text = text[:chars - 1].rstrip() + "…"
    return "- [%s]\n  %s" % (item.get("id") or "?", text.replace("\n", "\n  "))


# ── a status a person can read ──

def status(directory=None, mirror=None, now: float | None = None) -> dict:
    """What a consumer sees right now: every feed with its live/revoked counts and its sidecar
    decision as ``owner()`` reads it, plus the ``knowledge-sync`` mirror when one is named."""
    now = time.time() if now is None else now
    d = Path(directory) if directory is not None else feeds_dir()
    feeds = []
    try:
        files = sorted(p for p in d.glob("*.jsonl") if p.is_file()) if d.exists() else []
    except OSError:
        files = []
    for f in files:
        lines = puts = revokes = resets = torn = 0
        try:
            with open(f, "rb") as fh:
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        torn += 1
                        break
                    lines += 1
                    op = b'"op":"put"' in raw or b'"op": "put"' in raw
                    puts += op
                    revokes += b'"revoke"' in raw
                    resets += b'"reset"' in raw
        except OSError:
            continue
        side = read_sidecar(f)
        row = {"feed": f.stem, "file": str(f).replace("\\", "/"), "bytes": f.stat().st_size,
               "lines": lines, "puts": puts, "revokes": revokes, "resets": resets,
               "torn_tail": bool(torn), "live": len(live_records(f)),
               "sidecar": None}
        if side is not None:
            ok_at = side.get("ok_at")
            row["sidecar"] = {k: side.get(k) for k in ("owns", "decided_at", "local_owns", "more",
                                                         "error", "ok_at") if k in side}
            row["mirror_fresh"] = mirror_fresh(side, now)
            if isinstance(ok_at, (int, float)) and ok_at:
                row["mirror_age_s"] = round(now - float(ok_at), 1)
            if side.get("decided_at") is not None:
                try:
                    row["decision_age_s"] = round(now - float(side["decided_at"]), 1)
                except (TypeError, ValueError):
                    pass
        feeds.append(row)
    out = {"feeds_dir": str(d).replace("\\", "/"), "feeds": feeds, "owner": owner(d, now)}
    if mirror is not None:
        out["mirror"] = {"file": str(mirror).replace("\\", "/"), "live": len(live_records(mirror)),
                         "exists": Path(mirror).exists()}
    out["delivers_before_each_prompt"] = ("local recall (%s)" % out["owner"]["feed"]) if out["owner"] \
        else "the board's per-prompt block"
    return out
