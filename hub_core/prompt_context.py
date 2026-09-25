"""The knowledge block an agent harness injects before each prompt — rendered, fitted, deduplicated.

`GET /hub/guidance.json?focus=` returns the board's knowledge index ranked for what a console
is doing right now, plus a small live block (what is addressed to the agent, how many errors
are unclaimed). This module turns that payload into the text a prompt hook prints. It is the
client half of per-prompt knowledge delivery; `python -m hub_core.client prompt-context` is
the verb a hook calls.

FOUR RULES, each paid for on the instance this was lifted from:

  * SAY WHICH ORDER THIS IS. An index that silently reverts from relevance to recency looks
    exactly like one that ranked well. The header names the order and, when unranked, why.
  * SEND WHAT THE SESSION DOES NOT ALREADY HOLD. Hook output stays in the session's context
    until it is compacted, so re-sending a record injected three prompts ago is pure duplicate.
    A per-session receipt of delivered record keys (id + hash of the text delivered) means
    each prompt carries only new rows; the relevant rows already held are NAMED in one line so
    the ranking still speaks. A session start resets the receipt, because a fresh or compacted
    context holds nothing.
  * WHAT FITS IS WHAT IS DELIVERED. Agent harnesses commonly show hook output inline only up to
    a ceiling and replace anything longer with a short preview — so a 30 KB block delivers its
    first few rows while the receipt marks hundreds as held. Output is fitted under
    OUTPUT_MAX, rows that did not fit are written to a pack file the agent can read, and only
    rows that actually rendered become receipt keys. A cut is never silent.
  * A STATE CLAIM CARRIES ITS DATE AND ITS CHECK. A rule that says "port 8001 serves X" decays
    from the day it is written; rows print the hub's one label for the record (STATE as of ...
    · verify before acting / UNVERIFIED / CHECK FAILED — NEEDS REVIEW, hub_core.staleness), or
    "(as of <date>)" and "check: <command>" from a hub too old to send one.
  * RELEVANCE, NOT A BUDGET. When the hub ranked by MEANING it sends each row's cosine
    (`score`); then only rows at or above RELEVANCE_CUT are delivered, at most RELEVANCE_MAX,
    never padded to fill the room -- on the instance this was lifted from, a few thousand
    records delivered by budget were referred to a handful of times, and the rows past the
    first few added almost nothing to the answers people actually got. A row without a score
    is unranked, not relevant.
  * THE SAME QUESTION GETS NO NEW ANSWERS. A prompt whose focus is the one the session's last
    memory block was ranked for re-ranks the SAME query, so all it could add sits BELOW rows
    the session already holds (most prompts that repeat a focus are notifications and relayed
    messages). It renders no memory rows; a new focus, a session start or a compaction does.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

#: Stay under a typical hook's inline ceiling (10,000 chars) with room for the harness's wrapping.
OUTPUT_MAX = 9500
#: The most the memory block may take of that.
MEMORY_BUDGET = 8000
SPILL_LINE_RESERVE = 260
#: After a session's first injection, only this head of the ranking is considered for new rows,
#: and only the first DELTA_FULL of those carry their full RULE/WHY.
DELTA_RANK_WINDOW = 60
DELTA_FULL_WINDOW = 15
#: Per session, how many delivered keys are remembered (oldest dropped first).
DELIVERED_KEYS_MAX = 4000
#: The relevance floor for a meaning-ranked row, and how many such rows one prompt carries.
#: Measured on real answered questions on the instance this was lifted from; set per board
#: with HUB_PROMPT_RELEVANCE_CUT / HUB_PROMPT_RELEVANCE_MAX.
def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


RELEVANCE_CUT = _env_float("HUB_PROMPT_RELEVANCE_CUT", 0.48)
RELEVANCE_MAX = int(_env_float("HUB_PROMPT_RELEVANCE_MAX", 6))
SPILL_MARK = ("  (%d more records ranked for this prompt did not fit inline and were NOT delivered; "
              "they are in full, in rank order, in the file named below — read it when the task "
              "touches them)")


def state_dir() -> Path:
    """Where receipts and packs live: HUB_CLIENT_STATE_DIR, else ~/.hub-client (resolved by
    Python, never through a shell variable that may point at a network share)."""
    raw = os.environ.get("HUB_CLIENT_STATE_DIR", "").strip()
    return Path(raw) if raw else Path(os.path.expanduser("~")) / ".hub-client"


def _safe(sid) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(sid or ""))[:80] or "no-session"


def memory_key(m) -> str:
    """One record AS DELIVERED: id + hash of the text it carried, so an edited or newly-full
    record counts as new while an unchanged one is never sent twice."""
    text = "%s|%s|%s" % (m.get("title") or "", m.get("rule") or "", m.get("why") or "")
    return "%s#%s" % (m.get("id") or "?", hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10])


# ── the per-session receipt ──

def _receipt_path(sid) -> Path:
    return state_dir() / "receipts" / (_safe(sid) + ".json")


def _load_receipt(sid) -> dict:
    try:
        data = json.loads(_receipt_path(sid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_delivered(sid) -> list:
    keys = _load_receipt(sid).get("keys")
    return [str(k) for k in keys] if isinstance(keys, list) else []


def load_focus(sid) -> str:
    """The focus this session's last memory block was ranked for ('' when none)."""
    return str(_load_receipt(sid).get("memory_focus") or "")


