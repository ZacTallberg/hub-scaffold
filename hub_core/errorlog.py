"""Bounded, redacted operational error telemetry — the board's answer to "is anything
broken?", covering surfaces the audit cannot see (a served 500, a browser failure, a
satellite service's exception, a worker whose tooling died).

Framework-free; every function takes the hub dir explicitly. This is observed runtime
state, not project truth, so it lives beside presence/ and claims/ in HUB_DIR, never in the
append-only ledger. The store is deliberately small and fail-soft: error reporting must
never become the reason a request fails.

Rules this module holds, each paid for in production on the origin system:

* REDACT AT THE DOOR. Messages routinely contain pasted command output; secrets are
  stripped before the row exists.
* A REPEATING ERROR IS ONE FACT. Per-fingerprint throttling with a preserved occurrence
  count — an unthrottled flood does not just add noise, it EVICTS every other error from a
  bounded store.
* THE THROTTLE IS AS STRONG AS THE WHOLE SERVICE, NOT ONE WORKER. Its state lives in a
  locked sidecar, so the dedup holds across worker processes and restarts — and it fails
  OPEN: a throttle that malfunctions writes a duplicate, it never hides a row.
* A PER-OCCURRENCE ID IS NOT A NEW FAILURE. The fingerprint is taken over the NORMALIZED
  message (uuids, ids, counts, shas, quoted values, a forwarder's "[+N more]" suffix
  stripped), so one cause is one throttle bucket, one ack identity and one problem.
* CLASSIFY EXTERNAL NOISE BEFORE THE THROTTLE. Foreign-client traffic (a scanner's bad
  Host header) is recorded as a warning and flagged external, so a suppressed repeat
  reports the same severity its written sibling did and can never crowd the top of the
  stream.
* A CHANGE OF SEVERITY IS A NEW FACT. The throttle folds a repeat into the prior write only
  at the SAME severity: a service that demotes a class (error -> warning) gets a row at the
  new severity instead of lending its occurrences to the old error row.
* AN ACK IS BOUNDED BY ITS OWN TIME. It covers the occurrences at or before the moment it was
  given; a recurrence after it is a new event nobody accepted and surfaces again. is_acked()
  is the one predicate every reader shares, and it fails OPEN toward visible.
* A TRACE IS KEPT FOR ITS END. Details keep a head AND a tail — the exception line and the
  frame that raised live at the bottom — and the cause line rides on the row itself.
* ACK COLLAPSES, NEVER DELETES; a clear is bounded by AGE or by ACK, never "everything" —
  a clear must never be the operation that destroys evidence of a failure nobody has
  looked at. only_acked is a RESTRICTION: an age bound beside it narrows the acked set.
* AN EMPTY LIST MUST SAY WHETHER IT IS EVERYTHING. coverage() names each channel that CAN
  report and whether it is live, quiet (reported before) or has NEVER reported — never and
  quiet are different facts and must not share a word — plus what is out of scope.
* A BROKEN STORE MUST NOT READ AS A QUIET ONE. A failed write marks the read surface
  impaired instead of silently returning fewer rows.
* THE BAR IS APPLIED AT READ. passes_bar() is the one predicate every consumer shares;
  improving it reclassifies the whole retained window retroactively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import atomic
from .process_lock import ProcessFileLock

MAX_BYTES = 2 * 1024 * 1024
KEEP_ROWS = 600
READ_LIMIT = 120
THROTTLE_S = 900
_LAST_SEEN_MAX = 2000
MESSAGE_LIMIT = 4000
CONTEXT_LIMIT = 1200
# A real traceback is 1-4 KB; a forwarder may send up to this much. Past it the store keeps
# the HEAD (where the call started) and the TAIL (where it raised) and says what it dropped,
# so a spliced trace is never read as a whole one.
DETAILS_LIMIT = 32000
DETAILS_HEAD = 4000
_SEEN_WRITE_MIN_S = 60          # a durable "seen" stamp is refreshed at most once a minute
_SOURCES_MAX = 400

# Process-local, keyed per hub dir so two boards in one process (a test hub beside the real
# one) never share impairment flags.
_WRITE_FAILURE: dict = {}
_STAMP_FAILED_AT: dict = {}

_SECRET_PAIR = re.compile(
    r"(?i)\b(authorization|password|passwd|secret|token|cookie|api[-_ ]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL_SECRET = re.compile(r"(?i)([?&](?:k|key|token|secret|code)=)[^&#\s]+")

# Sources that are not THIS application failing: an external client sending a Host header
# we do not serve is a scanner or a stale bookmark, worth counting and worth never letting
# near the top of the stream.
_EXTERNAL_NOISE = ("django.security.disallowedhost", "django.security.suspiciousoperation")

_SEVERITY_MAP = {"info": "info", "warning": "warning", "warn": "warning", "error": "error",
                 "high": "error", "critical": "critical", "fatal": "critical"}


def _errors_path(hub_dir) -> Path:
    return Path(hub_dir) / "errors.jsonl"


def _acked_path(hub_dir) -> Path:
    return Path(hub_dir) / "errors-acked.json"


def _seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "errors-seen.json"


def _channel_seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "channel-seen.json"


def _sources_seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "sources-seen.json"


def _clean(value, limit=MESSAGE_LIMIT) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").strip()
    text = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _URL_SECRET.sub(r"\1[REDACTED]", text)
    return text if limit is None else text[:limit]


def _clean_details(value, limit=DETAILS_LIMIT) -> str:
    """Redact FIRST, then cut — in the middle. A head-first cut keeps a traceback's banner
    and throws away the exception it exists to show."""
    text = _clean(value, limit=None)
    if len(text) <= limit:
        return text
    tail = limit - DETAILS_HEAD
    return "%s\n... %d characters elided ...\n%s" % (
        text[:DETAILS_HEAD], len(text) - DETAILS_HEAD - tail, text[-tail:])


# Lines that are never the cause: the banner, the chaining sentences, frame headers and the
# caret art under them.
_TRACE_NOISE = ("Traceback (most recent", "The above exception", "During handling",
                'File "', "^", "~", "|")


def cause_of(details) -> str:
    """The line a reader needs FIRST, lifted out of a trace so a queue row can name the
    failure without a second call: a traceback ends on "ExceptionType: message"."""
    for line in reversed(str(details or "").splitlines()):
        line = line.strip()
        if line and not line.startswith(_TRACE_NOISE):
            return _clean(line, 600)
    return ""


def _note(value, limit=4000) -> str:
    """A claim/resolve note, redacted like every other field but kept WHOLE up to a generous
    bound -- and past it, clipped visibly. At 240 characters with no marker the root cause a
    person wrote when they claimed a problem lost its second half without a trace."""
    text = _clean(value, 10 ** 6)
    if len(text) <= limit:
        return text
    return text[:limit] + " … [clipped: %d of %d characters]" % (limit, len(text))


def _context(value) -> dict:
    if not isinstance(value, dict):
        return {}
    # "machine", "app" and "agent" are the three things a reader most needs: a row that
    # says WHAT broke and nothing about WHERE makes a worker's failure and a service's
    # failure look identical. The CI keys (project, ref, sha, job, jobs, trigger, restored_sha)
    # are what a problem fold groups on and what a later green must match: dropped here, a
    # success on one branch retired a failure on another; `source`/`actor` name who triggered a branch run; `line`/`col` place
    # a browser failure in the board's own same-origin script.
    allowed = ("component", "method", "path", "status", "reason", "operation", "release",
               "machine", "app", "agent", "url", "line", "col",
               "project", "ref", "sha", "job", "jobs", "kind", "source", "actor",
               "trigger", "restored_sha")
    out = {}
    for key in allowed:
        v = value.get(key)
        if v in (None, "", [], ()):
            continue
        if isinstance(v, (list, tuple)):
            out[key] = [_clean(x, 300) for x in v if str(x or "").strip()][:32]
        else:
            out[key] = _clean(v, CONTEXT_LIMIT)
    return out


def _write_json(path: Path, data) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _epoch(value) -> float:
    """An ISO-8601 (…Z) or epoch-ish value as a float, or -inf if unparseable."""
    if value in (None, ""):
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return float("-inf")


# ── identities: channel.identity.rest, where the identity may itself carry dots ──

_KNOWN: dict = {}


def known_identities(hub_dir) -> tuple:
    """Agent names the hub has observed (presence), longest first — so a dotted name
    ('alice.smith') is matched before its first segment would be. Cached 30 s."""
    key = str(hub_dir)
    now = time.time()
    hit = _KNOWN.get(key)
    if hit and now - hit[0] < 30:
        return hit[1]
    try:
        from . import presence
        names = {str(k).lower() for k in presence.read(hub_dir).keys()}
    except Exception:                                        # noqa: BLE001
        names = set()
    value = tuple(sorted((n for n in names if n), key=len, reverse=True))
    _KNOWN[key] = (now, value)
    return value


def split_ident(source, hub_dir=None) -> tuple:
    """(channel, identity, rest) of 'channel.identity.rest'.

    A worker identity may carry dots ('agent.alice.smith.notifier'); a reader that splits
    on the first dot reads the agent as 'alice' and owns the problem by the wrong name.
    Agent identities are matched against the names the hub has observed, longest first;
    anything unknown falls back to the first segment."""
    src = str(source or "").lower()
    if "." not in src:
        return src, "", ""
    channel, remainder = src.split(".", 1)
    if channel == "agent" and hub_dir is not None:
        for name in known_identities(hub_dir):
            if remainder == name or remainder.startswith(name + "."):
                return channel, name, remainder[len(name) + 1:]
    parts = remainder.split(".", 1)
    return channel, parts[0], (parts[1] if len(parts) > 1 else "")


# ── the fingerprint: what varies per OCCURRENCE is stripped ──
# A per-run id in the message defeats the throttle bucket, the ack identity and the problem
# fold at once: one cause becomes N problems, N unthrottled rows and N acks. A UUID collapses
# WHOLE and FIRST — its dashes would otherwise split it into groups too short for the hex pass.
_N_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_N_DIGITS = re.compile(r"\d+")
_N_HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
_N_QUERY = re.compile(r"\?[^\s]*")
_N_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_N_SPACES = re.compile(r"\s+")
# A service-side forwarder's own throttle appends "  [+3 more since last reported]" to the
# message it finally sends. That is a COUNT, not a new failure — left in, it keys a second
# problem for the same cause. Stripped whole, before anything else touches the text.
_N_THROTTLE_SUFFIX = re.compile(r"\s*\[\+\s*\d+\s+more\s+since\s+last\s+reported\]\s*$", re.I)


def normalize_message(message) -> str:
    """The message with per-occurrence detail removed. ONE implementation, used by the
    fingerprint AND the problem fold key: a resolve acks by fingerprint while the board shows
    by fold key, and two copies of these rules drift the first time one learns a new shape."""
    text = _N_THROTTLE_SUFFIX.sub("", str(message or "")).lower()
    text = _N_QUOTED.sub("'...'", text)
    text = _N_QUERY.sub("", text)
    text = _N_UUID.sub("#", text)
    text = _N_HEX.sub("#", text)
    text = _N_DIGITS.sub("#", text)
    return _N_SPACES.sub(" ", text).strip()[:120]


def _origin(source: str, context: dict) -> dict:
    """Where a row came from, as a FIELD — provenance encoded only in a source-naming
    convention is provenance a reader has to know to decode."""
    src = (source or "").lower()
    out = {}
    if src.startswith("agent."):
        parts = source.split(".")
        out["origin"] = "agent"
        if len(parts) > 1:
            out["origin_agent"] = parts[1]
    elif src.startswith("browser."):
        out["origin"] = "browser"
    elif src.startswith("ci."):
        out["origin"] = "ci"
        parts = source.split(".")
        if len(parts) > 1:
            out["origin_app"] = parts[1]
    elif src.startswith("app."):
        out["origin"] = "app"
        parts = source.split(".")
        if len(parts) > 1:
            out["origin_app"] = parts[1]
    elif src.startswith(_EXTERNAL_NOISE):
        out["origin"] = "external"
    else:
        out["origin"] = "hub-server"
    # An explicitly reported machine BEATS anything decoded from the source string: the
    # source key names the agent, which is a person's identity, not a computer's.
    if context.get("machine"):
        out["origin_machine"] = context["machine"]
    if context.get("app"):
        out["origin_app"] = context["app"]
    if context.get("agent") and src.startswith("agent."):
        out["origin_agent"] = context["agent"]
    return out


# Every way an error can reach this stream, and the ones that CANNOT. A list of errors
# answers "what broke"; it never answers "is this everything?" — and a reader who takes an
# empty card as a healthy system while a channel is dark is worse off than one with no card.
CHANNELS = (
    {"key": "django.request", "label": "Host app 5xx",
     "wired": "LOGGING -> hub_core.errorlog.HubErrorHandler (django.request, ERROR+)",
     "match": ("django.request", "django.server")},
    {"key": "hub", "label": "Hub internals",
     "wired": "explicit record() calls in hub code (auth refusals included)",
     "match": ("hub.",), "silence_ok": True},
    {"key": "browser", "label": "Board browser failures",
     "wired": "POST /hub/api/client-error from the board (same-origin, CSRF)",
     "match": ("browser.",), "silence_ok": True},
    {"key": "agent", "label": "Worker-side failures",
     "wired": "POST /hub/api/agent-error from a worker whose own tooling failed",
     "match": ("agent.",)},
    {"key": "app", "label": "Satellite services",
     "wired": "POST /hub/api/app-error from each service's error forwarder",
     "match": ("app.",)},
    {"key": "ci", "label": "CI and deploy results",
     "wired": "POST /hub/api/ci-event from a CI webhook or a pipeline step (webhook secret), and "
              "POST /hub/api/ci-failure from a failing job's own log tail (classified by the log)",
     "match": ("ci.",), "silence_ok": True},
    {"key": "external", "label": "Foreign/scanner traffic",
     "wired": "django.security.* — recorded as warnings, never as our errors",
     "match": ("django.security.",), "silence_ok": True},
)

# What this stream provably does NOT see — stated, because silence about a gap reads as
# coverage, which is the failure this surface exists to prevent.
NOT_COVERED = (
    "A service reaches this stream only once its forwarder is wired and pointed here; a "
    "service that forwards nothing is silent, not healthy.",
    "Process stdout/stderr before logging starts (a boot crash) never reaches here.",
    "A worker with no network path to the hub fails locally and appears only if it "
    "reports after reconnecting (the client records that blind window and reports it then).",
)


def _channel_of(source: str) -> str:
    src = str(source or "").lower()
    for ch in CHANNELS:
        if src.startswith(tuple(m.lower() for m in ch["match"])):
            return ch["key"]
    return ""


def family_of(source: str) -> str:
    """The per-APP / per-PROJECT / per-WORKER family a source belongs to (app.<slug>,
    ci.<project>, agent.<name>). This is what lets a board say 'budget-app has NEVER
    forwarded' — a channel-level 'app lane live' hides the one service whose forwarder is
    not wired."""
    channel, ident, _rest = split_ident(source)
    if channel in ("app", "ci", "agent") and ident:
        return channel + "." + ident
    return ""


def chat_family_of(source: str) -> str:
    """`chat.<slug>` for a source `app.<slug>.agent` — an app's in-app assistant faults get
    their OWN durable stamp, separate from its server errors. A silent chat channel is stated
    as silent, never folded into "forwarder armed" as if it were healthy."""
    channel, ident, rest = split_ident(source)
    if channel == "app" and ident and str(rest or "").split(".")[0] == "agent":
        return "chat." + ident
    return ""


def read_channel_seen(hub_dir) -> dict:
    """{channel: {first, last, count}} — DURABLE, unlike the bounded row window."""
    return _read_json(_channel_seen_path(hub_dir))


def read_sources_seen(hub_dir) -> dict:
    """{family: {first, last, count}} — durable per-app / per-project / per-worker stamps."""
    return _read_json(_sources_seen_path(hub_dir))


def _touch_channel(hub_dir, source: str, now: float) -> None:
    """Remember that this channel has EVER produced a row. 'Silent' computed from the
    retained window alone renders a channel that has never received anything exactly like
    one that is simply quiet this hour."""
    key = _channel_of(source)
    if not key:
        return
    path = _channel_seen_path(hub_dir)
    try:
        data = _read_json(path)
        prior = data.get(key) if isinstance(data.get(key), dict) else {}
        if prior and (now - float(prior.get("last") or 0)) < _SEEN_WRITE_MIN_S:
            return                      # already stamped this minute; the hot path pays nothing
        data[key] = {"first": prior.get("first") or now, "last": now,
                     "count": int(prior.get("count") or 0) + 1}
        _write_json(path, data)
    except OSError:
        pass


def _touch_family(hub_dir, source: str, now: float) -> None:
    keys = [k for k in (family_of(source), chat_family_of(source)) if k]
    if not keys:
        return
    path = _sources_seen_path(hub_dir)
    try:
        data = _read_json(path)
        wrote = False
        for fam in keys:
            prior = data.get(fam) if isinstance(data.get(fam), dict) else {}
            if prior and (now - float(prior.get("last") or 0)) < _SEEN_WRITE_MIN_S:
                continue
            data[fam] = {"first": prior.get("first") or now, "last": now,
                         "count": int(prior.get("count") or 0) + 1}
            wrote = True
        if not wrote:
            return
        if len(data) > _SOURCES_MAX:
            data = dict(sorted(data.items(),
                               key=lambda kv: float((kv[1] or {}).get("last") or 0))[-_SOURCES_MAX:])
        _write_json(path, data)
    except OSError as exc:
        # NEVER silent: a stamp that cannot be written makes the board say "never" about an
        # app that reports fine. Once an hour it becomes an error row of the hub's own.
        _stamp_failure(hub_dir, source, exc, now)


def _stamp_failure(hub_dir, source: str, exc: BaseException, now: float) -> None:
    key = str(hub_dir)
    last = _STAMP_FAILED_AT.get(key)
    if last is not None and now - last < 3600:
        return
    _STAMP_FAILED_AT[key] = now
    try:
        record(hub_dir, "hub.sources_seen",
               "sources-seen stamp could not be written: %s: %s"
               % (type(exc).__name__, str(exc)[:160]),
               severity="error", code="sources_seen_write_failed",
               details="stamping %s into %s" % (str(source)[:80], _sources_seen_path(hub_dir).name),
               context={"component": "hub"})
    except Exception:                                        # noqa: BLE001 - must not recurse
        pass


def touch_seen(hub_dir, source: str, now: float | None = None) -> None:
    """Stamp a source's channel and family as SEEN without writing a row — a green CI event
    proves the lane reaches the hub exactly as a red one would."""
    now = now or time.time()
    _touch_channel(hub_dir, source, now)
    _touch_family(hub_dir, source, now)


def sources_seen_store(hub_dir) -> dict:
    """What the per-family 'seen' verdicts are read from: the file, how many families it
    holds, when it was last written, and whether this process can write it. A 'never' means
    nothing when the store cannot be written; this is how a reader tells the two apart."""
    path = _sources_seen_path(hub_dir)
    out = {"file": path.name, "families": 0, "mtime": None, "writable": None}
    try:
        out["mtime"] = path.stat().st_mtime
    except OSError:
        out["mtime"] = None
    out["families"] = len(read_sources_seen(hub_dir))
    try:
        target = path if path.exists() else path.parent
        out["writable"] = os.access(str(target), os.W_OK)
    except OSError:
        out["writable"] = False
    return out


def coverage(rows, hub_dir=None) -> dict:
    """Which reporting channels are live, which are quiet, which have NEVER reported, and
    what is out of scope."""
    now = time.time()
    ever = read_channel_seen(hub_dir) if hub_dir is not None else {}
    out = []
    for ch in CHANNELS:
        seen = [r for r in rows
                if str(r.get("source") or "").lower().startswith(
                    tuple(m.lower() for m in ch["match"]))]
        newest = max((float(r.get("epoch") or 0) for r in seen), default=0.0)
        mark = ever.get(ch["key"]) if isinstance(ever.get(ch["key"]), dict) else {}
        ever_last = float(mark.get("last") or 0) or None
        # THREE states, because two of them were being called the same thing:
        #   live  - reported inside the retained window
        #   quiet - has reported before, nothing right now (healthy)
        #   never - has NEVER reported since this store began: a coverage HOLE, not quiet
        state = "live" if seen else ("quiet" if ever_last else "never")
        last = newest or ever_last
        out.append({
            "key": ch["key"], "label": ch["label"], "wired": ch["wired"],
            "rows": len(seen),
            "state": state,
            "ever_seen": bool(ever_last or seen),
            "ever_count": int(mark.get("count") or 0),
            "last_seen_epoch": last or None,
            "age_s": int(now - last) if last else None,
            # A channel whose silence is the normal state (the hub's own internals, foreign
            # traffic, an unwired CI lane) is never counted as a coverage hole.
            "silence_ok": bool(ch.get("silence_ok")),
            # Kept for older readers; `state` is the honest field.
            "silent": not seen,
        })
    never = [c["label"] for c in out if c["state"] == "never" and not c["silence_ok"]]
    return {"channels": out, "window_rows": len(rows),
            "families": read_sources_seen(hub_dir) if hub_dir is not None else {},
            "never_reported": never,
            "verdict": ("every expected channel has reported at least once" if not never else
                        "%d channel(s) have NEVER reported: %s — silent is not healthy"
                        % (len(never), ", ".join(never))),
            "not_covered": list(NOT_COVERED)}


# ── the read-time bar: the ONE predicate every consumer shares ──
# TRANSPORT BLIPS ARE NOT WORK, on the board's browser and in a service's browser alike: a
# tab that briefly could not reach its server, an <img> that 404'd, the board's own fetch
# timeout, a fetch the BROWSER cancelled (navigation, Stop, a discarded tab — each engine
# words it differently). Recorded, counted, queryable, never queued.
# Every blip alternative is anchored at BOTH ends: the message must be the transport wording
# and nothing else, so a real fault that merely BEGINS with the phrase ("The operation was
# aborted because the store is corrupt") still queues. A bare AbortError is the BROWSER
# cancelling a fetch (a navigation, Stop, a frozen tab); the board's own fetch timeout aborts
# with a NAMED reason ("Hub did not answer <path> within N seconds"), accepted bare or with the
# TimeoutError:/AbortError: prefix a reporter adds when it writes String(err).
_ABORT = (r"(AbortError: )?(The user aborted a request|The operation was aborted|"
          r"signal is aborted without reason|Fetch is aborted)\.?")
_BARE_TRANSPORT = r"(TypeError: )?(Failed to fetch|NetworkError[^()]*|Load failed)\.?"
# A 5xx/0 status line and a stream-unavailable notice are statuses, not wordings of a fault,
# so they keep their prefix form; everything a real fault could start with is end-anchored.
_BLIP = re.compile(r"^(HTTP (5\d\d|0)\b.*|(Live|Realtime) stream unavailable\b.*|"
                   + _BARE_TRANSPORT + r"|"
                   r"((TimeoutError|AbortError): )?Hub did not answer \S+ within \d+ seconds\.?|"
                   + _ABORT + r")$", re.I)
# A SERVICE's browser reporter (app.<slug>.browser) wraps the same no-response failure in its
# own words first — "request failed: GET <path> - Failed to fetch", "live stream failed:
# TypeError: Failed to fetch" — so the wrapped forms are listed too. Still anchored at the END of
# the bare transport text: a reporter that says "(2 consecutive background attempts, no
# response)" is describing a SUSTAINED outage, and that row still queues. A resource that failed
# to load and an htmx send error name only the resource, so they keep their prefix form.
_APP_BLIP = re.compile(r"^(resource failed to load: .*|network error on \S+|htmx:sendError\b.*|"
                       + _BARE_TRANSPORT + r"|"
                       r"request failed: [A-Z]+ \S* - " + _BARE_TRANSPORT + r"|"
                       r"live stream failed: " + _BARE_TRANSPORT + r"|" + _ABORT + r")$", re.I)
# A service's own POSITIVE CONTROL is not work: an error-visibility self-test fires one real
# error per channel, each carrying a fresh token, to prove the path. patterns/error-visibility.md
# names the token shape; the bar keys on it so a passing probe never queues as a defect.
_SELFTEST_TOKEN = re.compile(r"EV-SELFTEST-[0-9a-f]{12}", re.I)
# Synthetic rows prove a CHANNEL (forwarding-contract probes, canaries, a forced fault raised
# on a deployed service to prove its path here). Listed with ?include=all so the probe can
# still find its own row; never queued, never paged.
_SYNTHETIC = re.compile(r"^(PROOF\b|CANARY\b)|\(ignore\)|\bFORCED FAULT PROBE\b", re.I)
_SYNTHETIC_CODES = {"probe", "canary", "selfcheck", "proof"}
# A foreign client is foreign whoever forwarded it: a service's forwarder files everything
# under app.<slug>.<kind>, so the source-prefix rule above can never see these. Anchored to
# the exception NAME so an ordinary error that discusses a host header still queues.
_FOREIGN_HOST = re.compile(r"\b(DisallowedHost|SuspiciousOperation)\b")


def deploy_refs() -> tuple:
    """The refs a push DEPLOYS from. A CI failure anywhere else is a branch run — its
    author's, never the queue's. HUB_DEPLOY_REFS overrides (comma-separated)."""
    raw = os.environ.get("HUB_DEPLOY_REFS") or "main,master"
    return tuple(r.strip().lower() for r in raw.split(",") if r.strip())


def is_synthetic(row) -> bool:
    code = str((row or {}).get("code") or "").lower()
    return bool(_SYNTHETIC.search(str((row or {}).get("message") or ""))) or code in _SYNTHETIC_CODES


def _is_wsgi_trigger_blip(row) -> bool:
    """A WSGI server's post-response loopback-trigger write failing (the response was
    already written; nothing is left for a person to do). Matched on the TRACEBACK's frames,
    never the message — the server wraps EVERY exception in the same message line, so a
    message rule would hide real 500s. Separators are normalised: the trace is rendered by
    the host's os.path."""
    if not str(row.get("source") or "").lower().startswith("waitress"):
        return False
    details = str(row.get("details") or "").replace("\\", "/").lower()
    return "waitress/trigger.py" in details and "_physical_pull" in details


