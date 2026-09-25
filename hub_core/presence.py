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
* WHERE A CONSOLE STANDS IS ONE FACT. The working directory and the repository it sits in
  describe the same place, so a report that names a cwd names the repo WITH it -- empty
  included. "Empty never clobbers" applied field by field kept a console's old repo for its
  whole life once it moved to a directory with no repository (a client omits an empty
  header), and the board went on attributing it to work that had ended days earlier. A
  report that names no cwd still never clobbers either.
* A FILE LIST IS A SNAPSHOT, NOT A STATE. Clients report the files a console touched
  recently and omit the list when there are none, so a stored list would otherwise live for
  ever and keep pairing consoles on edits from days ago. Every list carries the time it was
  reported (files_at) and reads as empty once it is older than FILES_FRESH_S.
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

from . import atomic
from .text import preview
from .process_lock import ProcessFileLock

SESSION_ACTIVE_S = 900          # a console that prompted within 15 minutes is a live console
SESSION_KEEP_S = 1800           # a console quiet longer than this is closed, and is pruned
MAX_FILES = 24                  # a console's recent-files list is a hint, never an inventory
_PRUNE_INTERVAL_S = 900         # walk the presence dir at most this often on the write path
QUIET_REWRITE_S = 30            # a timestamp-only observation of a row this fresh is skipped
FILES_FRESH_S = 600             # a console's file list older than ten minutes is history
WORKING_S = 120                 # activity this recent reads as `working`, else `idle`
# The console fields a session carries, besides its stamps. One list, so the write merge, the
# read projection and the uniform row shape can never quietly disagree about what a session is.
CONSOLE_FIELDS = ("cwd", "focus", "name", "repo", "app", "state", "runtime")
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
    """The shared freshness contract. A heartbeat every HUB_PRESENCE_INTERVAL_S seconds (60 by
    default)
    permits two delayed frames before degrading and six missed frames before the computer is
    declared offline. Activity is a separate signal: a request proves the agent acted, never
    that the seat remains online after it returned."""
    try:
        interval = int(os.environ.get("HUB_PRESENCE_INTERVAL_S", "60"))
    except (TypeError, ValueError):
        interval = 60
    # 60 s by default, not 15: every seat's heartbeat is a request the hub must serve, and on a
    # hub already short of threads a fleet of 15 s beacons is a measurable share of its load for
    # no gain in truth (online/offline are derived from the interval, so they stay honest).
    interval = max(5, min(interval, 300))
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
            atomic.replace(p, _retired_dir(hub_dir) / p.name)   # archive over delete
            removed += 1
        except OSError:
            pass
    try:
        marker.write_text(str(now), encoding="utf-8")
    except OSError:
        pass
    return removed


FILES_WINDOW_S = FILES_FRESH_S   # an edit older than ten minutes is history, not shared work
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_focus(text: str) -> str:
    """A focus line is read by every other console: an address pasted into a prompt must
    not ride it there."""
    return _EMAIL.sub("[email]", str(text or ""))


#: Session fields a client MAY report beyond cwd/focus, each bounded. Kind/run/subject tell an
#: unattended run from a person's console; project/files feed the crossover detector; the digest
#: fields (phase .. last_result) are what a supervisor distilled from the session's own activity,
#: so the board can say "idle 12 min, last did X" instead of stamping every open window "active".
SESSION_FIELDS = {"kind": 16, "run": 64, "subject": 120, "subject_title": 160, "project": 80,
                  "runtime": 16, "phase": 16, "doing": 180, "narration": 220,
                  "last_result": 60, "outcome": 24, "state": 16}
SESSION_NUMBERS = ("started", "ended", "bounded_s", "doing_at")
#: Facts a seat reports about the computer itself, kept on its machine row: the interpreter the
#: client runs on (``X-Hub-Python``) and whether this machine can push (``X-Hub-Push``: yes | no
#: | unknown, from the adopter's non-interactive push probe). name -> max length.
MACHINE_FACTS = {"python": 24, "push": 8}
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


def valid_focus(focus: str) -> str:
    """The focus as stored, or "" when it says nothing a reader can use (a bare entity id, a
    one-word stub). A console's focus is the board's answer to "what is this window on"."""
    text = preview(focus, 500)
    if len(text) < 6 or _STUB_FOCUS.match(text):
        return ""
    return text