def save_delivered(sid, keys, focus=None) -> None:
    path = _receipt_path(sid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(dict.fromkeys(keys))[-DELIVERED_KEYS_MAX:]
        body = {"keys": keys, "at": time.time()}
        prior = _load_receipt(sid).get("memory_focus")
        if focus is not None or prior:
            body["memory_focus"] = focus if focus is not None else prior
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass                     # a lost receipt costs one duplicate block, never the prompt


def reset_delivered(sid) -> None:
    for path in (_receipt_path(sid), _live_path(sid)):
        try:
            path.unlink()
        except OSError:
            pass


# ── the live block speaks on CHANGE ──
#
# A status line that repeats unchanged on every prompt is read past, and then missed the one
# time it changes. Measured on one fleet's hook output: of ~1,800 status blocks, 39% differed
# from the previous one only in elapsed-time numbers and 11% were byte-identical. So the live
# block is fingerprinted with its AGES masked ("waited 4 h", "oldest 43 min", "7 days") while
# its COUNTS stay (1 -> 2 addressed items is news at once), sent when the fingerprint moves, and
# otherwise repeated at most once per STATUS_REMIND_S as a reminder.

#: An unchanged live block is repeated at most this often within one session.
STATUS_REMIND_S = 30 * 60
_AGE_TOKEN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?"
                        r"|d|days?|w|wk|weeks?)\b")


def _live_path(sid) -> Path:
    return state_dir() / "receipts" / (_safe(sid) + ".live.json")


def live_fingerprint(block: str) -> str:
    return hashlib.sha1(_AGE_TOKEN.sub("<age>", block or "").encode("utf-8", "replace")).hexdigest()[:16]