def passes_bar(row) -> tuple:
    """(on_bar, reason): critical or error, this system's own surface, actionable now."""
    if row.get("external"):
        return False, "foreign client, not this system"
    sev = str(row.get("severity") or "error").lower()
    if sev not in ("critical", "error"):
        return False, "severity %s" % sev
    src = str(row.get("source") or "")
    msg = str(row.get("message") or "")
    if src.startswith("ci."):
        ctx = row.get("context") or {}
        ref = str(ctx.get("ref") or "").strip().lower()
        if ref and ref not in deploy_refs():
            who, how = str(ctx.get("actor") or "").strip(), str(ctx.get("source") or "").strip()
            return False, "branch run on %s%s: the ref's author's%s, counted, not queued" % (
                ref, (" (%s)" % how) if how else "", (" (%s)" % who) if who else "")
    if src.startswith("browser.") and _BLIP.match(msg):
        return False, "transport blip that recovered"
    if src.startswith("app.") and src.endswith(".browser") and _APP_BLIP.match(msg):
        return False, "browser transport blip in the service: counted, not queued"
    if src.startswith("app.") and _SELFTEST_TOKEN.search(msg):
        return False, "error self-test positive-control probe: counted, not queued"
    if src.startswith("app.") and _FOREIGN_HOST.search(msg):
        return False, "foreign client sent a host header we do not serve: counted, not queued"
    if is_synthetic(row):
        return False, "synthetic proof row: it proves the channel, it is not work"
    # A BLIND WINDOW IS ONLY EVER REPORTED AFTER IT CLOSED — the client raises it on its
    # first successful call after the window — so the row can only exist because the path is
    # already back. Nobody can pick it up; a machine dark RIGHT NOW is a presence fact.
    if str(row.get("code") or "") == "hub_unreachable_span":
        return False, "blind window that had already closed when it reported: counted, not queued"
    if _is_wsgi_trigger_blip(row):
        return False, "WSGI wakeup-trigger blip after the response was sent: counted, not queued"
    return True, ""