def _session_merge(prior: dict, now: float, fields: dict, files, retract: str,
                   extra: dict | None = None) -> dict:
    """One console's record after this observation: merge-never-clobber for every field (a
    heartbeat carries no prompt, so it must not blank the last known focus), a stub focus is
    rejected, and a retraction clears only the focus it names."""
    out = {k: prior.get(k, "") for k in CONSOLE_FIELDS}
    for key in CONSOLE_FIELDS:
        value = str(fields.get(key) or "").strip()
        if key == "focus":
            value = valid_focus(value)
        limit = 400 if key == "cwd" else 200 if key == "repo" else (500 if key == "focus" else 60)
        if value:
            out[key] = value[:limit]
    # WHERE A CONSOLE STANDS IS ONE FACT: a report naming a cwd names the repo with it, empty
    # included ("this directory is in no repository"); only a report with no cwd leaves both.
    if str(fields.get("cwd") or "").strip():
        out["repo"] = str(fields.get("repo") or "").strip()[:200]
    if retract and out.get("focus") and valid_focus(retract) == out["focus"] \
            and not valid_focus(fields.get("focus") or ""):
        out["focus"] = ""
    if files is not None:
        out["files"] = [str(f)[:160] for f in files if str(f).strip()][:12]
        out["files_at"] = now
    else:
        out["files"] = prior.get("files") or []
        out["files_at"] = prior.get("files_at") or 0
    # The optional session fields (kind/run/subject, the supervisor's digest, the crossover
    # project) merge the same way: kept until a newer report replaces them.
    for key, value in prior.items():
        if (key in SESSION_FIELDS or key in SESSION_NUMBERS or key == "targets") \
                and key not in CONSOLE_FIELDS:
            out[key] = value
    fresh = _clean_session_extra(extra)
    fresh.pop("files", None)
    fresh.pop("files_at", None)
    out.update(fresh)
    if fresh.get("doing") or fresh.get("phase"):
        out["doing_at"] = now
    out["at"] = now
    out["active_at"] = now
    return out


def _same_but_stamps(old: dict, new: dict) -> bool:
    stamps = {"at", "active_at", "files_at", "doing_at"}
    return {k: v for k, v in (old or {}).items() if k not in stamps} == \
        {k: v for k, v in (new or {}).items() if k not in stamps}


# What only a seat running the client kit reports. Presence is written for ANY authenticated
# caller that sets X-Hub-Machine (a probe, a script borrowing a token), so a row that names a
# machine but carries none of these is a CALLER, not a computer: listing it as a device puts a
# phantom "never checked in" machine on somebody's card and a false "behind" on the distribution
# view. One rule, read by every device view.
KIT_TELEMETRY = ("client", "artifacts")
FILES_MAX = 12


def is_kit_machine(row) -> bool:
    """True when a presence row is a computer running the client kit, not a bare caller."""
    return bool(isinstance(row, dict) and str(row.get("machine") or "").strip()
                and any(row.get(k) for k in KIT_TELEMETRY))


def parse_artifacts(header: str) -> dict:
    """`name=sha,name=sha` (X-Hub-Artifacts) -> {name: sha}; malformed pairs are dropped."""
    import re
    out = {}
    for part in str(header or "").split(",")[:32]:
        name, sep, sha = part.partition("=")
        name, sha = name.strip().lower()[:40], sha.strip()[:80]
        if sep and name and re.fullmatch(r"[a-z0-9._-]+", name) and sha:
            out[name] = sha
    return out


def parse_files(raw) -> list | None:
    """A reported file list from a header value (comma/newline separated) or a list.

    None means NOT REPORTED (keep what is stored); an empty list is a report of no files.
    Each token is a `<project>/<path>`, slash-normalized and deduplicated in order; a token
    without a project segment is dropped (two consoles in different repos editing `src/views.py`
    are not editing one file), and so is one that climbs out with `..`. Bounded."""
    if raw is None:
        return None
    items = raw if isinstance(raw, (list, tuple)) else str(raw).replace(chr(10), ",").split(",")
    out = []
    for item in items:
        rel = str(item or "").strip().replace(chr(92), "/").strip("/")[:240]
        if rel and "/" in rel and ".." not in rel.split("/") and rel not in out:
            out.append(rel)
        if len(out) >= FILES_MAX:
            break
    return out


