"""One label for a shared knowledge record: STATE / UNVERIFIED / CHECK FAILED.

A knowledge record reaches an agent through several surfaces -- the per-prompt memory index,
ranked search, the append-only feed a machine mirrors locally -- and every one of them must say
the SAME thing about it. A rule that says "the export endpoint is still down" or "port 8443 serves
the model" decays from the day it is written; presenting it as standing law three weeks later is
how an agent acts confidently on a fact that stopped being true. So this module is the ONLY place
a record's label is decided, and every surface renders it from the same inputs.

    as of 2026-01-04 (12 d ago)                                          a durable rule
    STATE as of 2026-01-04 (12 d ago) · verify before acting: <check>    a state claim with a check
    STATE as of ... · UNVERIFIED — no verify line; check the live surface before acting on it
    ... · CHECK FAILED 2026-01-15 (404): GET <url> -> HTTP 404 — NEEDS REVIEW
    ... · check answered 2026-01-15

WHAT MAKES A RECORD "STATE". A deliberately literal phrase list (STATE_PATTERNS): "still needs",
"is down", "not yet", "awaiting", "measured 2026-01-04", "currently", "port 8443 serves", "latest
version". A false STATE label costs one extra line; a missed one costs a wrong act, so the list
leans inclusive. The judgement reads the record's injected surface (its rule, or its title when it
has none): one phrase there makes it STATE. The story (why) counts only when it is MOSTLY pending
state (three or more distinct phrases) -- a dated narrative legitimately says "pending" about the
day it describes. A dated EVENT ("shipped 2026-01-04", "fixed 2026-01-04") is history and stays
true as written; a dated SNAPSHOT ("measured 2026-01-04") is state.

The heuristic runs once per record VERSION (``is_state_memo``): the per-prompt path must not run
twenty regexes over thousands of records on every read. Age is applied at render time, so a
label never freezes the day it was computed.

The re-check (``hub_core.recheck``) writes ``recheck`` onto a record; a failure makes the label
loud (CHECK FAILED ... NEEDS REVIEW), because a failing check is the one outcome that changes
what a reader should do. Nothing here ever retires a record.

Stdlib only.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

#: A state claim older than this with no check reads as stale.
STALE_DAYS = 7.0

STATE_PATTERNS: tuple = (
    r"\bstill (?:needs?|lacks?|waiting|pending|open|missing|blocked|broken|red|down|un\w+)\b",
    r"\b(?:is|are|remains?|stays?) (?:currently )?(?:down|up|dark|red|green|blocked|offline|online|"
    r"broken|disabled|enabled|active|inactive|pending|open|closed|live|dead|frozen|paused)\b",
    r"\bnot yet\b",
    # A bare "needs"/"requires" describes how an API works ("needs the object id") as often as a
    # pending condition; only the pending forms count, or a need for a human/admin action.
    r"\b(?:still|now|yet to be|remains? to be)\s+(?:needs?|requires?|lacks?|missing|granted|done|"
    r"deployed|routed|fixed|wired|enrolled)\b",
    r"\b(?:awaits?|awaiting|pending|blocked on|waiting (?:on|for)|on hold|deferred until)\b",
    r"\b(?:needs?|requires?) (?:a |an |the |his |her |their |your |my )?(?:tenant|admin|human|manual|"
    r"grant|approval|ticket|restart|redeploy|rebuild|reboot|fix|policy|allowlist|permission|role|"
    r"access|credential|password|token|key|cert|sign-?off|go)\b",
    r"\bOPEN (?:manual )?gate\b",
    # A dated SNAPSHOT is state; a dated EVENT is history.
    r"\b(?:as of|verified|measured|checked|confirmed|live|red|green|down|up|broken|blocked|pending)"
    r" (?:on |since )?\d{4}-\d{2}-\d{2}\b",
    r"\b(?:currently|right now|at the moment|for now|as of (?:today|now|this (?:writing|session)))\b",
    r"\b(?:measured|verified|deployed|shipped|fixed|running|red|green|live|down|up|broken|dark|"
    r"disabled|enabled|stopped|started) (?:as of )?(?:today|tonight|this (?:morning|afternoon|week))\b",
    r"(?:\bport\s?|:)\d{4,5}\b.{0,40}\b(?:serves?|=|is|runs?)\b",
    r"\b(?:no|only) (?:user )?(?:picker|button|route|service|runner|pipeline)\b",
    r"\b(?:has|have) (?:no|not) (?:been )?\w+ed\b",
    r"\b(?:un(?:granted|deployed|routed|verified|assigned|enrolled|configured|installed))\b",
    r"\b(?:latest|current|newest) (?:version|release|build|model)\b",
    r"\bv?\d+\.\d+\.\d+\b.{0,30}\b(?:is|=) (?:current|latest|live|pinned)\b",
)
_STATE_RE = re.compile("|".join(STATE_PATTERNS), re.I)


def parse_datetime(value):
    """An ISO date/datetime (``Z`` accepted) as an aware UTC datetime, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def state_signals(text: str, *, limit: int = 6) -> list:
    """The state-claim phrases found in ``text`` (deduplicated, in order), at most ``limit``."""
    out, seen = [], set()
    for m in _STATE_RE.finditer(text or ""):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip()
        if phrase.casefold() in seen:
            continue
        seen.add(phrase.casefold())
        out.append(phrase)
        if len(out) >= limit:
            break
    return out