# ── the durable throttle ──

def _throttle(hub_dir, fingerprint: str, now: float, severity: str = "") -> dict:
    """Has this exact signature been recorded inside the window, on ANY worker?

    Returns {"suppressed": True, "count": n} to drop the row (the occurrence is still
    counted and its time stamped), or {"since": n} to write it and report how many were
    folded in since the last write. A change of SEVERITY is a new fact, never a recurrence.

    Fail-OPEN: if the sidecar cannot be read or written, the row is written."""
    path = _seen_path(hub_dir)
    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".errors-seen.lock", timeout=3):
            state = _read_json(path)
            prior = state.get(fingerprint) or {}
            first = float(prior.get("first") or 0)
            count = int(prior.get("count") or 0)
            same_sev = str(prior.get("sev") or severity) == str(severity)
            if first and (now - first) < THROTTLE_S and same_sev:
                state[fingerprint] = {"first": first, "count": count + 1, "last": now,
                                      "sev": severity}
                out = {"suppressed": True, "count": count + 1}
            else:
                state[fingerprint] = {"first": now, "count": 1, "last": now, "sev": severity}
                out = {"since": count} if (count and same_sev) else {}
            if len(state) > _LAST_SEEN_MAX:
                for stale in [fp for fp, v in state.items()
                              if (now - float((v or {}).get("first") or 0)) >= THROTTLE_S]:
                    state.pop(stale, None)
            _write_json(path, state)
            return out
    except Exception:                                        # noqa: BLE001 - fail open
        return {}


