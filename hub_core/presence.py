"""Observed worker presence: who is on this board right now, from which machine, in which
console, doing what. Framework-free; every function takes the hub dir explicitly (the
telemetry/cost idiom).

Presence is a SIDECAR like claims/ — observed runtime state, never ledger truth. A row is
written by the write seam whenever an authenticated agent touches the hub (activity), and by
the optional presence ping (a heartbeat). The distinction matters: a request proves an agent
ACTED; only a repeating heartbeat proves the seat is still there after the request returned.
The freshness contract below is shared by the API and the board so the two can never quietly
disagree about what online, degraded, and offline mean.

Rules this module holds, each paid for in production on the origin system:

* MERGE, NEVER CLOBBER. A bare request carries no machine/session detail, and overwriting a
  row with blanks erases what a richer request reported a minute earlier.
* ONE PERSON, SEVERAL MACHINES. Rows key on (agent, machine); a single per-agent row flips
  between computers and hides a stale one behind a fresh one.
* PER-CONSOLE SESSIONS. Agent + machine cannot tell four live consoles on one computer from
  one console checking in often — a session id + its focus can, and two consoles unknowingly
  working the same thing is the most expensive duplicate a fleet produces.
* COERCE EVERY STORED TIMESTAMP. Sidecar files accumulate shapes over years; one row whose
  stamp is a string must degrade to "no time known", never raise a TypeError that takes every
  board view to a 500.
* SELF-RETIRE, ARCHIVE OVER DELETE. A machine that stopped reporting keeps its row forever
  otherwise, and the fleet panel slowly becomes a museum. Rows unseen past the horizon move
  to _retired/ (evidence is never lost) and reappear the instant the machine checks in again.
* PRESENCE MUST NEVER BREAK A REQUEST. Every write path swallows I/O errors.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .process_lock import ProcessFileLock

SESSION_ACTIVE_S = 900          # a console that prompted within 15 minutes is a live console
SESSION_KEEP_S = 1800           # a console quiet longer than this is closed, and is pruned
_PRUNE_INTERVAL_S = 900         # walk the presence dir at most this often on the write path


def _dir(hub_dir) -> Path:
    return Path(hub_dir) / "presence"


def _retired_dir(hub_dir) -> Path:
    return _dir(hub_dir) / "_retired"


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9._-]+", "-", str(s or "").lower()).strip("-") or "agent"


def epoch(value) -> float:
    """A stored timestamp as a number, whatever shape it was written in. An unparseable
    stamp degrades to 0.0 ("never seen") rather than raising — diagnostic data must never
    be able to take the board down."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        pass
    try:
        import datetime as _dt
        parsed = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def contract() -> dict:
    """The shared freshness contract. A heartbeat every HUB_PRESENCE_INTERVAL_S seconds
    permits two delayed frames before degrading and six missed frames before the computer is
    declared offline. Activity is a separate signal: a request proves the agent acted, never
    that the seat remains online after it returned."""
    try:
        interval = int(os.environ.get("HUB_PRESENCE_INTERVAL_S", "15"))
    except (TypeError, ValueError):
        interval = 15
    interval = max(5, min(interval, 60))
    return {
        "heartbeat_interval_s": interval,
        "online_after_s": max(30, interval * 3),
        "offline_after_s": max(90, interval * 6),
        "activity_active_s": 120,
    }


def _retire_after_s() -> int:
    """A row unseen this long is a seat that left the fleet, not one idle for an afternoon.
    Floored at an hour so a live-but-quiet machine is never at risk."""
    try:
        v = int(os.environ.get("HUB_PRESENCE_RETIRE_AFTER_S", "259200"))
    except (TypeError, ValueError):
        v = 259200
    return max(3600, v)


