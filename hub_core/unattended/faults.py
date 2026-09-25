"""The lane's own failures, told apart from the item's.

An exit code cannot distinguish "it worked" from "it never ran". A launcher that reads every
non-zero exit as "the item defeated its responder" charges attempts against items nobody ever
read — two such runs retire an item to "leave it for a person" without a line of work done. So
every run is classified before it is charged:

* USAGE LIMIT — the account is out of usage. Non-zero exit whose output says so. Nothing is
  charged; every launch on this machine pauses until the reset the message names (bounded, so a
  misread clock can never park a machine for a day); the queue signature is forgotten so the first
  pass after the pause looks again.
* HARNESS DEAD — the run failed before doing ANY work for a reason that belongs to this machine
  (not logged in, bad key, no credit), or exited non-zero having spent nothing. Nothing is
  charged; the board gets a critical agent-error with the worker's own last words; launches pause
  for half an hour, because a credential a person must renew will not fix itself by retrying.
* AUTH EXPIRED — the subset of a dead harness that is an expired login (``auth_expired`` on the
  verdict). It DISARMS the lane until a person logs in again (``__main__.auth_expired``) instead
  of pausing it: a credential nobody renewed does not renew itself.
* API ERROR — the run DID work and the model API ended it (a 5xx the runtime gave up on). Not the
  item's failure and not a dead harness: a warning on the board, a five-minute pause, the item
  back on the queue.

The runtime's result may be ONE object or a LIST of result objects, and may be preceded by a line
of runtime chatter. A parser that only accepts one object reads a 19-turn run that ended on an
API error as "not JSON, 0 tokens: it never ran". ``num_turns > 0`` means it ran, whatever the
token parse says.
"""
from __future__ import annotations

import json
import re
import time

USAGE_LIMIT_RE = re.compile(
    r"hit your [\w -]{0,20}limit|reached your [\w -]{0,20}limit|"
    r"(?:usage|session|weekly|daily|5-hour) limit (?:reached|exceeded)", re.I)
USAGE_RESET_CLOCK_RE = re.compile(
    r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b(?:\s*\(([\w/+-]+)\))?", re.I)
# A WEEKLY limit names a DATE ("resets Sep 28, 3pm (Region/City)"). The clock pattern cannot
# match it (a month sits between "resets" and the hour), so it fell to the default pause and a
# machine probed a dead account every half hour for days.
USAGE_RESET_DATED_RE = re.compile(r"resets\s+(?:on\s+)?([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+"
                                  r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b"
                                  r"(?:\s*\(([\w/+-]+)\))?", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                            "aug", "sep", "oct", "nov", "dec"))}
USAGE_RESET_EPOCH_RE = re.compile(r"limit reached\|(\d{10})")
USAGE_LIMIT_DEFAULT_PAUSE_S = 1800
USAGE_LIMIT_MAX_PAUSE_S = 6 * 3600

HARNESS_DEAD_RE = re.compile(
    r"not logged in|please run /login|invalid api key|authentication[ _-]?error|"
    r"oauth token (?:has )?expired|credit balance is too low|"
    r"unable to (?:authenticate|connect to the api)", re.I)
HARNESS_DEAD_PAUSE_S = 1800
# The subset of a dead harness that is an EXPIRED LOGIN: a credential nobody renewed does not
# renew itself, so it DISARMS the lane until a person logs in (see ``__main__.auth_expired``),
# rather than pausing it and burning the next item on the same dead login every half hour.
AUTH_EXPIRED_RE = re.compile(
    r"oauth (?:session|token) (?:has )?expired|not logged in|please run /login|invalid api key|"
    r"authentication[ _-]?error", re.I)
API_ERROR_PAUSE_S = 300


def results(stdout: str) -> list[dict]:
    """The runtime's result object(s), in whatever shape this machine's runtime emits."""
    text = stdout or ""
    candidates = [text]
    for opener in ("\n{", "\n["):
        at = text.find(opener)
        if at >= 0:
            candidates.append(text[at + 1:])
    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _codex_events(stdout: str) -> list[dict]:
    events = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and str(event.get("type") or "").startswith(
                ("thread.", "turn.", "item.")):
            events.append(event)
    return events