def _compact_locked(hub_dir) -> None:
    path = _errors_path(hub_dir)
    try:
        if not path.exists() or path.stat().st_size <= MAX_BYTES:
            return
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_ROWS:]
        atomic.write_text(path, "\n".join(rows) + ("\n" if rows else ""))
    except OSError:
        return


def fingerprint_of(source, code, message) -> str:
    return hashlib.sha256(
        f"{_clean(source or 'runtime', 120)}\0{_clean(code or 'runtime_error', 120)}\0"
        f"{normalize_message(_clean(message or 'Unspecified operational error'))}"
        .encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def record(hub_dir, source, message, *, severity="error", code="runtime_error",
           details="", context=None) -> dict:
    """Append one safe diagnostic row and return its public representation. Throttled per
    fingerprint across every worker (count preserved on the next written row); redacted;
    bounded; fail-soft."""
    key = str(hub_dir)
    now = time.time()
    clean_source = _clean(source or "runtime", 120)
    clean_code = _clean(code or "runtime_error", 120)
    clean_message = _clean(message or "Unspecified operational error")
    clean_details = _clean_details(details)
    fingerprint = fingerprint_of(clean_source, clean_code, clean_message)
    raw_sev = str(severity or "").strip().lower()
    row = {
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
        "epoch": round(now, 3),
        # FAIL CLOSED ON SEVERITY: a level the stream does not name lands as a warning and
        # says why, rather than being coerced onto the queue as an error.
        "severity": _SEVERITY_MAP.get(raw_sev, "warning") if raw_sev else "error",
        "source": clean_source,
        "code": clean_code,
        "message": clean_message,
        "fingerprint": fingerprint,
    }
    if raw_sev and raw_sev not in _SEVERITY_MAP:
        row["severity_raw"] = raw_sev[:40]
    ctx = _context(context)
    if ctx:
        row["context"] = ctx
    row.update(_origin(clean_source, ctx))
    if clean_details:
        row["details"] = clean_details
        # The cause rides on the ROW, so every reader names the failure without fetching the
        # trace — only when it adds something (a message that IS the exception line would
        # otherwise print twice).
        cause = cause_of(clean_details)
        if cause and cause != clean_message:
            row["cause"] = cause
    if str(clean_source).lower().startswith(_EXTERNAL_NOISE):
        # Classified BEFORE the throttle, so a suppressed repeat reports the same severity
        # its written sibling did.
        row["severity"] = "warning"
        row["external"] = True
    _touch_channel(hub_dir, clean_source, now)
    _touch_family(hub_dir, clean_source, now)
    verdict = _throttle(hub_dir, fingerprint, now, row["severity"])
    if verdict.get("suppressed"):
        row["suppressed_since"] = verdict["count"]
        return row
    if verdict.get("since"):
        row["occurrences_since_last"] = verdict["since"]

    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            _compact_locked(hub_dir)
            # atomic.append_line: on Windows the open loses to a reader holding the file.
            atomic.append_line(_errors_path(hub_dir),
                               json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n",
                               newline="\n", fsync=False)
        _WRITE_FAILURE.pop(key, None)
    except Exception as exc:                                 # noqa: BLE001 - must not raise
        # Logging cannot raise into the failing request, but the board must not call a
        # broken store "quiet": this flag makes the read surface explicitly impaired.
        _WRITE_FAILURE[key] = {"reason": _clean(type(exc).__name__, 80), "at": row["ts"]}
    return row


def read(hub_dir, limit=READ_LIMIT) -> tuple[list, dict]:
    """Newest-first rows plus honest availability/retention metadata (the WINDOW is named:
    a bounded log that does not say what it is showing lies about what it holds).

    Callers that FOLD (the problem queue, app health) may ask for every retained row, up to
    KEEP_ROWS; the board's raw list keeps READ_LIMIT."""
    key = str(hub_dir)
    path = _errors_path(hub_dir)
    failure = _WRITE_FAILURE.get(key)
    try:
        if not path.exists():
            hub = Path(hub_dir)
            writable = os.access(hub if hub.exists() else hub.parent, os.W_OK)
            metadata = {"available": writable and failure is None,
                        "retention": KEEP_ROWS, "stored": 0, "bytes": 0}
            if failure:
                metadata.update(failure)
            elif not writable:
                metadata["reason"] = "store_not_writable"
            return [], metadata
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        decoded, malformed = [], 0
        for line in raw:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    decoded.append(item)
                else:
                    malformed += 1
            except (TypeError, ValueError):
                malformed += 1
        window = max(1, min(int(limit), KEEP_ROWS))
        rows = decoded[-window:]
        rows.reverse()
        # A recurrence the throttle folded into the count still HAPPENED. The sidecar's
        # last-occurrence stamp and folded count belong to the NEWEST row of a signature only
        # (stamped on every row they would make older rows look recurrent, or multiply).
        seen_state = _read_json(_seen_path(hub_dir))
        newest_of = set()
        for row in rows:                  # newest-first, so the first wins
            fp = row.get("fingerprint")
            if fp in newest_of:
                continue
            newest_of.add(fp)
            mark = seen_state.get(fp) if isinstance(seen_state.get(fp), dict) else {}
            last = float(mark.get("last") or 0)
            if last and last > float(row.get("epoch") or 0) + 0.01:
                row["last_occurrence"] = round(last, 3)
            folded = int(mark.get("count") or 0)
            if folded > 1:
                row["occurrences_folded"] = folded
        acked = read_acked(hub_dir)
        for row in rows:
            # Only the occurrences the ack actually covered. A recurrence after it is a NEW
            # event: a signature-only mute once hid eight of nine live rows of a different
            # root cause behind a days-old ack whose note claimed "0 recurrences since".
            if is_acked(row, acked):
                row["acked"] = acked[row.get("fingerprint")]
        metadata = {
            "available": failure is None,
            "retention": KEEP_ROWS,
            "stored": len(raw),
            "window": window,
            "returned": len(rows),
            "acked_count": sum(1 for row in rows if row.get("acked")),
            "bytes": path.stat().st_size,
            "malformed": malformed,
        }
        if failure:
            metadata.update(failure)
        return rows, metadata
    except OSError as exc:
        return [], {"available": False, "retention": KEEP_ROWS, "stored": None,
                    "reason": _clean(type(exc).__name__, 80)}


def read_acked(hub_dir) -> dict:
    """Signature -> when it was acknowledged and by whom."""
    return _read_json(_acked_path(hub_dir))


ACK_SKEW_S = 2.0


def is_acked(row: dict, acked: dict | None = None, hub_dir=None) -> bool:
    """Is THIS occurrence acknowledged? An ack covers only rows AT OR BEFORE its own
    timestamp; a recurrence after it re-surfaces, because nobody accepted it. The single
    predicate behind the row's `acked` mark, the unclaimed count every reader shares, and the
    only_acked clear — so the board, the API and an agent can never disagree about a row.

    Fails OPEN toward VISIBLE: an ack with no readable time, or a row with none, mutes
    nothing. A real error wrongly hidden is worse than a handled one shown twice."""
    if acked is None:
        acked = read_acked(hub_dir) if hub_dir is not None else {}
    mark = acked.get(row.get("fingerprint"))
    if not isinstance(mark, dict):
        return False
    ack_at = _epoch(mark.get("at"))
    if ack_at == float("-inf"):
        return False
    row_at = _epoch(row.get("epoch") if row.get("epoch") not in (None, "") else row.get("ts"))
    if row_at == float("-inf"):
        return False
    # A recurrence the throttle FOLDED into this row after the ack is still a new event.
    row_at = max(row_at, float(row.get("last_occurrence") or 0))
    # The ack is written after the row it accepts; allow clock jitter and rounding so an
    # accepted row never leaks back as "new".
    return row_at <= ack_at + ACK_SKEW_S


def ack(hub_dir, fingerprint: str, actor: str = "", note: str = "") -> dict:
    """Acknowledge one error SIGNATURE, up to NOW. Acking is not deleting: the rows stay in
    the stream and stay countable — they just stop competing for attention with failures
    nobody has looked at yet. A recurrence after this moment is not covered (is_acked).
    Returns {} on failure so a caller can report honestly."""
    fingerprint = _clean(fingerprint, 32)
    if not fingerprint:
        return {}
    # The note is the root cause a person will read next; it is redacted, never cut.
    entry = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "by": _clean(actor, 60), "note": _note(note)}
    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            current = read_acked(hub_dir)
            current[fingerprint] = entry
            if len(current) > KEEP_ROWS:      # bounded like the stream itself
                for stale in sorted(current, key=lambda k: current[k].get("at", "")
                                    )[:len(current) - KEEP_ROWS]:
                    current.pop(stale, None)
            atomic.write_json(_acked_path(hub_dir), current)
    except Exception:                                        # noqa: BLE001
        return {}
    return entry