def fresh_files(session: dict, now: float | None = None) -> list:
    """The session's file list if it is still current, else []. A list with no stamp was
    written before stamps existed and cannot be dated, so it is treated as history too."""
    now = time.time() if now is None else now
    stamp = epoch((session or {}).get("files_at"))
    if not stamp or now - stamp > FILES_FRESH_S:
        return []
    files = (session or {}).get("files")
    return [str(f) for f in files] if isinstance(files, list) else []


def observe(hub_dir, agent: str, *, machine: str = "", session: str = "", cwd: str = "",
            focus: str = "", heartbeat: bool = False, name: str = "", repo: str = "",
            app: str = "", state: str = "", runtime: str = "", files=None,
            retract_focus: str = "", extra: dict | None = None, client: str = "",
            client_digest: str = "", artifacts=None, project: str = "",
            unattended=None, memory_health: str = "",
            machine_facts: dict | None = None) -> None:
    """Record one observation of `agent`. Merge-never-clobber; keyed per (agent, machine);
    per-console sessions live INSIDE the machine row (a session is a fact about a machine),
    carrying its name, repo, app, state, focus and recently edited files. A heartbeat stamps
    heartbeat_at; anything else stamps activity_at. An observation that would only move stamps
    on a row seen within QUIET_REWRITE_S is skipped. ``extra`` carries the optional session
    fields (SESSION_FIELDS: kind, run, subject, the supervisor's digest, project, files);
    ``client`` the reporting client's version, kept per machine so a seat running an older
    client than the hub serves is visible. ``project`` and ``unattended`` are the
    crossover facts a client may pass directly (the same as extra project / kind).
    ``machine_facts`` are what a seat reports about the computer itself (MACHINE_FACTS: its
    interpreter version, whether it can push), kept on the machine row and graded by
    hub_core.distribution. A focus line is read by every other console, so an e-mail address
    pasted into it is redacted. Never raises."""
    agent = (agent or "").strip().lower()
    if not agent:
        return
    fields = {"cwd": cwd, "focus": preview(redact_focus(focus), 500), "name": name, "repo": repo, "app": app,
              "state": state, "runtime": runtime}
    if project or unattended:
        extra = dict(extra or {})
        if project:
            extra.setdefault("project", str(project).strip().lower())
        if unattended and not extra.get("kind"):
            extra["kind"] = "unattended"
    if files is None and isinstance(extra, dict) and extra.get("files"):
        files = _clean_session_extra({"files": extra.get("files")}).get("files")
    client = str(client or "").strip()[:80]
    client_digest = str(client_digest or "").strip()[:64]
    # X-Hub-Memory-Health: a fact about the MACHINE's local memory layer (knowledge_mirror).
    memory_health = str(memory_health or "").strip()[:240]
    facts = {k: str(v).strip()[:limit] for k, limit in MACHINE_FACTS.items()
             for v in [(machine_facts or {}).get(k)] if v not in (None, "")} if machine else {}
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
                prior_session, _session_merge(prior_session, now, fields, None, retract_focus,
                                              extra)))
            if (client and prior_row.get("client") != client) or (
                    client_digest and prior_row.get("client_digest") != client_digest) or artifacts:
                unchanged = False
            if memory_health and _health_moved(prior_row.get("memory_health"), memory_health):
                unchanged = False
            if any(prior_row.get(k) != v for k, v in facts.items()):
                unchanged = False
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
            # Kit telemetry is a fact about the MACHINE row, and only a machine row can carry it
            # (an agent-only row is a legacy aggregate, never a device).
            if machine and artifacts:
                prior_art = payload.get("artifacts")
                payload["artifacts"] = {**(prior_art if isinstance(prior_art, dict) else {}),
                                        **dict(artifacts)}
                payload["telemetry_at"] = now
            if sid:
                sessions = payload.get("sessions")
                if not isinstance(sessions, dict):
                    sessions = {}
                prior = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
                sessions[sid] = _session_merge(prior, now, fields, files, retract_focus, extra)
                # A console quiet past the keep window is closed. Without pruning this list
                # only grows and ends up reporting every window ever opened. A finished
                # unattended run is kept for its recap window, measured from when it ENDED.
                cutoff = now - SESSION_KEEP_S
                payload["sessions"] = {
                    k: v for k, v in sessions.items()
                    if isinstance(v, dict)
                    and max(epoch(v.get("at")), epoch(v.get("ended"))) >= cutoff}
            # The client's version+sha (X-Hub-Client, kit telemetry: a fact about the MACHINE
            # row, so an agent-only legacy row never carries it) and the digest the hub compares
            # against the client it serves (X-Hub-Client-Version, the stale-seat detector).
            if machine and client:
                payload["client"] = client
            if client_digest:
                payload["client_digest"] = client_digest
            if client or client_digest:
                payload["client_at"] = now
            if machine and memory_health:
                payload["memory_health"] = memory_health
                payload["memory_health_at"] = now
            # What the seat reports about the computer (a fact about the MACHINE row).
            payload.update(facts)
            # Atomic and durable, waiting out a Windows sharing window (hub_core.atomic).
            atomic.write_json(p, payload)
            # Self-cleaning under the same lock: the write that records a live seat retires
            # dead ones, so the panel is a picture of the CURRENT fleet.
            _prune_locked(hub_dir, now)
    except (OSError, TimeoutError):
        pass  # presence must never break a request


