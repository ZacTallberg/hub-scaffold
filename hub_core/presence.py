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
* PRESENCE MUST NOT WAKE THE FLEET. Every authenticated request observes presence; a write
  (lock + replace + mtime bump) that only moves a timestamp on a row seen seconds ago buys
  nothing and makes every waiter whose change signal watches presence re-fold. Such an
  observation is skipped while the row is younger than QUIET_REWRITE_S.
* A FOCUS SAYS WHAT, NOT WHICH. A bare entity id or a stub ("working", "…") is not a focus;
  it is rejected and the prior focus kept. A verb that declared a focus may RETRACT it, and a
  retraction only clears the focus it names — never a newer one another verb set.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .process_lock import ProcessFileLock

SESSION_ACTIVE_S = 900          # a console that prompted within 15 minutes is a live console
SESSION_KEEP_S = 1800           # a console quiet longer than this is closed, and is pruned
_PRUNE_INTERVAL_S = 900         # walk the presence dir at most this often on the write path
QUIET_REWRITE_S = 30            # a timestamp-only observation of a row this fresh is skipped
FILES_FRESH_S = 900             # a console's file list older than this is history
WORKING_S = 120                 # activity this recent reads as `working`, else `idle`
# The console fields a session carries, besides its stamps. One list, so the write merge, the
# read projection and the uniform row shape can never quietly disagree about what a session is.
SESSION_FIELDS = ("cwd", "focus", "name", "repo", "app", "state", "runtime")
_STUB_FOCUS = re.compile(r"^\s*(?:[a-z0-9_-]+:[a-z]+:[A-Za-z0-9._-]+|working|busy|idle|\W*)\s*$",
                         re.I)


def _dir(hub_dir) -> Path:
    return Path(hub_dir) / "presence"


def _retired_dir(hub_dir) -> Path:
    return _dir(hub_dir) / "_retired"


def _slug(s: str) -> str:
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


def valid_focus(focus: str) -> str:
    """The focus as stored, or "" when it says nothing a reader can use (a bare entity id, a
    one-word stub). A console's focus is the board's answer to "what is this window on"."""
    text = " ".join(str(focus or "").split())[:180]
    if len(text) < 6 or _STUB_FOCUS.match(text):
        return ""
    return text


def _session_merge(prior: dict, now: float, fields: dict, files, retract: str) -> dict:
    """One console's record after this observation: merge-never-clobber for every field (a
    heartbeat carries no prompt, so it must not blank the last known focus), a stub focus is
    rejected, and a retraction clears only the focus it names."""
    out = {k: prior.get(k, "") for k in SESSION_FIELDS}
    for key in SESSION_FIELDS:
        value = str(fields.get(key) or "").strip()
        if key == "focus":
            value = valid_focus(value)
        limit = 200 if key in ("cwd", "repo") else (180 if key == "focus" else 60)
        if value:
            out[key] = value[:limit]
    if retract and out.get("focus") and valid_focus(retract) == out["focus"] \
            and not valid_focus(fields.get("focus") or ""):
        out["focus"] = ""
    if files is not None:
        out["files"] = [str(f)[:160] for f in files if str(f).strip()][:12]
        out["files_at"] = now
    else:
        out["files"] = prior.get("files") or []
        out["files_at"] = prior.get("files_at") or 0
    out["at"] = now
    out["active_at"] = now
    return out


def _same_but_stamps(old: dict, new: dict) -> bool:
    stamps = {"at", "active_at", "files_at"}
    return {k: v for k, v in (old or {}).items() if k not in stamps} == \
        {k: v for k, v in (new or {}).items() if k not in stamps}


