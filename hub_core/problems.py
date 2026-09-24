"""Errors are PROBLEMS; problems are OWNED; owners are REACHED.

A row in the operational stream is an OCCURRENCE. A CI failure can land as two rows (the
pipeline roll-up and the job), a job failing every ten minutes leaves a row per throttle
window, and one broken network route can produce dozens of rows about one fault. Triage by
row means several consoles working the same fault without seeing each other, serially,
while the queue hovers. This module folds occurrences into the thing somebody actually fixes:

  * one KEY per problem — (project, job) for CI, (app, kind, normalized message) for a
    satellite service, (agent, component, code) for a worker, (source, code, message) for
    the hub itself;
  * one STATE per problem — unclaimed | in flight (which CONSOLE, since when) | escalated
    (diagnosed, waiting on a named ask or task that is still open) | resolved (by, note,
    evidence) — with a recurrence after a resolve REOPENING it rather than hiding under the
    old acknowledgement;
  * one ACTION per problem — `claim` puts a console's name on it for every other console,
    `resolve` acknowledges every row behind it at once and records the root cause where the
    next person will look, `escalate` parks a diagnosed problem on its blocker until that
    blocker closes, `release` hands it back;
  * addressed delivery with URGENCY — an unclaimed problem reaches its OWNER's inbox
    (critical at once, error after ESCALATE_OWNER_S) and the OPERATOR after
    ESCALATE_OPERATOR_S, each under its own item id so each side is told once. Only a FRESH
    problem (last occurrence inside FRESH_S) is ever pushed; an older one is still listed.

Framework-free; every function takes the hub dir explicitly. Sidecar state only (claims,
resolutions, escalations): the rows stay where they are in the error stream, and the
error ack stays the single source of truth for "handled" — resolving a problem IS acking
its rows, so an older reader sees the same facts.

A HOLDER IS A CONSOLE, not an agent: two consoles of one person are two holders. A claim
made from a console is that console's promise; once that console has been gone for
CLAIM_GONE_S the promise is void — but "gone" requires proof. A console is gone only when its
MACHINE is still reporting presence and the console is not; absence from a roster that
cannot see the machine at all is "unprovable", and only the plain TTL may act on it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import errorlog, presence
from .process_lock import ProcessFileLock

CLAIM_TTL_S = 4 * 3600                 # a claim nobody renews or resolves releases itself
CLAIM_GONE_S = 30 * 60                 # a claim whose CONSOLE has been gone this long is released
FRESH_S = 24 * 3600                    # older problems are LISTED, never PUSHED
ESCALATE_OWNER_S = 10 * 60             # an ERROR reaches its owner after this unclaimed
ESCALATE_OPERATOR_S = 30 * 60          # anything unclaimed this long reaches the operator
_RESOLVED_MAX = 600
READ_TTL_S = 8.0

LIVE, GONE, UNPROVABLE = "live", "gone", "unprovable"

PID_RE = re.compile(r"^p-[0-9a-f]{12}$")
_PID_ANY = re.compile(r"\bp-[0-9a-f]{12}\b")
# ci adapters write "pipeline failed on <ref>: <job>, <job>"; a row without a `jobs`
# context carries the job names only in its message.
_PIPELINE_MSG = re.compile(r"^pipeline failed on (\S+): (.+)$", re.I)
_SEV_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _claims_path(hub_dir) -> Path:
    return Path(hub_dir) / "problem-claims.json"


def _resolved_path(hub_dir) -> Path:
    return Path(hub_dir) / "problem-resolved.json"


def _escalations_path(hub_dir) -> Path:
    return Path(hub_dir) / "problem-escalations.json"


def _read_json(path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def _now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def age_phrase(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


# ------------------------------------------------------------------ keys

def pipeline_jobs(row: dict) -> tuple:
    """(ref, [jobs]) for a ci.<project>.pipeline roll-up row, from its context or message."""
    ctx = row.get("context") or {}
    jobs = ctx.get("jobs")
    if isinstance(jobs, str):
        jobs = [j.strip() for j in jobs.split(",") if j.strip()]
    jobs = [str(j).strip().lower() for j in (jobs or []) if str(j).strip()]
    ref = str(ctx.get("ref") or "")
    if not jobs:
        m = _PIPELINE_MSG.match(str(row.get("message") or "").strip())
        if m:
            ref = ref or m.group(1)
            jobs = [j.strip().lower() for j in m.group(2).split(",") if j.strip()]
    return ref or "main", jobs


def problem_key(row: dict, hub_dir=None) -> tuple:
    """(key, kind, where, subject) for one error row. `where` is the app/project/agent the
    problem belongs to; `subject` is the job / error kind / component inside it."""
    src = str(row.get("source") or "").lower()
    ctx = row.get("context") or {}
    norm = errorlog.normalize_message(row.get("message"))
    if src.startswith("ci."):
        parts = src.split(".")
        project = parts[1] if len(parts) > 1 else str(ctx.get("project") or "unknown")
        leaf = ".".join(parts[2:]) or "pipeline"
        if leaf == "pipeline":
            _ref, jobs = pipeline_jobs(row)
            if len(jobs) == 1:
                return "ci:%s:%s" % (project, jobs[0]), "ci", project, jobs[0]
            return ("ci:%s:pipeline:%s" % (project, "+".join(sorted(jobs)) or norm),
                    "ci", project, "pipeline")
        return "ci:%s:%s" % (project, leaf), "ci", project, leaf
    if src.startswith("app."):
        parts = src.split(".")
        app = parts[1] if len(parts) > 1 else str(ctx.get("app") or "unknown")
        kind = parts[2] if len(parts) > 2 else "server"
        return "app:%s:%s:%s" % (app, kind, norm), "app", app, kind
    if src.startswith("agent."):
        _ch, agent, comp = errorlog.split_ident(src, hub_dir)
        agent = agent or "?"
        comp = comp or "worker"
        code = str(row.get("code") or "").lower()
        return ("agent:%s:%s:%s" % (agent, comp, code or norm), "agent", agent, comp)
    if src.startswith("browser."):
        return "browser:%s:%s" % (src, norm), "browser", "board", src[len("browser."):]
    if row.get("external"):
        return "external:%s:%s" % (src, norm), "external", "foreign", src
    return ("hub:%s:%s:%s" % (src, str(row.get("code") or ""), norm), "hub", "hub", src)


def pid_of(key: str) -> str:
    return "p-" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def mentions(text: str) -> set:
    """Problem ids named in free text (a focus line, a task title)."""
    return set(_PID_ANY.findall(str(text or "")))


# ------------------------------------------------------------------ liveness

class Roster:
    """Which consoles are provably alive, provably gone, or not visible from here."""

    def __init__(self, hub_dir=None, now: float | None = None, *, rows=None):
        self.now = now or time.time()
        self.live: set = set()
        self.last_seen: dict = {}
        self.reporting: set = set()
        try:
            data = presence.read(hub_dir) if rows is None else rows
        except Exception:                                    # noqa: BLE001 - unreadable = blind
            data = {}
        for _agent, seen in (data or {}).items():
            for m in (seen or {}).get("machines") or []:
                machine = str(m.get("machine") or "").strip().lower()
                stamp = max(presence.epoch(m.get("heartbeat_at")), presence.epoch(m.get("activity_at")),
                            presence.epoch(m.get("last_seen")))
                if machine and stamp and (self.now - stamp) <= presence.SESSION_ACTIVE_S:
                    self.reporting.add(machine)
                for s in m.get("sessions") or []:
                    sid = str(s.get("id") or "")[:8]
                    at = presence.epoch(s.get("at"))
                    if not sid:
                        continue
                    self.last_seen[sid] = max(self.last_seen.get(sid, 0.0), at)
                    if at and (self.now - at) <= presence.SESSION_ACTIVE_S:
                        self.live.add(sid)

    def gone_for(self, session, machine, *, floor: float = 0.0):
        """(state, gone_s). GONE needs proof: the console's machine is still reporting and
        the console is not. Everything else it cannot see is UNPROVABLE."""
        sid = str(session or "")[:8]
        if not sid:
            return UNPROVABLE, None
        if sid in self.live:
            return LIVE, None
        machine = str(machine or "").strip().lower()
        if machine and machine in self.reporting:
            since = max(float(floor or 0), float(self.last_seen.get(sid) or 0))
            return GONE, max(0.0, self.now - since) if since else None
        return UNPROVABLE, None


# ------------------------------------------------------------------ claims

def read_claims(hub_dir, now: float | None = None, roster: Roster | None = None) -> dict:
    """Live claims {pid: entry}, each carrying `holder_state` (live | gone | unprovable) and
    how long it has left. Liveness is resolved HERE so every reader gets the same answer."""
    now = now or time.time()
    roster = roster or Roster(hub_dir, now)
    out = {}
    for pid, entry in _read_json(_claims_path(hub_dir)).items():
        if not isinstance(entry, dict):
            continue
        at = float(entry.get("at") or 0)
        if now - at >= CLAIM_TTL_S:
            continue                        # the backstop: nobody renewed or resolved it
        state, gone_s = roster.gone_for(entry.get("session"), entry.get("machine"), floor=at)
        if state == GONE and (gone_s or 0) >= CLAIM_GONE_S:
            continue                        # the console that promised this is gone
        out[pid] = {**entry, "holder_state": state,
                    "gone_s": int(gone_s or 0) if state == GONE else None,
                    "grace_s": (int(max(0, CLAIM_GONE_S - (gone_s or 0))) if state == GONE else None)}
    return out


def _same_console(held: dict, agent: str, session: str = "") -> bool:
    """A claim with no session on either side (a CLI call without HUB_SESSION_ID) falls back
    to the agent; otherwise two consoles of one agent are two holders."""
    if str((held or {}).get("agent") or "").lower() != str(agent or "").lower():
        return False
    a, b = str((held or {}).get("session") or "")[:8], str(session or "")[:8]
    return not a or not b or a == b


def claim(hub_dir, pid: str, *, agent: str, machine: str = "", session: str = "",
          name: str = "", note: str = "", take: bool = False,
          now: float | None = None) -> tuple:
    """Put this CONSOLE's name on a problem. Returns (entry, None) or (None, refusal).

    The same console re-claiming renews; any OTHER console is refused while the claim lives,
    with who holds it and for how long — the refusal is the coordination, exactly like a task
    lease. `take` displaces a live holder deliberately and the entry records who was
    displaced, so the handover is on the board instead of being a silent overwrite."""
    now = now or time.time()
    pid = str(pid or "").strip()
    agent = str(agent or "").strip().lower()
    if not pid or not agent:
        return None, {"code": "need_problem_and_agent"}
    path = _claims_path(hub_dir)
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    roster = Roster(hub_dir, now)
    with ProcessFileLock(Path(hub_dir), name=".problem-claims.lock", timeout=5):
        claims = {k: v for k, v in _read_json(path).items()
                  if isinstance(v, dict) and now - float(v.get("at") or 0) < CLAIM_TTL_S}
        held = claims.get(pid)
        displaced = None
        if held and not _same_console(held, agent, session):
            state, gone_s = roster.gone_for(held.get("session"), held.get("machine"),
                                            floor=float(held.get("at") or 0))
            gone = state == GONE and (gone_s or 0) >= CLAIM_GONE_S
            if not gone and not take:
                age = int(now - float(held.get("since") or held.get("at") or now))
                who = holder_name(held)
                if state == GONE:
                    return None, {
                        "code": "claimed_elsewhere", "holder": held, "holder_state": state,
                        "gone_s": int(gone_s or 0),
                        "grace_s": int(max(0, CLAIM_GONE_S - (gone_s or 0))),
                        "msg": ("%s is GONE (last seen %s ago); the claim frees itself in %s. "
                                "Wait, take it now with --take, or resolve it with evidence if "
                                "you already have the fix" % (
                                    who, age_phrase(gone_s), age_phrase(CLAIM_GONE_S - (gone_s or 0))))}
                msg = ("%s claimed it %s ago; ask them or wait for it to release. If you already "
                       "have the FIX, resolve it with evidence: a fix closes a problem whoever "
                       "holds it, and the note records both names" % (who, age_phrase(age)))
                if str(held.get("agent") or "").lower() == agent:
                    msg += ". That console is yours: --take takes it over and records the handover"
                return None, {"code": "claimed_elsewhere", "msg": msg, "holder": held,
                              "holder_state": state}
            if not gone:
                displaced = {k: held.get(k) for k in ("agent", "name", "session", "machine")}
            held = None
        entry = {"agent": agent, "machine": str(machine or "").lower()[:60],
                 "session": str(session or "")[:8], "name": str(name or "")[:40],
                 "note": str(note or ""), "at": now,
                 "since": float((held or {}).get("since") or now)}
        if displaced:
            entry["taken_from"] = displaced
        claims[pid] = entry
        _write_json(path, claims)
    return entry, None


def release(hub_dir, pid: str, *, agent: str, operator: bool = False,
            claims_only: bool = False) -> bool:
    """Hand a problem back. A live claim: only its agent (or the operator). A problem that
    REOPENED under the console that resolved it holds no lease, so handing it back is
    recorded on the resolution (`handed_back`) and the fold stops presuming."""
    pid = str(pid or "").strip()
    agent = str(agent or "").strip().lower()
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".problem-claims.lock", timeout=5):
        claims = _read_json(_claims_path(hub_dir))
        held = claims.get(pid)
        if isinstance(held, dict):
            if str(held.get("agent") or "").lower() != agent and not operator:
                return False
            claims.pop(pid, None)
            _write_json(_claims_path(hub_dir), claims)
            return True
    if claims_only:
        return False
    with ProcessFileLock(Path(hub_dir), name=".problem-resolved.lock", timeout=5):
        cur = _read_json(_resolved_path(hub_dir))
        res = cur.get(pid)
        if not isinstance(res, dict) or res.get("handed_back"):
            return False
        if str(res.get("by") or "").lower() != agent and not operator:
            return False
        res["handed_back"] = True
        cur[pid] = res
        _write_json(_resolved_path(hub_dir), cur)
    return True


def read_resolved(hub_dir) -> dict:
    return _read_json(_resolved_path(hub_dir))


# ------------------------------------------------------------------ escalation

def blocker_open(blocked_on: str, state) -> bool:
    """Is the named ask or task still waiting? An ask is open while its note carries the
    `open` tag (answering drops it); a task while it is not done or dropped. A blocker the
    ledger does not hold reads CLOSED: an escalation that cannot be checked must never keep a
    live failure off the queue."""
    ent = ((state or {}).get("entities") or {}).get(str(blocked_on or ""))
    if not isinstance(ent, dict):
        return False
    if ent.get("type") == "note":
        return "open" in [str(t).lower() for t in (ent.get("tags") or [])]
    if ent.get("type") == "task":
        return str(ent.get("status") or "").lower() not in ("done", "dropped")
    return False


def escalate(hub_dir, pid: str, *, agent: str, blocked_on: str, note: str = "",
             machine: str = "", session: str = "", name: str = "",
             now: float | None = None) -> dict:
    """Record that a DIAGNOSED problem waits on a named ask or task. The newest diagnosis
    wins; the record names who. Kept apart from claims: it is a fact about the problem, not
    a lease on a worker, so it must outlive the session that recorded it."""
    now = now or time.time()
    entry = {"agent": str(agent or "").strip().lower(), "blocked_on": str(blocked_on),
             "note": str(note or ""), "machine": str(machine or "").lower()[:60],
             "session": str(session or "")[:8], "name": str(name or "")[:40], "at": now}
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".problem-escalations.lock", timeout=5):
        cur = _read_json(_escalations_path(hub_dir))
        cur[str(pid)] = entry
        if len(cur) > _RESOLVED_MAX:
            cur = dict(sorted(cur.items(),
                              key=lambda kv: float((kv[1] or {}).get("at") or 0))[-_RESOLVED_MAX:])
        _write_json(_escalations_path(hub_dir), cur)
    return entry


def clear_escalation(hub_dir, pid: str) -> bool:
    with ProcessFileLock(Path(hub_dir), name=".problem-escalations.lock", timeout=5):
        cur = _read_json(_escalations_path(hub_dir))
        if str(pid) not in cur:
            return False
        cur.pop(str(pid), None)
        _write_json(_escalations_path(hub_dir), cur)
    return True


def read_escalations(hub_dir, state) -> dict:
    """{pid: entry} for escalations whose blocker is STILL OPEN. One that lapsed (its ask was
    answered, its task closed) is simply absent, so the problem reads unclaimed again at the
    moment it became workable."""
    raw = _read_json(_escalations_path(hub_dir))
    if not raw or state is None:
        return {}
    return {pid: dict(e) for pid, e in raw.items()
            if isinstance(e, dict) and blocker_open(e.get("blocked_on"), state)}


# ------------------------------------------------------------------ resolve

def resolve(hub_dir, pid: str, fingerprints: list, *, agent: str, note: str = "",
            evidence: str = "", session: str = "", name: str = "",
            now: float | None = None) -> dict:
    """Acknowledge EVERY row behind a problem and record why, then drop its claim and any
    escalation. A fix closes a problem whoever holds it — the claim is coordination, the fix
    is the fact — but the note then names both, so the holder learns it from the board."""
    now = now or time.time()
    pid = str(pid or "").strip()
    agent = str(agent or "").strip().lower()
    holder = read_claims(hub_dir, now).get(pid) or {}
    if holder and holder.get("agent") and not _same_console(holder, agent, session):
        note = ("resolved by %s%s while %s held it -- " % (
            agent, (" " + str(name)) if name else "", holder_name(holder))) + str(note or "")
    acked = 0
    for fp in fingerprints or []:
        if errorlog.ack(hub_dir, str(fp), actor=agent, note=(note or evidence or "resolved")):
            acked += 1
    entry = {"at": _now_iso(now), "epoch": round(now, 3), "by": agent, "note": str(note or ""),
             "evidence": str(evidence or ""), "rows": int(acked),
             "session": str(session or "")[:8], "name": str(name or "")[:40]}
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".problem-resolved.lock", timeout=5):
        cur = _read_json(_resolved_path(hub_dir))
        cur[pid] = entry
        if len(cur) > _RESOLVED_MAX:
            cur = dict(sorted(cur.items(),
                              key=lambda kv: float((kv[1] or {}).get("epoch") or 0))[-_RESOLVED_MAX:])
        _write_json(_resolved_path(hub_dir), cur)
    try:
        release(hub_dir, pid, agent=agent, operator=True, claims_only=True)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        clear_escalation(hub_dir, pid)          # a fixed problem is waiting on nobody
    except Exception:                                        # noqa: BLE001
        pass
    return entry


# ------------------------------------------------------------------ folding

def _title(kind: str, where: str, subject: str, newest: dict, count: int) -> str:
    msg = str(newest.get("message") or "")
    ctx = newest.get("context") or {}
    if kind == "ci":
        ref, jobs = pipeline_jobs(newest) if subject == "pipeline" else (str(ctx.get("ref") or "main"), [])
        if subject == "pipeline" and not jobs:
            # NO JOBS IS NOT AN UNKNOWN JOB: the pipeline ran nothing and deployed nothing.
            return "%s: pipeline on %s -- no job ran (%d push%s)" % (
                where, ref, count, "" if count == 1 else "es")
        what = subject if subject != "pipeline" else ("pipeline (%s)" % ", ".join(jobs)[:60])
        return "%s: %s failing on %s (%d run%s)" % (where, what, ref, count, "" if count == 1 else "s")
    if kind in ("app", "agent"):
        return "%s: %s" % (where, msg[:300])
    return msg[:320]


def _presumed_holder(res: dict, roster: Roster) -> dict | None:
    """The console that resolved a problem, as its holder after a reopen — only while that
    console is provably ALIVE and has not handed it back. Stricter than a claim on purpose:
    a presumption needs proof of life, where a claim needs proof of death."""
    if not isinstance(res, dict) or res.get("handed_back"):
        return None
    sid = str(res.get("session") or "")[:8]
    if not sid or sid not in roster.live:
        return None
    epoch = float(res.get("epoch") or 0) or time.time()
    return {"agent": str(res.get("by") or "").lower(), "name": str(res.get("name") or "")[:40],
            "session": sid, "machine": "", "at": epoch, "since": epoch, "presumed": True,
            "holder_state": LIVE, "gone_s": None, "grace_s": None,
            "note": "reopened: it recurred after this console resolved it"}


def fold(rows: list, *, hub_dir=None, acked=None, claims=None, resolved=None,
         escalations=None, bar=None, roster: Roster | None = None,
         now: float | None = None) -> list:
    """Fold error rows (newest first) into problems, strongest first.

    `bar` is the row predicate (on_bar, reason); a problem is on the board when any of its
    OPEN occurrences is. Everything else is folded too and reported as deferred, so the
    surface can say what it is NOT showing. States: resolved (every row acked), in_flight (a
    console holds it), escalated (diagnosed, waiting on an open ask/task), unclaimed."""
    now = now or time.time()
    bar = bar or errorlog.passes_bar
    acked = errorlog.read_acked(hub_dir) if acked is None else acked
    roster = roster or Roster(hub_dir, now)
    claims = read_claims(hub_dir, now, roster) if claims is None else claims
    resolved = read_resolved(hub_dir) if resolved is None else resolved
    escalations = escalations or {}
    groups, order = {}, []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key, kind, where, subject = problem_key(row, hub_dir)
        p = groups.get(key)
        epoch = float(row.get("epoch") or 0) or None
        row_acked = errorlog.is_acked(row, acked)
        # The larger of the two counts is the honest weight: summing them double-counts the
        # occurrence this row already is.
        weight = max(1 + int(row.get("occurrences_since_last") or 0),
                     int(row.get("occurrences_folded") or 0))
        on_bar, why = bar(row)
        if p is None:
            p = {"id": pid_of(key), "key": key, "kind": kind, "where": where, "subject": subject,
                 "severity": str(row.get("severity") or "error"), "rows": [], "count": 0,
                 "first_seen": epoch, "last_seen": epoch, "open_first": None, "open_last": None,
                 "open_rows": 0, "newest": row, "on_board": False, "defer_reason": why,
                 "acks": [], "url": "", "details": "", "cause": ""}
            groups[key] = p
            order.append(key)
        rctx = row.get("context") or {}
        if not p["url"] and rctx.get("url"):
            p["url"] = str(rctx.get("url"))[:600]
        if not p["details"] and row.get("details"):
            p["details"] = str(row.get("details"))[:errorlog.DETAILS_LIMIT]
        if not p["cause"] and row.get("cause"):
            p["cause"] = str(row.get("cause"))[:600]
        fp = str(row.get("fingerprint") or "")
        if fp and fp not in p["rows"]:
            p["rows"].append(fp)
        p["count"] += weight
        occurred = max(epoch or 0, float(row.get("last_occurrence") or 0)) or epoch
        if epoch:
            p["first_seen"] = min(p["first_seen"] or epoch, epoch)
            p["last_seen"] = max(p["last_seen"] or occurred, occurred)
        if _SEV_RANK.get(str(row.get("severity") or "error"), 1) < _SEV_RANK.get(p["severity"], 1):
            p["severity"] = str(row.get("severity"))
        # AN ACKED ROW CANNOT PUT A PROBLEM BACK ON THE QUEUE: the verdict is the bar's
        # reading of the OPEN rows. A problem with no open rows keeps its historical verdict.
        if on_bar:
            p["ever_on_bar"] = True
            if not row_acked:
                p["on_board"] = True
                p["defer_reason"] = ""
        elif not row_acked and not p["on_board"]:
            p["defer_reason"] = why
        if row_acked:
            mark = acked.get(fp) or {}
            if mark and mark not in p["acks"]:
                p["acks"].append(mark)
        else:
            p["open_rows"] += weight
            if epoch:
                p["open_first"] = min(p["open_first"] or epoch, epoch)
                p["open_last"] = max(p["open_last"] or occurred, occurred)
    for p in groups.values():
        if p.pop("ever_on_bar", False) and not p["open_rows"]:
            p["on_board"] = True
            p["defer_reason"] = ""
    # A PIPELINE ROLL-UP IS NOT A PROBLEM OF ITS OWN when each job it names has one: its rows
    # join each job's problem (so resolving a job acks them too) and it leaves the fold —
    # otherwise the roll-up is handed out as separate work while both jobs read in flight.
    for key in list(order):
        p = groups.get(key)
        if not p or p["kind"] != "ci" or p["subject"] != "pipeline":
            continue
        _ref, jobs = pipeline_jobs(p["newest"])
        if len(jobs) < 2:
            continue
        targets = [groups.get("ci:%s:%s" % (p["where"], j)) for j in jobs]
        if not all(targets):
            continue
        for tgt in targets:
            for fp in p["rows"]:
                if fp not in tgt["rows"]:
                    tgt["rows"].append(fp)
            if p["on_board"]:
                tgt["on_board"] = True
        order.remove(key)
        groups.pop(key, None)
    out = []
    for key in order:
        p = groups[key]
        newest = p.pop("newest")
        ctx = newest.get("context") or {}
        res = resolved.get(p["id"]) if isinstance(resolved.get(p["id"]), dict) else None
        holder = claims.get(p["id"])
        recurred = bool(res and p["open_rows"] and p["open_last"]
                        and float(res.get("epoch") or 0) < float(p["open_last"]))
        esc = escalations.get(p["id"]) if isinstance(escalations.get(p["id"]), dict) else None
        if recurred and not holder and not (esc and float(esc.get("at") or 0) > float(res.get("epoch") or 0)):
            # A recurrence after a resolve REOPENS under the console that fixed it last — it
            # knows the row. Presumed, not leased: gone console -> unclaimed.
            holder = _presumed_holder(res, roster)
        if p["open_rows"] == 0:
            state = "resolved"
        elif holder:
            state = "in_flight"
        elif esc:
            state = "escalated"
        else:
            state = "unclaimed"
        last_ack = max(p["acks"], key=lambda a: str(a.get("at") or "")) if p["acks"] else None
        p.update({
            "state": state,
            "title": _title(p["kind"], p["where"], p["subject"], newest, p["count"]),
            "message": str(newest.get("message") or "")[:1200],
            "code": newest.get("code") or "",
            "details": p.get("details") or str(newest.get("details") or "")[:errorlog.DETAILS_LIMIT],
            "cause": p.get("cause") or str(newest.get("cause") or "")[:600],
            "url": p.get("url") or str(ctx.get("url") or "")[:600],
            "context": {k: ctx[k] for k in ("ref", "sha", "jobs", "path", "operation", "machine",
                                            "app", "project", "source", "actor", "line", "col")
                        if ctx.get(k)},
            "age_s": (int(now - p["open_first"]) if p["open_first"] else
                      (int(now - p["first_seen"]) if p["first_seen"] else None)),
            # RECENCY rides every queue line: an old problem that happened a minute ago and
            # one that has been silent for a day are different kinds of work.
            "since_last_s": int(now - p["last_seen"]) if p["last_seen"] else None,
            "fresh": bool(p["last_seen"]) and (now - p["last_seen"]) <= FRESH_S,
            "holder": holder or None,
            "held_for_s": int(now - float(holder.get("since") or holder.get("at") or now)) if holder else None,
            "holder_state": (holder or {}).get("holder_state") or None,
            "resolved": res, "reopened": bool(recurred and state != "resolved"),
            "last_ack": last_ack,
            "bar": "on" if p["on_board"] else "deferred",
            "escalation": ({**esc, "for_s": int(now - float(esc.get("at") or now))}
                           if esc and state != "resolved" else None),
        })
        p.pop("acks", None)
        out.append(p)
    rank = {"unclaimed": 0, "in_flight": 1, "escalated": 2}
    out.sort(key=lambda p: (rank.get(p["state"], 3), _SEV_RANK.get(p["severity"], 1),
                            -(p["age_s"] or 0)))
    return out


_READ_CACHE: dict = {}


def _folded(hub_dir, state, now: float, limit: int) -> tuple:
    """(rows, meta, problems) cached per hub on the stamps of every input sidecar — this
    rides every board payload and the inbox long-poll."""
    key = (errorlog.stamp(hub_dir), presence.stamp(hub_dir), int(limit),
           _escalation_signature(hub_dir, state))
    hit = _READ_CACHE.get(str(hub_dir))
    if hit and hit["key"] == key and (now - hit["at"]) < READ_TTL_S:
        rows, meta, probs = hit["value"]
        return rows, dict(meta), copy.deepcopy(probs)
    rows, meta = errorlog.read(hub_dir, limit=limit)
    roster = Roster(hub_dir, now)
    probs = fold(rows, hub_dir=hub_dir, now=now, roster=roster,
                 escalations=read_escalations(hub_dir, state))
    _READ_CACHE[str(hub_dir)] = {"key": key, "at": now, "value": (rows, meta, probs)}
    return rows, dict(meta), copy.deepcopy(probs)


def _escalation_signature(hub_dir, state) -> tuple:
    """Which escalation blockers are open right now — an answered ask changes the fold with
    no sidecar write at all."""
    raw = _read_json(_escalations_path(hub_dir))
    return tuple(sorted((pid, blocker_open(e.get("blocked_on"), state))
                        for pid, e in raw.items() if isinstance(e, dict)))


def read(hub_dir, state=None, *, include: str = "", app: str = "",
         now: float | None = None, limit: int = errorlog.KEEP_ROWS) -> tuple:
    """(problems, metadata). Default: on the board and not resolved. include="resolved"
    adds resolved ones; include="all" adds the deferred ones too.

    The COUNTS describe the QUEUE, whatever the caller asked to see: a listing that includes
    off-board rows must never read as a drowning queue."""
    now = now or time.time()
    auto_resolve(hub_dir, now=now)
    rows, meta, probs = _folded(hub_dir, state, now, limit)
    app = str(app or "").strip().lower()
    if app:
        probs = [p for p in probs if p["where"] == app]
    deferred = sum(1 for p in probs if not p["on_board"])
    if include != "all":
        probs = [p for p in probs if p["on_board"]]
    if include not in ("resolved", "all"):
        probs = [p for p in probs if p["state"] != "resolved"]
    queued = [p for p in probs if p["on_board"]]
    off_board = [p for p in probs if not p["on_board"]]
    counts = {s: sum(1 for p in queued if p["state"] == s)
              for s in ("unclaimed", "in_flight", "escalated", "resolved")}
    counts.update({"deferred": deferred, "rows": len(rows)})
    if off_board:
        counts["off_board"] = {"listed": len(off_board),
                               "unclaimed": sum(1 for p in off_board if p["state"] == "unclaimed")}
    oldest = max((p["age_s"] or 0 for p in queued if p["state"] == "unclaimed"), default=0)
    metadata = {"counts": counts, "oldest_unclaimed_s": oldest,
                "available": meta.get("available", True),
                "window": meta.get("window"), "stored": meta.get("stored"),
                "escalation": {"owner_error_s": ESCALATE_OWNER_S,
                               "operator_s": ESCALATE_OPERATOR_S,
                               "claim_ttl_s": CLAIM_TTL_S, "claim_gone_s": CLAIM_GONE_S,
                               "fresh_s": FRESH_S}}
    return probs, metadata


def find(hub_dir, pid: str, state=None, now: float | None = None):
    pid = str(pid or "").strip()
    if not pid:
        return None
    probs, _meta = read(hub_dir, state, include="all", now=now)
    return next((p for p in probs if p["id"] == pid), None)


# ------------------------------------------------------------------ auto-resolution

_AUTO_AT: dict = {}
_AUTO_MIN_INTERVAL_S = 60


def auto_resolve(hub_dir, now: float | None = None, force: bool = False) -> dict:
    """Retire rows on POSITIVE evidence, never by age:

      * a worker's blind-window row is closed by that worker's OWN later presence — the
        worker reporting again is the evidence the window ended;
      * a pipeline roll-up is handled once every job it names is acknowledged.

    Throttled per process; idempotent (acks are)."""
    now = now or time.time()
    key = str(hub_dir)
    if not force and (now - _AUTO_AT.get(key, 0.0)) < _AUTO_MIN_INTERVAL_S:
        return {"skipped": "throttled"}
    _AUTO_AT[key] = now
    done = {"reconnected": 0, "rollup": 0, "ci_passed": 0}
    try:
        # A CI job's failure is over when THAT job passed later on the same ref (hub_core.ci
        # stamps every pass) — never because a later deploy happened to be green.
        from . import ci as _ci
        done["ci_passed"] = _ci.retire_passed(hub_dir, now=now)
    except Exception:                                        # noqa: BLE001 - never break a read
        pass
    try:
        rows, _meta = errorlog.read(hub_dir, limit=errorlog.KEEP_ROWS)
        acked = errorlog.read_acked(hub_dir)
        seen = None
        for row in rows:
            fp = str(row.get("fingerprint") or "")
            if not fp or errorlog.is_acked(row, acked):
                continue
            src = str(row.get("source") or "").lower()
            if src.startswith("ci.") and src.endswith(".pipeline"):
                _ref, jobs = pipeline_jobs(row)
                project = src.split(".")[1] if len(src.split(".")) > 1 else ""
                if len(jobs) >= 2 and project:
                    job_rows = [r for r in rows if str(r.get("source") or "").lower()
                                in {"ci.%s.%s" % (project, j) for j in jobs}]
                    if job_rows and all(errorlog.is_acked(r, acked) for r in job_rows):
                        errorlog.ack(hub_dir, fp, actor="hub", note=(
                            "roll-up of %s: every job it names is acknowledged" % ", ".join(jobs)))
                        done["rollup"] += 1
                continue
            if src.startswith("agent.") and str(row.get("code") or "") == "hub_unreachable_span":
                if seen is None:
                    seen = presence.read(hub_dir) or {}
                _ch, agent, _rest = errorlog.split_ident(src, hub_dir)
                machine = str((row.get("context") or {}).get("machine") or "").lower()
                last = 0.0
                for m in (seen.get(agent) or {}).get("machines") or []:
                    if machine and str(m.get("machine") or "").lower() != machine:
                        continue
                    last = max(last, presence.epoch(m.get("last_seen")))
                if last and last > float(row.get("epoch") or 0) + 30:
                    errorlog.ack(hub_dir, fp, actor="hub", note=(
                        "the worker reported again at %s; the blind window closed itself"
                        % _now_iso(last)))
                    done["reconnected"] += 1
    except Exception:                                        # noqa: BLE001 - never break a read
        return done
    return done


# ------------------------------------------------------------------ owners and addressing

def owners_of(p: dict, apps: dict | None = None, live_rows=None) -> set:
    """Who is reached about a problem. A worker's problem belongs to that worker. A service
    or CI problem belongs to the owners the adopter declared for it (HUB_APPS); when nobody is
    declared for a CI project, whoever has a live console in that project right now is the
    person who would want to know — for ADDRESSING only, never for a destructive action."""
    where = str(p.get("where") or "").strip().lower()
    kind = p.get("kind")
    if not where:
        return set()
    if kind == "agent":
        return {where}
    if kind in ("hub", "browser", "external"):
        return set()
    out = set()
    for slug, cfg in (apps or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        names = {str(slug).lower(), str(cfg.get("project") or "").lower()}
        if where in names:
            out |= {str(o).strip().lower() for o in (cfg.get("owners") or []) if str(o).strip()}
    out = {o for o in out if o and not presence.is_service_identity(o)}
    if not out and kind == "ci":
        for r in live_rows or []:
            if str(r.get("project") or "").lower() == where and r.get("agent"):
                out.add(str(r["agent"]).lower())
    return out


def holder_name(holder: dict) -> str:
    holder = holder or {}
    return "%s%s" % (holder.get("agent") or "?",
                     (" " + str(holder["name"])) if holder.get("name") else
                     ((" " + str(holder["session"])) if holder.get("session") else ""))


def holder_phrase(p: dict) -> str:
    """How a held problem is SAID — one vocabulary for every surface. "in flight" is a claim
    about the present tense: printed only when a live console is on the roster."""
    holder = p.get("holder") or {}
    if not holder:
        return str(p.get("state") or "").replace("_", " ")
    who, age = holder_name(holder), age_phrase(p.get("held_for_s"))
    state = holder.get("holder_state") or UNPROVABLE
    if state == GONE:
        return ("held by a console that is GONE - %s, gone %s, frees itself in %s"
                % (who, age_phrase(holder.get("gone_s")), age_phrase(holder.get("grace_s"))))
    if state == LIVE:
        return "in flight - %s since %s" % (who, age)
    if holder.get("session"):
        return "claimed - %s %s ago (that console is not visible from here)" % (who, age)
    return "claimed - %s %s ago" % (who, age)


def as_item(p: dict, item_id: str = "", *, owners=None, escalated: bool = False,
            now: float | None = None) -> dict:
    """A problem shaped like an inbox item (kind `error`), carrying the exact commands."""
    now = now or time.time()
    age = int(p.get("age_s") or 0)
    holder = p.get("holder") or {}
    who = ", ".join(sorted(owners or [])) or "nobody on record"
    lines = [
        "[%s] %s - %s" % (str(p["severity"]).upper(), p["where"], p["title"]),
        "state: %s" % (holder_phrase(p) if holder else p["state"].replace("_", " ")),
        "occurrences: %d (%d row%s) - first %s ago - last %s ago" % (
            p["count"], len(p["rows"]), "" if len(p["rows"]) == 1 else "s",
            age_phrase(age), age_phrase(p.get("since_last_s"))),
        "owners: %s%s" % (who, ("  (escalated to you as operator: unclaimed %s)" % age_phrase(age))
                          if escalated else ""),
    ]
    if p.get("reopened"):
        res = p.get("resolved") or {}
        lines.append("REOPENED: it recurred after %s resolved it (%s)" % (
            res.get("by"), (res.get("note") or "")[:120]))
    if p.get("cause"):
        lines.append("cause: %s" % p["cause"][:300])
    if p.get("url"):
        lines.append("link: %s" % p["url"])
    lines.append("claim it: python -m hub_core.client claim %s --note \"<what you are checking>\"" % p["id"])
    lines.append("resolve it (acks every row behind it): python -m hub_core.client resolve %s "
                 "--note \"<root cause>\" --evidence <sha|url>" % p["id"])
    return {"kind": "error", "id": item_id or ("problem:" + p["id"]), "problem": p["id"],
            "from": "the hub", "severity": p["severity"], "where": p["where"],
            "title": "[%s] %s - %s%s" % (
                p["severity"], p["title"][:150],
                ("reopened under %s" % holder_name(holder)) if holder.get("presumed")
                else "unclaimed %s" % age_phrase(age),
                " (escalated)" if escalated else ""),
            "body": "\n".join(lines), "at": _now_iso(float(p.get("first_seen") or now)),
            "waited_s": age}


def items_for_agent(hub_dir, state, agent: str, operator: str, *, apps: dict | None = None,
                    live_rows=None, now: float | None = None) -> list:
    """Unclaimed FRESH problems ADDRESSED to this agent, shaped like inbox items.

    To an OWNER: critical at once, error after ESCALATE_OWNER_S. To the OPERATOR: anything
    unclaimed past ESCALATE_OPERATOR_S, under its own id so each side is told once. A problem
    that reopened under one of this agent's consoles is told at once, once per reopen."""
    now = now or time.time()
    agent = str(agent or "").strip().lower()
    if not agent:
        return []
    probs, _meta = read(hub_dir, state, now=now)
    out = []
    for p in probs:
        holder = p.get("holder") or {}
        owners = owners_of(p, apps, live_rows)
        if p["state"] == "in_flight" and holder.get("presumed") \
                and str(holder.get("agent") or "").lower() == agent:
            res = p.get("resolved") or {}
            out.append(as_item(p, "problem:%s:reopened:%d" % (p["id"], int(float(res.get("epoch") or 0))),
                               owners=owners, now=now))
            continue
        if p["state"] != "unclaimed" or not p.get("fresh", True):
            continue
        age = int(p.get("age_s") or 0)
        to_owner = agent in owners and (p["severity"] == "critical" or age >= ESCALATE_OWNER_S)
        to_operator = agent == str(operator or "").lower() and age >= ESCALATE_OPERATOR_S
        if not (to_owner or to_operator):
            continue
        item_id = "problem:%s" % p["id"] if to_owner else "problem:%s:operator" % p["id"]
        out.append(as_item(p, item_id, owners=owners, escalated=not to_owner, now=now))
    out.sort(key=lambda i: (0 if i.get("severity") == "critical" else 1, -(i.get("waited_s") or 0)))
    return out