def unack(hub_dir, fingerprint: str) -> bool:
    """Reopen a signature — an ack that cannot be undone is a delete with extra steps.
    False when the signature was not acked (a reopen of nothing must not report success)."""
    fingerprint = _clean(fingerprint, 32)
    try:
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            current = read_acked(hub_dir)
            if fingerprint not in current:
                return False
            current.pop(fingerprint)
            atomic.write_json(_acked_path(hub_dir), current)
        return True
    except Exception:                                        # noqa: BLE001
        return False


def clear(hub_dir, before_epoch=None, only_acked=False) -> dict:
    """Remove rows outright — the operation a person wants after a fix ships. Bounded by
    design: by AGE or by ACK, never "everything". only_acked is a RESTRICTION: with it set
    an unacknowledged occurrence (including a recurrence after an ack) never drops however
    old, and an age bound beside it narrows the acked set rather than widening the clear."""
    if before_epoch is None and not only_acked:
        return {"removed": 0, "kept": 0, "reason": "refused: pass an age or only_acked"}
    acked = read_acked(hub_dir)
    kept, removed = [], 0
    path = _errors_path(hub_dir)
    try:
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=10):
            if not path.exists():
                return {"removed": 0, "kept": 0}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    removed += 1          # malformed rows are not evidence of anything
                    continue
                old = before_epoch is None or float(row.get("epoch") or 0) < float(before_epoch)
                if only_acked:
                    # is_acked, not membership: a recurrence after the ack is NOT acked, so
                    # an only_acked clear must never delete the rows the ack keeps visible.
                    drop = is_acked(row, acked) and old
                else:
                    drop = old
                if drop:
                    removed += 1
                else:
                    kept.append(line)
            atomic.write_text(path, "\n".join(kept) + ("\n" if kept else ""))
    except Exception as exc:                                 # noqa: BLE001
        return {"removed": 0, "kept": 0, "reason": type(exc).__name__}
    return {"removed": removed, "kept": len(kept)}