def _health_moved(prior, line: str) -> bool:
    """A memory-health line changed in substance -- its `age` part ticks every second and must
    not by itself defeat the quiet path (a rewrite per request)."""
    def core(text):
        return ";".join(p for p in str(text or "").split(";") if not p.startswith("age="))
    return core(prior) != core(line)


def retired_rows(hub_dir) -> list:
    """Rows presence retirement archived (``_retired/``). Retirement archives, it never deletes,
    and a seat that stopped calling home is still enrolled until a person forgets it -- so the
    distribution view keeps listing these as dormant. Never raises."""
    out = []
    try:
        for p in sorted(_retired_dir(hub_dir).glob("*.json")):
            try:
                row = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(row, dict) and row.get("agent"):
                out.append(row)
    except OSError:
        pass
    return out


_ROWS_MEMO = {"key": None, "rows": None, "at": 0.0}
ROWS_TTL_S = 2.0


def rows(hub_dir) -> list:
    """Every stored (agent, machine) row. A malformed file is skipped, never fatal.

    MEMOIZED for a couple of seconds on the directory's own fingerprint (``stamp``: file count
    plus newest mtime). One board read walks presence several times (activity rows, agent
    cards, lease liveness, the console binding), and each walk opened and parsed every file.
    A write always moves the fingerprint, so the memo never serves a state that has moved;
    callers get deep copies they may mutate; an empty or unreadable directory is never cached."""
    import copy as _copy
    key = (str(_dir(hub_dir)), stamp(hub_dir))
    now = time.time()
    memo = _ROWS_MEMO
    if (key[1] != (0, 0) and memo["key"] == key and memo["rows"] is not None
            and now - float(memo["at"]) < ROWS_TTL_S):
        return _copy.deepcopy(memo["rows"])
    out = _rows_uncached(hub_dir)
    if key[1] != (0, 0):
        memo.update(key=key, rows=_copy.deepcopy(out), at=now)
    return out