def _prune_locked(hub_dir, now: float, force: bool = False) -> int:
    """Archive rows unseen past the retirement horizon. THE CALLER HOLDS THE LOCK. Throttled
    by a marker so the common write path pays one stat, not a directory walk."""
    pdir = _dir(hub_dir)
    marker = pdir / ".last-prune"
    if not force:
        try:
            if now - float(marker.read_text(encoding="utf-8")) < _PRUNE_INTERVAL_S:
                return 0
        except (OSError, ValueError):
            pass
    horizon = _retire_after_s()
    try:
        _retired_dir(hub_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    removed = 0
    for p in pdir.glob("*.json"):           # non-recursive: never re-scans _retired/
        try:
            row = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if now - epoch(row.get("last_seen")) <= horizon:
            continue
        try:
            os.replace(str(p), str(_retired_dir(hub_dir) / p.name))   # archive over delete
            removed += 1
        except OSError:
            pass
    try:
        marker.write_text(str(now), encoding="utf-8")
    except OSError:
        pass
    return removed


#: Session fields a client MAY report beyond cwd/focus, each bounded. Kind/run/subject tell an
#: unattended run from a person's console; project/files feed the crossover detector; the digest
#: fields (phase .. last_result) are what a supervisor distilled from the session's own activity,
#: so the board can say "idle 12 min, last did X" instead of stamping every open window "active".
SESSION_FIELDS = {"kind": 16, "run": 64, "subject": 120, "subject_title": 160, "project": 80,
                  "runtime": 16, "phase": 16, "doing": 180, "narration": 220,
                  "last_result": 60, "outcome": 24, "state": 16}
SESSION_NUMBERS = ("started", "ended", "bounded_s", "doing_at")
UNATTENDED_KINDS = ("responder", "scheduled", "autoworker", "unattended")
UNATTENDED_RECAP_S = 1800       # a finished run stays visible as a recap this long, then goes


def _clean_session_extra(extra) -> dict:
    """Bound and type every optional session field; unknown keys are dropped, never stored."""
    extra = extra if isinstance(extra, dict) else {}
    out = {}
    for key, limit in SESSION_FIELDS.items():
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:limit]
    for key in SESSION_NUMBERS:
        try:
            value = float(extra.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            out[key] = value
    for key, limit, cap in (("files", 160, 12), ("targets", 60, 4)):
        value = extra.get(key)
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list):
            out[key] = [str(v).strip()[:limit] for v in value if str(v).strip()][:cap]
    if "files" in out:
        out["files_at"] = time.time()
    return out


def observe(hub_dir, agent: str, *, machine: str = "", session: str = "", cwd: str = "",
            focus: str = "", heartbeat: bool = False, extra: dict | None = None,
            client: str = "") -> None:
    """Record one observation of `agent`. Merge-never-clobber; keyed per (agent, machine);
    per-console sessions live INSIDE the machine row (a session is a fact about a machine).
    A heartbeat stamps heartbeat_at; anything else stamps activity_at. ``extra`` carries the
    optional session fields (SESSION_FIELDS); ``client`` the reporting client's version, kept
    per machine so a seat running an older client than the hub serves is visible. Never raises."""
    agent = (agent or "").strip().lower()
    if not agent:
        return
    try:
        pdir = _dir(hub_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(pdir, name=".presence.lock", timeout=5):
            machine = (machine or "").strip().lower()[:120]
            stem = agent + ("--" + machine if machine else "")
            p = pdir / (_slug(stem) + ".json")
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            # The filename is the identity boundary: an agent-only write must stay
            # agent-only even when migrating a legacy payload that retained a machine.
            if not machine:
                payload.pop("machine", None)
            else:
                payload["machine"] = machine
            payload["agent"] = agent
            now = time.time()
            payload["last_seen"] = now
            if heartbeat:
                payload["heartbeat_at"] = now
            else:
                payload["activity_at"] = now
            sid = (session or "").strip()[:64]
            if sid:
                sessions = payload.get("sessions")
                if not isinstance(sessions, dict):
                    sessions = {}
                prior = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
                # A heartbeat carries no prompt, so it must not blank the last known focus —
                # keep the prior one until a new prompt replaces it (merge-never-clobber).
                merged = {k: v for k, v in prior.items()
                          if k in SESSION_FIELDS or k in SESSION_NUMBERS
                          or k in ("files", "files_at", "targets")}
                fresh = _clean_session_extra(extra)
                merged.update(fresh)
                merged.update({
                    "cwd": (cwd or "").strip()[:200] or prior.get("cwd", ""),
                    "focus": (focus or "").strip()[:180] or prior.get("focus", ""),
                    "at": now,
                })
                if fresh.get("doing") or fresh.get("phase"):
                    merged["doing_at"] = now
                sessions[sid] = merged
                # A console quiet past the keep window is closed. Without pruning this list
                # only grows and ends up reporting every window ever opened. A finished
                # unattended run is kept for its recap window, measured from when it ENDED.
                cutoff = now - SESSION_KEEP_S
                payload["sessions"] = {
                    k: v for k, v in sessions.items()
                    if isinstance(v, dict)
                    and max(epoch(v.get("at")), epoch(v.get("ended"))) >= cutoff}
            if client:
                payload["client"] = str(client).strip()[:64]
                payload["client_at"] = now
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, p)
            # Self-cleaning under the same lock: the write that records a live seat retires
            # dead ones, so the panel is a picture of the CURRENT fleet.
            _prune_locked(hub_dir, now)
    except (OSError, TimeoutError):
        pass  # presence must never break a request