def stamp(hub_dir) -> tuple:
    """Cheap SSE change fingerprint over every sidecar the queue reads — acknowledging, and
    claiming or resolving a problem, are changes to the queue that write OTHER files than the
    stream. A watcher on the stream alone shows a claimed row as unclaimed on every other
    open board until something unrelated appends, which is exactly the double-pickup the
    claim exists to prevent."""
    size = mtime = 0
    hub = Path(hub_dir)
    for path in (_errors_path(hub_dir), _acked_path(hub_dir), hub / "problem-claims.json",
                 hub / "problem-resolved.json", hub / "problem-escalations.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        size += stat.st_size
        mtime = max(mtime, stat.st_mtime_ns)
    return size, mtime


class HubErrorHandler(logging.Handler):
    """Logging handler that records ERROR+ without exposing args or request bodies. Wire it
    on django.request (or any logger) with the hub dir:

        LOGGING["handlers"]["hub_errors"] = {
            "()": "hub_core.errorlog.HubErrorHandler", "hub_dir": str(HUB_DIR),
            "level": "ERROR"}
    """

    def __init__(self, hub_dir, level=logging.ERROR):
        super().__init__(level=level)
        self.hub_dir = str(hub_dir)

    def emit(self, log_record) -> None:  # pragma: no cover - exercised by logging itself
        try:
            if log_record.name == __name__:
                return
            details = ""
            if log_record.exc_info:
                details = "".join(traceback.format_exception(*log_record.exc_info))
            record(
                self.hub_dir, log_record.name, log_record.getMessage(),
                severity="critical" if log_record.levelno >= logging.CRITICAL else "error",
                code=getattr(log_record, "code", None) or log_record.levelname.lower(),
                details=details,
                context={"component": getattr(log_record, "module", "")},
            )
        except Exception:
            self.handleError(log_record)