def live_due(sid, block: str, now: float | None = None) -> bool:
    """True when this session should see `block` now: it changed in substance since the last
    delivery, or the last delivery is older than STATUS_REMIND_S. Records the delivery when
    due. No session id, or an unreadable receipt, means always due -- a lost receipt costs one
    repeated line, never a missed change."""
    if not block:
        return False
    if not sid:
        return True
    now = time.time() if now is None else now
    fp = live_fingerprint(block)
    path = _live_path(sid)
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(prior, dict) and prior.get("fp") == fp
                and now - float(prior.get("at") or 0) < STATUS_REMIND_S):
            return False
    except (OSError, ValueError, TypeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fp": fp, "at": now}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return True


# ── rendering ──

def currency(m) -> str:
    """The row's standing: the hub's ONE label when it sends one (hub_core.staleness -- STATE /
    UNVERIFIED / CHECK FAILED, identical on search and the feed), else the older date/check form
    for a hub that does not."""
    label = " ".join(str((m or {}).get("label") or "").split())
    if label:
        if len(label) > 260:
            # A cut must never drop the one word that changes what the reader does.
            tail = " — NEEDS REVIEW" if label.endswith("NEEDS REVIEW") else ""
            label = label[:257 - len(tail)] + "..." + tail
        return "  (%s)" % label
    asof = str((m or {}).get("verified_as_of") or "")[:10]
    verify = " ".join(str((m or {}).get("verify") or "").split())
    out = ""
    if asof:
        out += "  (as of %s)" % asof
    if verify:
        out += "  check: %s" % (verify if len(verify) <= 90 else verify[:87] + "...")
    return out


def _row(m) -> str:
    mark = "*" if m.get("tier") == "foundational" else ""
    row = "- %s[%s] %s%s" % (mark, m.get("id", "?"), m.get("title", ""), currency(m))
    # A finding's statement IS its title; print the RULE only when it says more than the row.
    if m.get("rule") and " ".join(str(m["rule"]).split()) != " ".join(str(m.get("title") or "").split()):
        row += "\n    RULE: %s" % m["rule"]
    if m.get("why"):
        row += "\n    WHY: %s" % m["why"]
    return row


def relevance_filter(memory, rank, *, cut=None, most=None) -> tuple:
    """``(rows, applied)``: when the ranking is by MEANING and rows carry scores, only rows
    scored at least ``cut``, at most ``most`` of them, in rank order -- never padded. Any other
    ranking is returned unchanged (applied False): a keyword or standing order has no score."""
    cut = RELEVANCE_CUT if cut is None else cut
    most = RELEVANCE_MAX if most is None else most
    rank = rank or {}
    if not rank.get("ranked") or rank.get("by") == "wording":
        return list(memory), False
    if not any(isinstance(m.get("score"), (int, float)) for m in memory):
        return list(memory), False
    kept = [m for m in memory if isinstance(m.get("score"), (int, float)) and m["score"] >= cut]
    return kept[:max(0, int(most))], True


#: When the hub could only rank by WORDS, each matching row carries `lexical_score`: the share
#: of THIS query's own BM25 ceiling it answers (0..1). Read from real focuses, not a round
#: number: the top row must reach LEXICAL_SCORE_MIN, a later row LEXICAL_FOLLOW_MIN AND
#: LEXICAL_FOLLOW_FRAC of the top, at most LEXICAL_RELEVANT_MAX rows — fewer than a meaning-based
#: ranking would get, because a shared word is weaker evidence of relevance than a close meaning.
LEXICAL_SCORE_MIN = 0.25
LEXICAL_FOLLOW_MIN = 0.24
LEXICAL_FOLLOW_FRAC = 0.6
LEXICAL_RELEVANT_MAX = 4


def lexical_cut(memory) -> tuple:
    """(rows that clear the word-match cut, how many scored rows were considered)."""
    scored = [m for m in memory if isinstance(m.get("lexical_score"), (int, float))]
    scored.sort(key=lambda m: -float(m["lexical_score"]))
    if not scored or float(scored[0]["lexical_score"]) < LEXICAL_SCORE_MIN:
        return [], len(scored)
    top = float(scored[0]["lexical_score"])
    follow = max(LEXICAL_FOLLOW_MIN, LEXICAL_FOLLOW_FRAC * top)
    kept = [scored[0]] + [m for m in scored[1:] if float(m["lexical_score"]) >= follow]
    return kept[:LEXICAL_RELEVANT_MAX], len(scored)


def render(payload, *, delivered=None, budget=MEMORY_BUDGET, client_hint="python -m hub_core.client"):
    """`{"memory", "live", "keys", "spill"}` — the memory and live blocks (each "" when silent),
    the receipt keys of rows that actually rendered, and the rendered rows that did not fit."""
    memory = list(payload.get("memory") or [])
    rank = payload.get("memory_rank") or {}
    word_cut = None
    if rank.get("ranked") and rank.get("by") == "wording" and any(
            isinstance(m.get("lexical_score"), (int, float)) for m in memory):
        # WORD MATCHES ARE CUT, NOT PADDED. A standing-order tail under a "ranked" header reads
        # as relevance it is not; a prompt that got nothing because no record matched well
        # enough must SAY so rather than look like one where nothing applied.
        memory, considered = lexical_cut(memory)
        word_cut = {"kept": len(memory), "considered": considered}
    # MEANING-ranked rows are cut the same way, by their absolute score (a ranking by words is
    # left to the word cut above: relevance_filter returns it unchanged).
    memory, cut_applied = relevance_filter(memory, rank)
    already, fresh = [], []
    if delivered:
        memory = memory[:DELTA_RANK_WINDOW]
        memory = [m if i < DELTA_FULL_WINDOW else {k: v for k, v in m.items() if k not in ("rule", "why")}
                  for i, m in enumerate(memory)]
        held = set(delivered)
        # A TITLE-ONLY row is a pointer, and a pointer to a record this session already holds
        # (in full or as a title) says nothing new. Keyed on text alone, a record delivered in
        # full on one prompt and ranked into the title band on the next hashed differently and
        # was re-sent as a bare title every time the focus moved.
        held_ids = {str(k).rsplit("#", 1)[0] for k in held}
        for m in memory:
            pointer = not m.get("rule") and not m.get("why")
            seen = memory_key(m) in held or (pointer and str(m.get("id")) in held_ids)
            (already if seen else fresh).append(m)
    else:
        fresh = memory
    memory_block, kept_keys, spill = "", [], []
    if word_cut is not None and not word_cut["kept"]:
        memory_block = ("<hub-knowledge>\nBOARD KNOWLEDGE was NOT delivered for this prompt: it could "
                        "only be ranked by WORDS (%s) and no record matched them strongly enough "
                        "(%d word match(es) considered). Search it yourself: %s search \"<symptom>\"\n"
                        "</hub-knowledge>" % (re.sub(r"[<>]", "", str(
                            rank.get("semantic_reason") or rank.get("reason")
                            or "no meaning-based ranking"))[:160],
                                              word_cut["considered"], client_hint))
    if fresh or already:
        if rank.get("ranked") and rank.get("by") == "wording":
            order = ("ranked by the WORDS of what this console is doing, not their meaning — "
                     "a record phrased differently sits lower")
            if word_cut is not None:
                order += ("; only the %d that matched them strongly are shown — check each applies"
                          % word_cut["kept"])
        elif rank.get("ranked") and cut_applied:
            order = ("ranked for what this console is doing; only rows scored %.2f or higher, "
                     "at most %d" % (RELEVANCE_CUT, RELEVANCE_MAX))
        elif rank.get("ranked"):
            order = "ranked for what this console is doing"
        else:
            order = "in STANDING ORDER, not ranked (%s)" % (rank.get("reason") or "no ranking")
        head = ["<hub-knowledge>",
                "BOARD KNOWLEDGE (%s records; %s; the top ones carry their RULE and WHY, the rest "
                "are titles — pull any in full:" % (payload.get("memory_total") or len(memory), order),
                "  %s recall <id>   ·   ranked search: %s search \"<symptom>\")" % (client_hint, client_hint)]
        tail = ['  ("*" = foundational. "as of" is the day a claim was last verified; STATE marks a '
                'claim that decays (its "verify before acting" check answers it NOW; UNVERIFIED '
                'means it has none); CHECK FAILED means the re-check of that check failed — '
                'NEEDS REVIEW.)']
        if already:
            tail.append("  Also relevant to THIS prompt and already in your context from earlier in "
                        "this session (not repeated): %s" % ", ".join(str(m.get("id")) for m in already[:20])
                        + (" (+%d more)" % (len(already) - 20) if len(already) > 20 else ""))
        tail.append("</hub-knowledge>")
        room = budget - sum(len(x) + 1 for x in head + tail) - SPILL_LINE_RESERVE
        kept, used = [], 0
        for m in fresh:
            row = _row(m)
            if not spill and used + len(row) + 1 <= room:
                kept.append(row)
                kept_keys.append(memory_key(m))
                used += len(row) + 1
            else:
                spill.append(row)        # rank order holds: nothing lower jumps a row that did not fit
        if kept or already:
            lines = head + kept + ([SPILL_MARK % len(spill)] if spill else []) + tail
            memory_block = "\n".join(lines)
    live = payload.get("live") or {}
    live_lines = []
    inbox = live.get("inbox") or {}
    if inbox.get("count"):
        live_lines.append("ADDRESSED TO YOU (%d): %s — read with: %s inbox --agent <you>" % (
            inbox["count"], "; ".join("%s %s" % (i.get("kind") or "", (i.get("title") or "")[:80])
                                      for i in (inbox.get("items") or [])[:3]), client_hint))
    if live.get("errors_unclaimed"):
        live_lines.append("UNCLAIMED OPERATIONAL ERRORS: %d on the board (errors.json) — claim before you dig."
                          % live["errors_unclaimed"])
    if live.get("error"):
        live_lines.append("(the live block could not be read: %s)" % live["error"])
    live_block = "\n".join(["<hub-live>"] + live_lines + ["</hub-live>"]) if live_lines else ""
    return {"memory": memory_block, "live": live_block, "keys": kept_keys, "spill": spill}


def write_spill(sid, rows) -> str:
    """Rows that did not fit inline, written where the agent can read them; the path, or ""."""
    if not rows:
        return ""
    try:
        d = state_dir() / "packs"
        d.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1("\n".join(rows).encode("utf-8", "replace")).hexdigest()[:8]
        path = d / ("%s-%s.md" % (_safe(sid), digest))
        cutoff = time.time() - 86400
        for old in d.glob("*.md"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text("# Board knowledge ranked for this prompt (%s); did not fit inline.\n\n%s\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%S"), "\n".join(rows)), encoding="utf-8")
        os.replace(tmp, path)
        return str(path).replace("\\", "/")
    except OSError:
        return ""


def fit_output(parts, limit=OUTPUT_MAX) -> str:
    """Join blocks in order and hold the total under `limit`; a block that would cross the line
    is cut at a line boundary WITH a marker. Never a silent truncation."""
    out, used = [], 0
    parts = [p for p in parts if p]
    for i, p in enumerate(parts):
        need = len(p) + (1 if out else 0)
        if used + need <= limit:
            out.append(p)
            used += need
            continue
        room = limit - used - 160
        if room > 400:
            cut = p[:room]
            cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
            out.append(cut + "\n  (cut here to stay under the hook's inline ceiling)")
        else:
            out.append("(%d more chars of board context were withheld to stay under the hook's "
                       "inline ceiling)" % sum(len(x) for x in parts[i:]))
        break
    return "\n".join(out)