def observe(hub_dir, agent: str, *, machine: str = "", session: str = "", cwd: str = "",
            focus: str = "", heartbeat: bool = False, name: str = "", repo: str = "",
            app: str = "", state: str = "", runtime: str = "", files=None,
            retract_focus: str = "") -> None:
    """Record one observation of `agent`. Merge-never-clobber; keyed per (agent, machine);
    per-console sessions live INSIDE the machine row (a session is a fact about a machine),
    carrying its name, repo, app, state, focus and recently edited files. A heartbeat stamps
    heartbeat_at; anything else stamps activity_at. An observation that would only move stamps
    on a row seen within QUIET_REWRITE_S is skipped. Never raises."""
    agent = (agent or "").strip().lower()
    if not agent:
        return
    fields = {"cwd": cwd, "focus": focus, "name": name, "repo": repo, "app": app,
              "state": state, "runtime": runtime}
    try:
        pdir = _dir(hub_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        machine = (machine or "").strip().lower()[:120]
        stem = agent + ("--" + machine if machine else "")
        p = pdir / (_slug(stem) + ".json")
        sid = (session or "").strip()[:64]
        # THE QUIET PATH: no lock, no replace, no mtime bump when nothing but a stamp would move.
        try:
            prior_row = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_row = None
        if isinstance(prior_row, dict) and files is None:
            now = time.time()
            stamp_key = "heartbeat_at" if heartbeat else "activity_at"
            fresh = (now - epoch(prior_row.get("last_seen")) < QUIET_REWRITE_S
                     and now - epoch(prior_row.get(stamp_key)) < QUIET_REWRITE_S)
            sessions = prior_row.get("sessions") if isinstance(prior_row.get("sessions"), dict) else {}
            prior_session = sessions.get(sid) if sid else None
            unchanged = not sid or (isinstance(prior_session, dict) and _same_but_stamps(
                prior_session, _session_merge(prior_session, now, fields, None, retract_focus)))
            if fresh and unchanged and (not sid or now - epoch(prior_session.get("at")) < QUIET_REWRITE_S):
                return
        with ProcessFileLock(pdir, name=".presence.lock", timeout=5):
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
            if sid:
                sessions = payload.get("sessions")
                if not isinstance(sessions, dict):
                    sessions = {}
                prior = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
                sessions[sid] = _session_merge(prior, now, fields, files, retract_focus)
                # A console quiet past the keep window is closed. Without pruning this list
                # only grows and ends up reporting every window ever opened.
                cutoff = now - SESSION_KEEP_S
                payload["sessions"] = {
                    k: v for k, v in sessions.items()
                    if isinstance(v, dict) and epoch(v.get("at")) >= cutoff}
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
                (dict({f: (v or {}).get(f, "") for f in SESSION_FIELDS}, id=k,
                      files=list((v or {}).get("files") or []),
                      files_at=epoch((v or {}).get("files_at")),
                      at=epoch((v or {}).get("at")),
                      active_at=epoch((v or {}).get("active_at") or (v or {}).get("at")))
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


def session_row(agent: str, machine: str, s: dict, now: float) -> dict:
    """ONE console, in the one shape every reader gets — the same key set whether the console
    reported a name and files or only a cwd. A projection that re-lists fields per caller is
    the projection that silently drops the next one."""
    at = epoch(s.get("active_at") or s.get("at"))
    files_at = epoch(s.get("files_at"))
    files = [str(f)[:160] for f in (s.get("files") or [])][:12] \
        if (not files_at or now - files_at <= FILES_FRESH_S) else []
    state = str(s.get("state") or "") or ("working" if at and now - at < WORKING_S else "idle")
    return {
        "agent": agent, "machine": machine or "",
        "session": str(s.get("id") or "")[:8],
        "name": str(s.get("name") or "")[:40],
        "runtime": str(s.get("runtime") or "")[:16],
        "cwd": str(s.get("cwd") or "")[:200],
        "repo": str(s.get("repo") or "")[:200],
        "app": str(s.get("app") or "")[:60],
        "focus": str(s.get("focus") or "")[:180],
        "state": state[:16],
        "files": files,
        "active_at": at or None,
        "age_s": round(now - at) if at else None,
    }


def live_sessions(hub_dir, now: float | None = None) -> list:
    """Every ACTIVE console across the fleet, newest first, in the uniform session_row shape.
    The per-agent roll-up keeps one focus per name and turns the rest into a count; this is
    the flat per-console view, so each session can see its siblings — the surface that stops
    two consoles from unknowingly working the same thing."""
    now = time.time() if now is None else now
    out = []
    for agent, seen in read(hub_dir).items():
        if is_service_identity(agent):
            continue
        for m in seen.get("machines") or []:
            for s in m.get("sessions") or []:
                at = epoch(s.get("active_at") or s.get("at"))
                if not (s.get("focus") or s.get("cwd") or s.get("name") or s.get("repo")):
                    continue
                if not at or (now - at) > SESSION_ACTIVE_S:
                    continue
                out.append(session_row(agent, m.get("machine") or "", s, now))
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