def rows(hub_dir) -> list:
    """Every stored (agent, machine) row. A malformed file is skipped, never fatal."""
    out = []
    try:
        for p in _dir(hub_dir).glob("*.json"):
            try:
                row = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(row, dict) and row.get("agent"):
                out.append(row)
    except OSError:
        pass
    return out


def read(hub_dir) -> dict:
    """Per-agent view over the per-(agent, machine) rows: {agent: {agent, last_seen,
    machines: [...]}}. Machine rows keep every stored field except bookkeeping (DENYLIST,
    not allowlist — the projection that re-lists fields by hand is the projection that
    silently drops the next one), and sessions are reshaped to a newest-first list."""
    by_agent = {}
    for row in rows(hub_dir):
        by_agent.setdefault(row["agent"], []).append(row)
    out = {}
    for agent, agent_rows in by_agent.items():
        agent_rows.sort(key=lambda r: epoch(r.get("last_seen")), reverse=True)
        top = {"agent": agent, "last_seen": epoch(agent_rows[0].get("last_seen")) or None}
        # Liveness stamps are TIMES, so the freshest is the MAX across rows — not the first
        # non-empty one, which would pick an arbitrary row's stamp.
        for k in ("heartbeat_at", "activity_at"):
            stamps = [epoch(r.get(k)) for r in agent_rows if epoch(r.get(k))]
            if stamps:
                top[k] = max(stamps)
        machines, seen = [], set()
        for r in agent_rows:
            name = (r.get("machine") or "").strip().lower()
            key = name or "-"
            if key in seen:
                continue
            seen.add(key)
            entry = {k: v for k, v in r.items()
                     if k not in ("agent", "sessions") and not k.startswith("_")}
            entry.setdefault("machine", name)
            sess = r.get("sessions")
            entry["sessions"] = sorted(
                (dict(v, id=k, cwd=v.get("cwd", ""), focus=v.get("focus", ""),
                      at=epoch(v.get("at")))
                 for k, v in (sess or {}).items() if isinstance(v, dict)),
                key=lambda s: s.get("at") or 0, reverse=True) if isinstance(sess, dict) else []
            machines.append(entry)
        top["machines"] = machines
        out[agent] = top
    return out


def device_state(row: dict, now: float | None = None) -> dict:
    """Truthful connection/activity ages for one row (agent- or machine-level)."""
    now = time.time() if now is None else now
    c = contract()

    def age(key):
        stamp = epoch(row.get(key))
        return round(max(0, now - stamp)) if stamp else None

    heartbeat_age = age("heartbeat_at")
    activity_age = age("activity_at")
    last_seen_age = age("last_seen")
    if heartbeat_age is not None:
        if heartbeat_age <= c["online_after_s"]:
            connection = "online"
        elif heartbeat_age <= c["offline_after_s"]:
            connection = "degraded"
        else:
            connection = "offline"
    elif activity_age is not None and activity_age <= c["activity_active_s"]:
        # A request just proved activity, not continuous liveness after it returned.
        connection = "activity-only"
    elif row.get("machine") or row.get("agent"):
        connection = "unverified"
    else:
        connection = "never"
    return {
        "connection": connection,
        "heartbeat_age_s": heartbeat_age,
        "activity_age_s": activity_age,
        "last_seen_age_s": last_seen_age,
        "active": activity_age is not None and activity_age <= c["activity_active_s"],
    }


