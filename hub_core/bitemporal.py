"""Bi-temporal validity over the ledger: valid-time + transaction-time + superseded_by
(ADR-0009 D — Zep/Graphiti temporal-knowledge-graph SEMANTICS as a pure ledger projection,
not a graph-DB deploy). Every answer is RECONSTRUCTED from events: as_of replays exactly the
prefix of the ledger the queried moment could see, so history stays queryable forever without
any mutable state — the append-only chain IS the time machine.

Two axes per fact (note / lesson / finding):
  transaction-time — when the LEDGER learned each version: the recording event's ts/seq/git_sha.
  valid-time       — when the fact HELD in the world: explicit valid_from/valid_to payload
                     fields when the author states them, else the transaction boundaries
                     (recorded = starts holding; retired = stops holding).

Retirement mirrors the forgetting authority (hub_app._mem_forget_mode): an explicit `forget`
(str or {mode}) or a bare `superseded_by` retires a fact, and a note's own vocabulary
status=="superseded" retires it the same way. A retired fact is invisible to any as_of at or
after its retiring transaction but visible to every earlier one. The interval table keeps
every row — purged included, flagged — because the hash chain cannot unlearn history; purge
governs RETRIEVAL surfaces (hub_app.retrievable_memory), not the audit record.
"""
import re
import time

FACT_TYPES = ("note", "lesson", "finding")

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")


def _type_of(entity_id):
    parts = str(entity_id or "").split(":")
    return parts[1] if len(parts) >= 2 else "?"


def _first_ref(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _forget_mode(snapshot):
    f = snapshot.get("forget")
    if isinstance(f, dict):
        f = f.get("mode")
    if f in ("purge", "release", "supersede"):
        return f
    if snapshot.get("superseded_by"):
        return "supersede"
    if snapshot.get("status") == "superseded":
        return "supersede"
    return None


def _norm_ts(ts):
    """Normalize to a lexicographically comparable UTC ISO string (the ledger's own format)."""
    s = str(ts).strip().replace(" ", "T")
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def resolve_as_of(events, at):
    """Resolve an as_of anchor to (cut_ts, cut_seq|None).

    Anchors, disambiguated deterministically (never by similarity):
      - int/float            -> epoch seconds (UTC)
      - ISO-ish date/datetime -> transaction-time cutoff by ts
      - hex sha (7-40 chars) -> the LAST event recorded under that git_sha (prefix-matched
                                either way: the ledger stamps 12-hex); pins BOTH ts and seq —
                                'the ledger as that build knew it'
    An unresolvable anchor raises ValueError — a point-in-time query must never guess.
    """
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)), None
    s = str(at).strip()
    low = s.lower()
    if _SHA_RE.fullmatch(low):
        hits = [ev for ev in events
                if ev.get("git_sha") and (str(ev["git_sha"]).startswith(low)
                                          or low.startswith(str(ev["git_sha"])))]
        if hits:
            last = max(hits, key=lambda ev: ev.get("seq") or 0)
            return _norm_ts(last.get("ts") or ""), last.get("seq")
        if not low.isdigit():
            raise ValueError(f"as_of sha {s!r} matches no ledger event")
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(s))), None
    if _ISO_RE.match(s):
        return _norm_ts(s), None
    raise ValueError(f"as_of anchor {s!r} is neither a timestamp, epoch, nor a known sha")


def project(events, *, types=FACT_TYPES):
    """The full bi-temporal interval table: one row per fact aggregate, seq-ordered replay.

    Row: {id, type, entity, tx_from, tx_last, retired_tx, valid_from, valid_to,
          superseded_by, forget}. tx_* are {ts, seq, git_sha}. valid_from/valid_to prefer the
    author's explicit payload fields; otherwise the recording / retiring transaction bounds.
    A later event clearing the retirement (dispute resolved) reopens the window.
    """
    rows = {}
    for ev in sorted(events, key=lambda e: e.get("seq") or 0):
        agg = ev.get("aggregate")
        if not agg or _type_of(agg) not in types:
            continue
        tx = {"ts": _norm_ts(ev.get("ts") or ""), "seq": ev.get("seq"),
              "git_sha": ev.get("git_sha")}
        row = rows.get(agg)
        if row is None:
            row = rows[agg] = {"id": agg, "type": _type_of(agg), "entity": {},
                               "tx_from": tx, "tx_last": tx, "retired_tx": None,
                               "valid_from": None, "valid_to": None,
                               "superseded_by": None, "forget": None}
        row["entity"].update(ev.get("payload") or {})
        row["tx_last"] = tx
        mode = _forget_mode(row["entity"])
        if mode:
            if row["retired_tx"] is None:
                row["retired_tx"] = tx
            row["forget"] = mode
            row["superseded_by"] = _first_ref(row["entity"].get("superseded_by"))
        else:
            row["retired_tx"] = None
            row["forget"] = None
            row["superseded_by"] = None
    for row in rows.values():
        explicit_from = row["entity"].get("valid_from")
        explicit_to = row["entity"].get("valid_to")
        row["valid_from"] = _norm_ts(explicit_from) if explicit_from else row["tx_from"]["ts"]
        row["valid_to"] = (_norm_ts(explicit_to) if explicit_to
                           else (row["retired_tx"]["ts"] if row["retired_tx"] else None))
    return sorted(rows.values(), key=lambda r: r["id"])


def as_of(events, at, *, types=FACT_TYPES):
    """The facts VALID at `at`: replay only the events the moment could see, then keep rows
    whose validity window contains the moment and that were still authoritative there (not
    superseded / released / purged by any event at or before the cutoff)."""
    cut_ts, cut_seq = resolve_as_of(events, at)
    if cut_seq is not None:
        seen = [ev for ev in events if (ev.get("seq") or 0) <= cut_seq]
    else:
        seen = [ev for ev in events if _norm_ts(ev.get("ts") or "") <= cut_ts]
    out = []
    for row in project(seen, types=types):
        if row["forget"]:
            continue
        if row["valid_from"] and cut_ts < row["valid_from"]:
            continue
        if row["valid_to"] and row["valid_to"] <= cut_ts:
            continue
        out.append(row)
    return out