def parse_output(stdout: str) -> tuple[int, str, int]:
    """(tokens, final message, turns) from either runtime's machine-readable output.

    Claude Code (``-p --output-format json``): one object or a list; tokens from ``usage``, or
    per model from ``modelUsage`` when the API ended the run; turns from ``num_turns``.
    Codex (``exec --json``): JSONL events; tokens from ``turn.completed`` usage (cached input is a
    SUBSET of input, so it is not added again), the final ``agent_message`` item, and one turn per
    ``turn.completed``. Anything else is raw text with tokens 0."""
    events = _codex_events(stdout)
    if events:
        tokens, result, turns = 0, "", 0
        for event in events:
            if event.get("type") == "turn.completed":
                turns += 1
                usage = event.get("usage") or {}
                tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                result = str(item.get("text") or "")
        return tokens, result, turns
    found = results(stdout)
    if not found:
        return 0, stdout or "", 0
    tokens, turns = 0, 0
    for item in found:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        spent = sum(int(usage.get(key) or 0) for key in (
            "input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens"))
        if not spent and isinstance(item.get("modelUsage"), dict):
            for per_model in item["modelUsage"].values():
                if isinstance(per_model, dict):
                    spent += sum(int(per_model.get(key) or 0) for key in (
                        "inputTokens", "outputTokens", "cacheReadInputTokens",
                        "cacheCreationInputTokens"))
        tokens += spent or int(item.get("total_tokens") or 0)
        turns = max(turns, int(item.get("num_turns") or 0))
    return tokens, str(found[-1].get("result") or ""), turns


def session_of(stdout: str) -> str:
    """The runtime's own session id: Claude's ``session_id`` or Codex's ``thread.started``."""
    for item in results(stdout):
        if item.get("session_id"):
            return str(item["session_id"])
    for event in _codex_events(stdout):
        if event.get("type") == "thread.started":
            return str(event.get("thread_id") or "")
    return ""


def usage_limit_until(rc, text: str, now: float | None = None) -> float | None:
    """When a run EXITED NON-ZERO saying the account is out of usage: the epoch at which
    launching makes sense again. None otherwise.

    The exit code is part of the test on purpose: a run that finished normally and merely
    discussed a limit (a responder fixing an API's rate limit writes exactly those words) is not
    paused, and a killed run (rc None) is the clock's, not the account's."""
    if rc in (0, None) or not USAGE_LIMIT_RE.search(text or ""):
        return None
    now = now or time.time()
    match = USAGE_RESET_EPOCH_RE.search(text)
    dated = None if match else USAGE_RESET_DATED_RE.search(text)
    if match:
        target = float(match.group(1))
    elif dated and dated.group(1).lower() in _MONTHS:
        month, day = _MONTHS[dated.group(1).lower()], int(dated.group(2))
        hour = int(dated.group(3)) % 12 + (12 if dated.group(5).lower() == "pm" else 0)
        minute = int(dated.group(4) or 0)
        try:
            from datetime import datetime
            zone = None
            if dated.group(6):
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(dated.group(6))
            here = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now)
            at = here.replace(month=month, day=day, hour=hour, minute=minute, second=0,
                              microsecond=0)
            if at.timestamp() <= now:                     # "Jan 2" read in late December
                at = at.replace(year=at.year + 1)
            target = at.timestamp()
        except Exception:  # noqa: BLE001 - an unreadable date gets the default
            target = now + USAGE_LIMIT_DEFAULT_PAUSE_S
    else:
        match = USAGE_RESET_CLOCK_RE.search(text)
        if not match:
            return now + USAGE_LIMIT_DEFAULT_PAUSE_S
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
        minute = int(match.group(2) or 0)
        target = None
        if match.group(4):
            try:
                from datetime import datetime, timedelta
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(match.group(4))
                at = datetime.fromtimestamp(now, zone).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)
                if at.timestamp() <= now:
                    at += timedelta(days=1)
                target = at.timestamp()
            except Exception:  # noqa: BLE001 - no tz database: fall back to local time
                target = None
        if target is None:
            local = time.localtime(now)
            target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute,
                                  0, 0, 0, -1))
            if target <= now:
                target += 86400
    return min(max(target + 60, now + 60), now + USAGE_LIMIT_MAX_PAUSE_S)