def session_kind(s: dict) -> str:
    """``attended`` for a person's console; otherwise the unattended lane that started it. A
    separate axis from state: a finished run is still unattended, an idle person still a person."""
    kind = str((s or {}).get("kind") or "").strip().lower()
    return kind if kind in UNATTENDED_KINDS else "attended"


def _phrase(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    return "%d h" % (seconds // 3600)


def live_sessions(hub_dir, now: float | None = None) -> list:
    """Every live console across the fleet, newest first: agent, machine, session id, cwd,
    focus, age, and what it is DOING. The per-agent roll-up keeps one focus per name and turns
    the rest into a count; this is the flat per-console view, so each session can see its
    siblings — the surface that stops two consoles from unknowingly working the same thing.

    State is honest: ``working`` while the console acted in the last two minutes, else ``idle``
    with ``activity`` reading "idle 12 min, last did …" — a window that is merely OPEN is not a
    console that is working. An unattended run that ENDED stays listed as ``done`` for
    UNATTENDED_RECAP_S (how it ended is the point), aged from when it ended, and never rides its
    last "working" stamp back into the live list."""
    now = time.time() if now is None else now
    out = []
    for agent, seen in read(hub_dir).items():
        for m in seen.get("machines") or []:
            for s in m.get("sessions") or []:
                at = epoch(s.get("at"))
                kind = session_kind(s)
                ended = epoch(s.get("ended"))
                finished = kind != "attended" and (bool(ended) or s.get("state") == "done")
                if not s.get("focus") and not s.get("cwd") and kind == "attended":
                    continue
                if finished:
                    anchor = ended or at
                    if not anchor or (now - anchor) > UNATTENDED_RECAP_S:
                        continue
                elif s.get("state") == "gone" or not at or (now - at) > SESSION_ACTIVE_S:
                    continue
                idle_for = round(now - at) if at else None
                if finished:
                    state = "done"
                elif s.get("state") == "waiting":
                    state = "waiting"
                else:
                    state = "working" if idle_for is not None and idle_for < 120 else "idle"
                did = str(s.get("doing") or s.get("narration") or s.get("focus") or "")[:120]
                if state == "idle":
                    activity = "idle %s%s" % (_phrase(idle_for), (", last did " + did) if did else "")
                elif state == "done":
                    activity = "finished %s ago%s" % (
                        _phrase(now - (ended or at)),
                        (" — " + str(s.get("outcome"))) if s.get("outcome") else "")
                else:
                    activity = did or state
                files_at = epoch(s.get("files_at"))
                out.append({"agent": agent, "machine": m.get("machine") or "",
                            "session": str(s.get("id") or "")[:8],
                            "cwd": str(s.get("cwd") or "")[:64],
                            "focus": str(s.get("focus") or "")[:100],
                            "project": str(s.get("project") or "")[:80],
                            # A file list older than the activity window is history, not a
                            # crossover: pairing on edits from days ago is noise.
                            "files": (list(s.get("files") or [])[:12]
                                      if files_at and now - files_at <= SESSION_ACTIVE_S else []),
                            "kind": kind, "unattended": kind != "attended",
                            "run": str(s.get("run") or "")[:64],
                            "subject": str(s.get("subject") or "")[:120],
                            "subject_title": str(s.get("subject_title") or "")[:160],
                            "runtime": str(s.get("runtime") or "")[:16],
                            "phase": str(s.get("phase") or "")[:16],
                            "doing": str(s.get("doing") or "")[:180],
                            "narration": str(s.get("narration") or "")[:220],
                            "last_result": str(s.get("last_result") or "")[:60],
                            "targets": list(s.get("targets") or [])[:4],
                            "outcome": str(s.get("outcome") or "")[:24],
                            "started": epoch(s.get("started")) or None,
                            "ended": ended or None,
                            "bounded_s": int(epoch(s.get("bounded_s"))) or None,
                            "state": state, "finished": finished, "activity": activity,
                            "idle_for_s": idle_for,
                            "age_s": round(now - (ended if finished and ended else at))})
    out.sort(key=lambda x: x.get("age_s") if x.get("age_s") is not None else 10 ** 9)
    return out


def split(rows: list) -> dict:
    """``{"attended", "unattended", "finished"}`` from one list of console rows. ``unattended``
    is exactly the runs that are LIVE (working, idle or waiting); an ended run is a recap in
    ``finished``, never a row in the live section. Live runs sort working first, then newest."""
    attended = [r for r in rows or [] if not r.get("unattended")]
    rest = [r for r in rows or [] if r.get("unattended")]
    finished = [r for r in rest if r.get("finished")]
    unattended = [r for r in rest if not r.get("finished")]
    unattended.sort(key=lambda r: (0 if r.get("state") == "working" else 1, r.get("age_s") or 0))
    finished.sort(key=lambda r: r.get("age_s") or 0)
    return {"attended": attended, "unattended": unattended, "finished": finished}


def attribute_leases(sessions: list, leases: list) -> list:
    """Bind each live console to the task lease IT holds — never to one inferred from a path.

    A claim asserts responsibility, so attributing it by directory ("this console is standing
    in the repo that task is about") turns anyone who opens a repository to read a file into
    its owner — a confident board that is wrong, which is worse than an empty one. The rule:

    * CERTAIN — the lease records the claiming session and it is this console's session.
    * LEGACY — a lease written without a session (an older client) is attributed only when the
      agent has exactly ONE live console it could belong to. With several, the honest answer
      is that the hub does not know, and nothing is attributed.

    Returns new session rows carrying ``task_id`` (or "") and ``has_task``; the input is not
    mutated. Pure: sessions are ``live_sessions`` rows, leases are live lease records."""
    by_agent = {}
    for s in sessions or []:
        by_agent.setdefault(str(s.get("agent") or "").lower(), []).append(s)
    held = {}
    for lease in leases or []:
        agent = str((lease or {}).get("agent") or "").lower()
        if agent and lease.get("task"):
            held.setdefault(agent, []).append(lease)
    out = []
    for agent, rows_ in by_agent.items():
        mine = held.get(agent, [])
        for s in rows_:
            sid = str(s.get("session") or "")[:8]
            tid = next((lease["task"] for lease in mine
                        if sid and str(lease.get("session") or "")[:8] == sid), "")
            if not tid and len(rows_) == 1:
                tid = next((lease["task"] for lease in mine if not lease.get("session")), "")
            row = dict(s)
            row.update({"task_id": tid, "has_task": bool(tid)})
            out.append(row)
    out.sort(key=lambda x: x.get("age_s") if x.get("age_s") is not None else 10 ** 9)
    return out


def service_identities() -> set:
    """Agents that are automation, not somebody's seat: they authenticate and write, but
    must never appear on the fleet's device roster (a row that can never heartbeat skews
    every denominator forever). Extend without a deploy via HUB_SERVICE_IDENTITIES."""
    extra = {s.strip().lower()
             for s in (os.environ.get("HUB_SERVICE_IDENTITIES") or "").split(",") if s.strip()}
    return {"ci"} | extra


def is_service_identity(agent: str) -> bool:
    return (agent or "").strip().lower() in service_identities()


def forget(hub_dir, machine: str = "", agent: str = "") -> int:
    """Drop rows for a machine and/or agent — the remedy for a phantom device. Refuses to
    drop everything (both filters empty)."""
    machine = (machine or "").strip().lower()
    agent = (agent or "").strip().lower()
    if not machine and not agent:
        return 0
    removed = 0
    try:
        with ProcessFileLock(_dir(hub_dir), name=".presence.lock", timeout=5):
            for p in _dir(hub_dir).glob("*.json"):
                try:
                    row = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if machine and (row.get("machine") or "").lower() != machine:
                    continue
                if agent and (row.get("agent") or "").lower() != agent:
                    continue
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    except (OSError, TimeoutError):
        pass
    return removed


def stamp(hub_dir) -> tuple:
    """Cheap change fingerprint over the presence sidecar (count + max mtime), so the live
    stream can push a 'presence' tick the moment any seat phones in — presence writes never
    touch the ledger, which is exactly why a fleet strip that watches only ledger events
    updates only when something unrelated happens to move."""
    try:
        stamps = [p.stat().st_mtime_ns for p in _dir(hub_dir).glob("*.json")]
        return (len(stamps), max(stamps, default=0))
    except OSError:
        return (0, 0)