def record_is_state(*, title: str = "", rule: str = "", why: str = "") -> bool:
    """Whether a record reads as a STATE claim: one phrase in its injected surface (the rule, or
    the title when there is none), or a story that is mostly pending state."""
    return bool(state_signals(rule or title or "")) or len(state_signals(why or "")) >= 3


_MEMO: dict = {}
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 50000


def is_state_memo(eid, version, *, title: str = "", rule: str = "", why: str = "") -> bool:
    """``record_is_state`` memoized per (id, version): a record's text changes only with its
    version, so the heuristic runs once per edit, not once per read."""
    key = (eid, version)
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None:
        return hit
    value = record_is_state(title=title, rule=rule, why=why)
    with _MEMO_LOCK:
        if len(_MEMO) > _MEMO_MAX:
            _MEMO.clear()
        _MEMO[key] = value
    return value


def recheck_suffix(recheck) -> str:
    """What the re-check of a record's ``verify`` found, or "" when it has not run."""
    if not isinstance(recheck, dict) or not recheck.get("status"):
        return ""
    when = str(recheck.get("checked_at") or "")[:10] or "undated"
    status = str(recheck.get("status"))
    if status == "failed":
        code = recheck.get("http_status") or recheck.get("kind") or "error"
        detail = " ".join(str(recheck.get("detail") or "").split())[:120]
        since = str(recheck.get("since") or "")[:10]
        failing = (" (failing since %s)" % since) if since and since != when else ""
        return " · CHECK FAILED %s (%s)%s: %s — NEEDS REVIEW" % (when, code, failing, detail)
    if status == "answered":
        return " · check answered %s" % when
    return ""


def render_label(*, is_state: bool, verified_as_of=None, updated_at=None, verify=None,
                 recheck=None, now=None, stale_days: float = STALE_DAYS) -> str:
    """The one line every knowledge surface prints for a record. Never empty: every record dates
    itself, by ``verified_as_of`` when it has one, else by its last update."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    asof = parse_datetime(verified_as_of) or parse_datetime(updated_at)
    age = (current - asof).total_seconds() / 86400.0 if asof else None
    when = asof.strftime("%Y-%m-%d") if asof else "undated"
    ago = ("%d d ago" % int(age) if age is not None and age >= 1
           else ("today" if age is not None else "age unknown"))
    verify = " ".join(str(verify or "").split())
    if not is_state:
        label = "as of %s (%s)" % (when, ago)
    elif verify:
        label = "STATE as of %s (%s) · verify before acting: %s" % (when, ago, verify[:200])
    else:
        label = ("STATE as of %s (%s) · UNVERIFIED — no verify line; check the live surface "
                 "before acting on it" % (when, ago))
    return label + recheck_suffix(recheck)


def label_inputs(ent: dict, *, title: str = "", rule: str = "", why: str = "") -> dict:
    """What a label needs, computed once per corpus build (age is applied at render time).
    ``title``/``rule``/``why`` are the texts the surface itself ships for the record."""
    ent = ent or {}
    return {"is_state": is_state_memo(ent.get("id"), ent.get("version"), title=title, rule=rule,
                                      why=why),
            "verified_as_of": ent.get("verified_as_of") or "",
            "updated_at": (ent.get("provenance") or {}).get("updated_at") or "",
            "verify": str(ent.get("verify") or "").strip(),
            "recheck": ent.get("recheck") if isinstance(ent.get("recheck"), dict) else None}


def render_inputs(inputs, now=None) -> str:
    """``render_label`` from ``label_inputs``; "" when there are none."""
    if not inputs:
        return ""
    return render_label(is_state=bool(inputs.get("is_state")),
                        verified_as_of=inputs.get("verified_as_of"),
                        updated_at=inputs.get("updated_at"), verify=inputs.get("verify"),
                        recheck=inputs.get("recheck"), now=now)