def _rows_uncached(hub_dir) -> list:
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
            entry["kit"] = is_kit_machine(r)
            entry["sessions"] = sorted(
                (dict(v or {}, **{f: (v or {}).get(f, "") for f in CONSOLE_FIELDS}, id=k,
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


def session_row(agent: str, machine: str, s: dict, now: float) -> dict:
    """ONE console, in the one shape every reader gets — the same key set whether the console
    reported a name and files or only a cwd. A projection that re-lists fields per caller is
    the projection that silently drops the next one.

    State is honest: an unattended run that ENDED is ``done``; a reported state other than
    working/idle (``waiting``) is kept; otherwise ``working`` while the console acted within
    WORKING_S, else ``idle`` with ``activity`` reading "idle 12 min, last did …" — a window
    that is merely OPEN is not a console that is working."""
    at = epoch(s.get("active_at") or s.get("at"))
    files_at = epoch(s.get("files_at"))
    files = [str(f)[:160] for f in (s.get("files") or [])][:12] \
        if (not files_at or now - files_at <= FILES_FRESH_S) else []
    kind = session_kind(s)
    ended = epoch(s.get("ended"))
    finished = kind != "attended" and (bool(ended) or s.get("state") == "done")
    idle_for = round(now - at) if at else None
    reported = str(s.get("state") or "")
    if finished:
        state = "done"
    elif reported and reported not in ("working", "idle"):
        state = reported
    else:
        state = "working" if at and now - at < WORKING_S else "idle"
    did = str(s.get("doing") or s.get("narration") or s.get("focus") or "")[:120]
    if state == "idle":
        activity = "idle %s%s" % (_phrase(idle_for), (", last did " + did) if did else "")
    elif state == "done":
        activity = "finished %s ago%s" % (
            _phrase(now - (ended or at)),
            (" — " + str(s.get("outcome"))) if s.get("outcome") else "")
    else:
        activity = did or state
    anchor = ended if finished and ended else at
    return {
        "agent": agent, "machine": machine or "",
        "session": str(s.get("id") or "")[:8],
        # The FULL id beside the 8-char display key: prefixes collide across a board's worth
        # of consoles, so anything that MATCHES a console (delivery, self-filtering) uses this.
        "session_id": str(s.get("id") or "")[:64],
        "name": str(s.get("name") or "")[:40],
        "runtime": str(s.get("runtime") or "")[:16],
        "cwd": preview(s.get("cwd"), 200),
        "repo": str(s.get("repo") or "")[:200],
        "app": str(s.get("app") or "")[:60],
        "focus": preview(s.get("focus"), 180),
        "project": str(s.get("project") or "")[:80],
        "state": state[:16],
        "files": files,
        "kind": kind, "unattended": kind != "attended",
        "run": str(s.get("run") or "")[:64],
        "subject": str(s.get("subject") or "")[:120],
        "subject_title": str(s.get("subject_title") or "")[:160],
        "phase": str(s.get("phase") or "")[:16],
        "doing": str(s.get("doing") or "")[:180],
        "narration": str(s.get("narration") or "")[:220],
        "last_result": str(s.get("last_result") or "")[:60],
        "targets": list(s.get("targets") or [])[:4],
        "outcome": str(s.get("outcome") or "")[:24],
        "started": epoch(s.get("started")) or None,
        "ended": ended or None,
        "bounded_s": int(epoch(s.get("bounded_s"))) or None,
        "finished": finished, "activity": activity,
        "idle_for_s": idle_for,
        "active_at": at or None,
        "age_s": round(now - anchor) if anchor else None,
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
                kind = session_kind(s)
                ended = epoch(s.get("ended"))
                finished = kind != "attended" and (bool(ended) or s.get("state") == "done")
                if kind == "attended" and not (s.get("focus") or s.get("cwd")
                                               or s.get("name") or s.get("repo")):
                    continue
                if finished:
                    # An ended unattended run stays a recap for UNATTENDED_RECAP_S, aged from
                    # when it ended, and never rides its last "working" stamp back into view.
                    anchor = ended or at
                    if not anchor or (now - anchor) > UNATTENDED_RECAP_S:
                        continue
                elif s.get("state") == "gone" or not at or (now - at) > SESSION_ACTIVE_S:
                    continue
                out.append(session_row(agent, m.get("machine") or "", s, now))
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


FILE_OVERLAP_WINDOW_S = 600


def file_overlaps(sessions: list) -> list:
    """FILE crossovers: two live consoles that both edited the same `<project>/<path>` inside
    the window — the strongest duplicate-work signal there is, and the one nobody sees from
    inside either console. Pairs are of DIFFERENT consoles (a console never overlaps itself);
    each pair is listed once, most shared files first. Pure over live_sessions() rows."""
    live = [s for s in sessions or []
            if s.get("files") and (s.get("age_s") is None or s["age_s"] <= FILE_OVERLAP_WINDOW_S)]
    out = []
    for i, one in enumerate(live):
        for other in live[i + 1:]:
            key_one = (one.get("agent"), one.get("machine"), one.get("session"))
            if key_one == (other.get("agent"), other.get("machine"), other.get("session")):
                continue
            shared = sorted(set(one["files"]) & set(other["files"]))
            if shared:
                out.append({"a": {k: one.get(k) for k in ("agent", "machine", "session")},
                            "b": {k: other.get(k) for k in ("agent", "machine", "session")},
                            "files": shared})
    out.sort(key=lambda o: -len(o["files"]))
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