def api_error(stdout: str) -> str:
    """Non-empty when the run RAN and then lost its session to the model API itself."""
    for item in results(stdout):
        if not item.get("is_error"):
            continue
        status = item.get("api_error_status")
        reason = str(item.get("terminal_reason") or "")
        text = " ".join(str(item.get("result") or "").split())
        match = re.search(r"API Error:?\s*(\d{3})", text)
        if reason != "api_error" and not status and not match:
            continue
        status = status or (match.group(1) if match else "?")
        turns = int(item.get("num_turns") or 0)
        seconds = int(item.get("duration_ms") or 0) // 1000
        return ("the model API answered %s after %d turn%s and %ds: an upstream fault, not the "
                "item's (%s)" % (status, turns, "" if turns == 1 else "s", seconds,
                                 text[:160] or reason))
    return ""


def harness_dead(rc, tokens: int, turns: int, text: str) -> str:
    """Non-empty when a run failed BEFORE DOING ANY WORK for a reason that is this machine's.

    Both halves matter: a non-zero exit that spent tokens argued with the item and lost (an honest
    failure on the item); a non-zero exit that spent nothing never started."""
    if rc == 0 or int(tokens or 0) > 0 or int(turns or 0) > 0:
        return ""
    if re.search(r'"num_turns":\s*[1-9]', text or ""):
        return ""
    match = HARNESS_DEAD_RE.search(text or "")
    if match:
        return "the agent harness is not authenticated on this machine (%s)" % match.group(0).strip()
    if rc is None:
        return ""                       # killed at its clock: that is the reaper's report
    return "the worker exited %s having spent no tokens: it never ran" % rc


def auth_expired_text(rc, text: str) -> str:
    """The matched words when a run EXITED NON-ZERO because this machine's agent login is
    expired or missing; '' otherwise. A killed run (rc None) and a clean exit are never it."""
    if rc in (0, None):
        return ""
    match = AUTH_EXPIRED_RE.search(text or "")
    return match.group(0).strip() if match else ""


def classify(rc, stdout: str, *, tail_chars: int = 4000) -> dict:
    """One verdict for a finished run: ``{kind, tokens, turns, result, session, reason, until}``
    where ``kind`` is ``usage-limit`` | ``api-error`` | ``harness-dead`` | ``ran``."""
    tokens, result, turns = parse_output(stdout)
    text = (result or "") + "\n" + (stdout or "")[-tail_chars:]
    verdict = {"tokens": tokens, "turns": turns, "result": result, "session": session_of(stdout),
               "reason": "", "until": None, "kind": "ran", "auth_expired": ""}
    until = usage_limit_until(rc, text)
    if until:
        verdict.update(kind="usage-limit", until=until,
                       reason=" ".join(str(result or stdout or "").split())[:160])
        return verdict
    fault = api_error(stdout)
    if fault:
        verdict.update(kind="api-error", reason=fault, until=time.time() + API_ERROR_PAUSE_S)
        return verdict
    dead = harness_dead(rc, tokens, turns, text)
    if dead:
        verdict.update(kind="harness-dead", reason=dead, until=time.time() + HARNESS_DEAD_PAUSE_S)
    # An expired login is a lane fault whatever the token count: a run that did a few turns and
    # then lost its login is not the item's failure either.
    expired = auth_expired_text(rc, text)
    if expired:
        verdict["auth_expired"] = expired
        if verdict["kind"] == "ran":
            verdict.update(kind="harness-dead", until=time.time() + HARNESS_DEAD_PAUSE_S,
                           reason="the agent login on this machine expired (%s)" % expired)
    return verdict
