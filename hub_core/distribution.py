"""Is every seat running what this hub publishes? Answered on a surface, never asked of a person.

Framework-free: the adapter supplies the presence rows and the published artifact files; this
module does the comparison. Every seat already reports the sha of each artifact it runs (the
client sends ``X-Hub-Artifacts``: the client itself, the charter core, and any adopter-declared
file), and the hub already holds the bytes it expects them to run. What was missing was the
COMPARISON — so "did the improvement land everywhere?" was a question for a person instead of a
row on a surface.

Rules this module holds, each paid for on the origin system:

* GRADE WHAT THE SEAT ACTUALLY HASHES. Compare the same bytes in the same form: both sides hash
  LF-normalized content, and the raw-bytes form is accepted too, so a checkout with CRLF endings
  or an older client is never reported stale for a line-ending difference. The first live pages
  of the origin system's version of this surface were false for exactly that reason.
* VERSION+SHA IS SPLIT BEFORE GRADING. A report of "1.1+abc123" can never prefix-match a sha;
  graded raw, every seat reads stale forever and nobody notices, because a stale client right
  after a publish looks exactly like propagation.
* A ROW WITHOUT A MACHINE IS LEGACY, A MACHINE WITHOUT KIT TELEMETRY IS A PHANTOM. Neither is
  graded, counted, or paged — both are listed, so they stay visible for tracing.
* OFFLINE IS NOT DRIFT. A seat that cannot reach the hub cannot converge; blaming it teaches
  people the surface lies. Seats silent past DORMANT_AFTER_S are DORMANT and kept off the offline
  list, so it stays short enough that a yesterday-was-fine seat is noticed in it.
* COLLAPSED IS NOT DROPPED. A dormant seat stays in the machine list (state ``dormant``, never
  graded), in the verdict, and in the operator inbox as ONE stable "confirm it is retired, or turn
  it on" item -- including a row presence retirement already archived, for DORMANT_KEEP_S. On the
  origin system a seat past the horizon left the list, the verdict and the inbox at once, so the
  one failure that needs a person became nobody's item exactly when it became permanent.
* THE VERDICT CARRIES ITS OWN SCOPE. "every online seat is current" is true and reads as
  all-clear while excluding exactly the seats most likely to have a problem. The verdict names
  what it did NOT grade: silent seats, phantoms, legacy rows, and artifacts nobody publishes.
* A SILENT SEAT REACHES A PERSON. Everything converges by pulling, so a seat that makes no
  requests has no channel to fix it; past DARK_AFTER_S it becomes an OPERATOR inbox item of
  kind ``offline`` (never ``drift``). Drift on an ONLINE seat is raised only once the artifact
  has been published longer than DRIFT_PATIENCE_S, measured from the published file's own mtime,
  and the item says when it was computed, because a seat that just updated reports it on its next
  request. A graded fact with NO published file (the seat's interpreter, below) has no mtime, so
  its patience runs from when THIS seat was first observed stale on it (``first_stale``, kept by
  the adapter across passes) -- read as zero it could never raise an item at all.
* A RUNTIME REQUIREMENT IS GRADED LIKE AN ARTIFACT. A seat reports its interpreter version
  (``python`` on its presence row) and whether it can push (``push``: yes | no | unknown, from its
  own non-interactive probe -- patterns/machine-push-identity.md). With a required ``major.minor``
  configured, a seat on another line is stale; an absent report is ``unreported``, never stale.
  ``push`` is shown, never graded: a seat that cannot push is the hand-off lane's business.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

W = 16                                   # reported shas are compared at 16 hex chars
OFFLINE_AFTER_S = 2 * 3600
DORMANT_AFTER_S = 72 * 3600
DARK_AFTER_S = 6 * 3600
DRIFT_PATIENCE_S = 6 * 3600
DORMANT_KEEP_S = 30 * 86400


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def published(files: dict) -> dict:
    """{name: path} -> {name: {"accepted": [sha, ...], "sha": sha, "published_at": mtime}}.
    A missing or unreadable file is simply unpublished — never a crash, never a guess."""
    out = {}
    for name, path in (files or {}).items():
        try:
            p = Path(path)
            raw = p.read_bytes()
            mtime = p.stat().st_mtime
        except (OSError, TypeError, ValueError):
            continue
        lf = _digest(raw.replace(b"\r\n", b"\n"))
        accepted = [lf, _digest(raw), _digest(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))]
        out[str(name).lower()] = {"sha": lf[:W], "accepted": sorted(set(a[:W] for a in accepted)),
                                  "published_at": mtime}
    return out


def _sha_half(value) -> str:
    value = str(value or "").strip()
    return value.rsplit("+", 1)[-1] if "+" in value else value


def grade(reported, accepted) -> str:
    """current | stale | unreported | unpublished — one artifact on one seat."""
    accepted = [a for a in (accepted or []) if a]
    if not accepted:
        return "unpublished"
    reported = _sha_half(reported)[:W].lower()
    if not reported:
        return "unreported"
    return "current" if any(a.startswith(reported) or reported.startswith(a) for a in accepted) \
        else "stale"


def grade_python(reported, required) -> str:
    """current | stale | unreported | unpublished for a seat's interpreter against a required
    ``(major, minor)`` (``None`` = nothing required, so nothing is graded)."""
    if not required:
        return "unpublished"
    value = str(reported or "").strip()
    if not value:
        return "unreported"
    m = re.match(r"(\d+)\.(\d+)", value)
    if not m:
        return "stale"
    return "current" if (int(m.group(1)), int(m.group(2))) == tuple(required)[:2] else "stale"


def parse_required_python(value):
    """``"3.13"`` -> ``(3, 13)``; anything unparseable -> None (not graded)."""
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _epoch(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def assess(rows, pub: dict, *, now: float | None = None, is_kit=None,
           is_service=None, retired=None, required_python=None) -> dict:
    """Every (seat, artifact) against the published truth, plus a verdict that states its scope.

    ``retired`` are presence rows retirement archived: a kit seat among them silent between
    DORMANT_AFTER_S and DORMANT_KEEP_S is still listed as dormant (retirement archives; only an
    explicit forget deletes). ``required_python`` is an optional ``(major, minor)``."""
    now = time.time() if now is None else now
    is_kit = is_kit or (lambda r: bool(r.get("machine")) and bool(r.get("client") or r.get("artifacts")))
    is_service = is_service or (lambda agent: False)
    machines, drifted, offline, dormant, phantoms, legacy = [], [], [], [], [], []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        agent = str(row.get("agent") or "?")
        if is_service(agent):
            continue                                   # automation is not somebody's seat
        name = str(row.get("machine") or "").strip()
        if not name:
            legacy.append(agent)
            machines.append({"machine": "(legacy row)", "agent": agent, "state": "legacy",
                             "artifacts": {}})
            continue
        if not is_kit(row):
            phantoms.append(name)
            machines.append({"machine": name, "agent": agent, "state": "phantom", "artifacts": {}})
            continue
        seen = _epoch(row.get("last_seen"))
        age = max(0.0, now - seen) if seen else None
        entry = {"machine": name, "agent": agent,
                 "seen_min_ago": int(age // 60) if age is not None else None, "artifacts": {}}
        entry["python"] = str(row.get("python") or "") or "unreported"
        entry["push"] = str(row.get("push") or "") or "unreported"
        if age is not None and age > DORMANT_AFTER_S:
            dormant.append(name)
            entry["state"] = "dormant"
            machines.append(entry)
            continue                                   # listed, never graded
        if age is None or age > OFFLINE_AFTER_S:
            entry["state"] = "offline"
            offline.append(name)
            machines.append(entry)
            continue
        reported = dict(row.get("artifacts") or {}) if isinstance(row.get("artifacts"), dict) else {}
        if "client" not in reported and row.get("client"):
            reported["client"] = row["client"]         # version+sha form, split by grade()
        checks = {artifact: grade(reported.get(artifact), meta.get("accepted"))
                  for artifact, meta in pub.items()}
        if required_python:
            checks["python"] = grade_python(row.get("python"), required_python)
        entry["artifacts"] = checks
        stale = sorted(k for k, v in checks.items() if v == "stale")
        entry["state"] = "drifted" if stale else "current"
        if stale:
            entry["stale"] = stale
            drifted.append("%s (%s)" % (name, ", ".join(stale)))
        machines.append(entry)

    listed = {str(m.get("machine") or "").lower() for m in machines}
    for row in retired or []:
        if not isinstance(row, dict) or is_service(str(row.get("agent") or "")):
            continue
        name = str(row.get("machine") or "").strip()
        if not name or name.lower() in listed or not is_kit(row):
            continue
        seen = _epoch(row.get("last_seen"))
        age = max(0.0, now - seen) if seen else None
        if age is None or age <= DORMANT_AFTER_S or age > DORMANT_KEEP_S:
            continue                                   # past the keep window it is history
        listed.add(name.lower())
        dormant.append(name)
        machines.append({"machine": name, "agent": str(row.get("agent") or "?"),
                         "state": "dormant", "seen_min_ago": int(age // 60), "artifacts": {},
                         "retired_row": True})

    graded = [m for m in machines if m.get("state") in ("current", "drifted")]
    if not pub:
        head = "nothing is published, so nothing was graded"
    elif not graded:
        head = "no online seat to grade"
    elif drifted:
        head = "%d of %d online seat(s) are NOT current: %s" % (len(drifted), len(graded),
                                                                "; ".join(drifted))
    else:
        head = "all %d online seat(s) run what this hub publishes" % len(graded)
    scope = []
    if offline:
        scope.append("%d silent, not graded: %s" % (len(offline), ", ".join(sorted(offline))))
    if dormant:
        scope.append("%d dormant past %d h, awaiting retire-or-return: %s"
                     % (len(set(dormant)), DORMANT_AFTER_S // 3600, ", ".join(sorted(set(dormant)))))
    if phantoms:
        scope.append("%d phantom caller(s) not counted" % len(phantoms))
    if legacy:
        scope.append("%d legacy row(s) not graded" % len(legacy))
    return {
        "published": {**{k: v["sha"] for k, v in pub.items()},
                      **({"python": "%d.%d" % tuple(required_python)[:2]} if required_python else {})},
        "machines": machines,
        "converged": bool(graded) and not drifted,
        "graded": len(graded),
        "drifted": drifted,
        "offline": sorted(offline),
        "dormant": sorted(set(dormant)),
        "phantoms": sorted(phantoms),
        "legacy": len(legacy),
        "verdict": head + ("" if not scope else " (" + "; ".join(scope) + ")"),
        "thresholds": {"offline_after_s": OFFLINE_AFTER_S, "dormant_after_s": DORMANT_AFTER_S,
                       "dark_after_s": DARK_AFTER_S, "drift_patience_s": DRIFT_PATIENCE_S,
                       "dormant_keep_s": DORMANT_KEEP_S},
    }


def observe_stale(report: dict, prior: dict | None, *, now: float | None = None) -> dict:
    """``{"machine:artifact": epoch first observed stale}`` for every stale pair in ``report``,
    carrying the prior clock forward (the adapter persists it between passes). A pair that is no
    longer stale is dropped, so a recurrence starts a fresh clock. Pure."""
    now = time.time() if now is None else now
    prior = prior if isinstance(prior, dict) else {}
    cur = {}
    for m in report.get("machines") or []:
        if m.get("state") != "drifted":
            continue
        for artifact in m.get("stale") or []:
            key = "%s:%s" % (m.get("machine"), artifact)
            try:
                cur[key] = float(prior.get(key) or now)
            except (TypeError, ValueError):
                cur[key] = now
    return cur


def drift_age_s(artifact: str, machine, pub: dict, first_stale: dict | None,
                now: float | None = None) -> float:
    """How long ``machine`` has had to converge on ``artifact``: the published file's age when
    there is a file, else how long this seat has been observed stale on it (0 when unknown --
    late, never early)."""
    now = time.time() if now is None else now
    meta = pub.get(artifact) or {}
    if meta.get("published_at"):
        return max(0.0, now - float(meta["published_at"]))
    since = (first_stale or {}).get("%s:%s" % (machine, artifact))
    try:
        return max(0.0, now - float(since)) if since else 0.0
    except (TypeError, ValueError):
        return 0.0


def inbox_items(report: dict, pub: dict, *, now: float | None = None,
                reply_cmd: str = "python -m hub_core.client distribution",
                first_stale: dict | None = None) -> list:
    """Operator inbox items: seats gone silent (kind ``offline``), seats DORMANT past
    DORMANT_AFTER_S (one retire-or-return item each), and persistent drift on an ONLINE seat
    (kind ``drift``). Ids are stable per machine so a long-lived condition is ONE item, not one
    per check."""
    now = time.time() if now is None else now
    as_of = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    out = []
    for m in report.get("machines") or []:
        seen_min = m.get("seen_min_ago")
        if m.get("state") == "dormant" and seen_min is not None:
            days = seen_min / 1440.0
            out.append({
                "kind": "offline", "id": "dormant:%s" % m["machine"], "from": "the hub",
                "title": "%s has been silent %.0f days: confirm it is retired, or turn it on"
                         % (m["machine"], days),
                "body": ("Seat %s (agent %s) has made no request to this hub for %.0f days. Past "
                         "%d hours it is no longer graded, but it is still enrolled: its credential "
                         "works and it still counts as a seat. If it is retired, forget its presence "
                         "row (python -m hub_core.client forget-presence --machine %s) and revoke "
                         "its credential; if not, turn it on and it catches up by itself."
                         % (m["machine"], m.get("agent") or "?", days, DORMANT_AFTER_S // 3600,
                            m["machine"])),
                "at": as_of, "as_of": as_of, "reply_cmd": reply_cmd,
            })
            continue
        if m.get("state") == "offline" and seen_min is not None and seen_min * 60 >= DARK_AFTER_S:
            hours = seen_min / 60.0
            out.append({
                "kind": "offline", "id": "dark:%s" % m["machine"], "from": "the hub",
                "title": "%s has not reported for %.0f h" % (m["machine"], hours),
                "body": ("Seat %s (agent %s) has made no request to this hub for %.0f hours. Every "
                         "seat converges by pulling, so one that is not calling home cannot be "
                         "reached or fixed remotely — this is the one condition that needs a person. "
                         "Anything the board shows about its consoles and artifacts is a %.0f-hour-old "
                         "snapshot, not a live reading." % (m["machine"], m.get("agent") or "?", hours, hours)),
                "at": as_of, "as_of": as_of, "reply_cmd": reply_cmd,
            })
            continue
        if m.get("state") != "drifted":
            continue
        overdue = [a for a in m.get("stale") or []
                   if drift_age_s(a, m.get("machine"), pub, first_stale, now) > DRIFT_PATIENCE_S]
        if not overdue:
            continue                                    # still inside normal propagation
        out.append({
            "kind": "drift", "id": "drift:%s" % m["machine"], "from": "the hub",
            "title": "%s is not running the current %s" % (m["machine"], ", ".join(overdue)),
            "body": ("Seat %s (agent %s) was seen %d min ago but still runs a stale %s, published "
                     "more than %d h ago — its update path is not converging. Computed %s from that "
                     "seat's report of %d min earlier; a seat that just updated reports it on its "
                     "next request, so read `distribution` live before acting."
                     % (m["machine"], m.get("agent") or "?", seen_min or 0, ", ".join(overdue),
                        DRIFT_PATIENCE_S // 3600, as_of, seen_min or 0)),
            "at": as_of, "as_of": as_of, "report_age_min": seen_min, "reply_cmd": reply_cmd,
        })
    return out
