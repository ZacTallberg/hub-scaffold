"""Shared work between live consoles, computed ONCE on the hub and told ONLY to the two
consoles it concerns — so agents share evidence, divide useful work and coordinate edits,
and nobody pays for a roster of everyone else's work on every prompt.

The hub already holds every live console's project, recently edited files, focus, held task
and claimed problems (presence + leases + problem claims). The comparison belongs here, and
the answer belongs in the console it is about. Kinds, strongest first:

  file     two consoles edited the same file in the last ten minutes
  problem  both hold or name the same board problem, or both are triaging the error queue
  task     both hold the same board task — or one holds a task about the project the other
           works in untracked, on the same subject
  project  same project AND shared work: the same subtree, or the same subject in the
           project's vocabulary. Co-location alone produces nothing.
  topic    different projects, but both name the same external system (the adopter's own
           vocabulary, HUB_OVERLAP_SYSTEMS) or touch the same distinctive file — alignment
           worth a message, not a collision; capped per console

What deliberately does NOT fire, each measured as noise on the origin system: one person's
own windows pairing on project or topic (that is how they work — only the same FILE, problem
or task between them is a collision); an unattended console being told anything (nobody is
reading it), or pairing on anything weaker than a task; a console standing above every
project being "aligned"; a mention of a problem that SOMEBODY ELSE holds (a mention is not a
claim); a project's own name counting as a shared subject; ambient words present on a third
of the live consoles.

A signal has a stable id while its condition holds (kind + the two parties + the key), so it
is announced ONCE per side; when its files or subject change the id changes and it announces
again, and a standing one may repeat after REANNOUNCE_S. Delivery state is a sidecar.
Framework-free; every function takes the hub dir explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .process_lock import ProcessFileLock

REANNOUNCE_S = 6 * 3600
_SEEN_MAX = 2000
TOPIC_CAP_PER_CONSOLE = 2
STRENGTH = {"file": 0, "problem": 1, "task": 2, "project": 3, "topic": 4}

_STOP = set("""
the a an and or of to in on for with from by at is are was were be been being it its this that
these those i we you they he she them our your their my me us make made making do does did done
doing get got go going need needs want wants please just also only still very some any all more
most less then than there here into out over under about after before again once when where why
how what which who whom while because until unless not no yes can could should would will shall
may might must let lets like new old one two three first next last now today up down off
add fix update change check run test try use using used set see look show find keep work working
app apps code file files thing things stuff something everything anything way ways
https http www com net local html json yaml index main self none true false null
hub repo repos branch commit deploy deployed deploys push pushed pull
apply applied design designed page pages line lines note notes item items list lists
finished holds longer problem
""".split())
_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")
# Filenames every repository has: two consoles editing their OWN copy is not a signal.
_COMMON_FILES = {"__init__.py", "settings.py", "urls.py", "views.py", "models.py", "admin.py",
                 "tests.py", "apps.py", "readme.md", "main.py", "utils.py", "forms.py",
                 "config.py", "index.html", "base.html", "app.css", "app.js", "conftest.py",
                 "requirements.txt", "package.json", ".gitignore", ".env", "manage.py",
                 "claude.md", "agents.md", "changelog.md", "notes.md", "dockerfile",
                 "makefile", "pyproject.toml", "serializers.py", "signals.py"}
# A console's scratch area is private by construction, and a throwaway worktree folder is
# not a project: neither can be shared work, and a worktree NAMED after a subject would
# otherwise become a standing false subject for everyone who later works inside it.
_PRIVATE_PATH = re.compile(r"(?i)(^|/)(?:temp|tmp|appdata|scratchpad|__pycache__|node_modules|\.git)/")
_WORKTREE_SEG = re.compile(r"(?i)(^|/)(?:_?wt[-_.][^/]*|_?wt)(?=/)")
_TRIAGE = re.compile(r"\b(board|error|errors|problems?)\b.*\btriage\b|\btriage\b.*\b(board|error|errors|problems?)\b", re.I)
_PID = re.compile(r"\bp-[0-9a-f]{12}\b")


def systems() -> tuple:
    """The adopter's systems of record and project stacks (HUB_OVERLAP_SYSTEMS, comma
    separated). Deliberately NOT shared plumbing every console touches (the VCS host, the web
    framework, the language): matching on those produces identical pairs everywhere, which
    teaches everyone to ignore the signal within a day."""
    raw = os.environ.get("HUB_OVERLAP_SYSTEMS") or ""
    return tuple(sorted({s.strip().lower() for s in raw.split(",") if s.strip()}))


def _terms(text: str) -> set:
    out = set()
    for w in _WORD.findall(str(text or "").lower()):
        w = w.strip("_-")
        if len(w) >= 4 and w not in _STOP and not w.isdigit():
            out.add(w)
    return out


def _sid(row: dict) -> str:
    return str(row.get("session") or "")[:8]


def _files(row: dict) -> list:
    return [f for f in (row.get("files") or [])
            if f and not _PRIVATE_PATH.search("/" + str(f).replace("\\", "/"))
            and not _WORKTREE_SEG.search("/" + str(f).replace("\\", "/"))]


def _norm_name(s: str) -> str:
    return str(s or "").strip().lower().replace("_", "-")


def _subsystems(row: dict) -> set:
    """The part of the repository a console works IN: the first path segment below the
    project. Two consoles in one repo on different subtrees are co-located, not colliding."""
    out = set()
    project = str(row.get("project") or "")
    for f in _files(row):
        parts = [x for x in str(f).replace("\\", "/").split("/") if x and x not in (".", "..")]
        if parts and _norm_name(parts[0]) == _norm_name(project):
            parts = parts[1:]
        if len(parts) > 1:
            out.add(parts[0].lower())
        elif parts:
            out.add("(root)")
    return out


def _file_names(row: dict) -> set:
    out = set()
    for f in _files(row):
        base = str(f).replace("\\", "/").rsplit("/", 1)[-1].lower().strip()
        if base and base not in _COMMON_FILES:
            out.add(base)
    return out


def _systems_named(row: dict) -> set:
    hay = " ".join([str(row.get("focus") or ""), " ".join(_files(row)),
                    str(row.get("project") or "")]).lower()
    return {s for s in systems() if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", hay)}


def _subject_terms(row: dict) -> set:
    """What this console works ON, as comparable words: focus words plus file stems
    (without extension, so a task about 'export_job' meets a console editing export_job.py)."""
    out = _terms(row.get("focus"))
    for base in _file_names(row):
        out.add(base)
        stem = base.rsplit(".", 1)[0]
        if len(stem) >= 4:
            out.add(stem)
    return out | _systems_named(row)


def _console_terms(row: dict, projects: set) -> set:
    """ONLY meaningful tokens: a named system, ANOTHER live console's project name, or a
    distinctive file basename. Naming what counts is bounded; excluding noise word by word
    is endless."""
    hay = " ".join([str(row.get("focus") or ""), " ".join(_files(row)),
                    str(row.get("project") or "")]).lower()
    out = set(_systems_named(row))
    own = str(row.get("project") or "").strip()
    if own:
        out.add(own)
    for proj in projects:
        if proj and proj != own and re.search(
                r"(?<![a-z0-9-])" + re.escape(proj) + r"(?![a-z0-9-])", hay):
            out.add(proj)
    return out | _file_names(row)


def _problem_subjects(row: dict, titles: dict, claims: dict) -> set:
    """The problems a console is on: what it holds, what its focus or task names by id — but
    a MENTION counts only while the problem is unheld or held by this console (somebody else
    holding it means they are on it, and the mentioner is not) — and 'queue-triage' when it
    is triaging the error queue as such."""
    sid = _sid(row)
    out = {pid for pid, c in claims.items() if str((c or {}).get("session") or "")[:8] == sid and sid}
    title = str(titles.get(str(row.get("task_id") or "")) or "")
    for pid in set(_PID.findall(str(row.get("focus") or ""))) | set(_PID.findall(title)):
        held_by = str((claims.get(pid) or {}).get("session") or "")[:8]
        if not held_by or held_by == sid:
            out.add(pid)
    if _TRIAGE.search(title) or _TRIAGE.search(str(row.get("focus") or "")):
        out.add("queue-triage")
    return out


def _same_project(a: dict, b: dict) -> bool:
    pa, pb = str(a.get("project") or ""), str(b.get("project") or "")
    return bool(pa) and pa == pb


def _where(row: dict) -> str:
    p = str(row.get("project") or "").strip()
    if p:
        return p
    cwd = str(row.get("cwd") or "").replace("\\", "/").rstrip("/")
    return (cwd.rsplit("/", 1)[-1] if cwd else "") or "no project"


def _who(row: dict) -> str:
    return "%s%s%s" % (row.get("agent") or "?", ("@" + row["machine"]) if row.get("machine") else "",
                       (" " + row["name"]) if row.get("name") else (" " + _sid(row) if _sid(row) else ""))


def doing(row: dict, titles: dict) -> str:
    """The best available answer to "what is that console doing?" — its focus, else the
    task it holds, else the files it is editing. A signal that says only "somebody is in your
    project" costs an interruption and gives the reader nothing to decide on."""
    focus = str(row.get("focus") or "").strip()
    if focus:
        return focus[:140]
    tid = str(row.get("task_id") or "")
    if titles.get(tid):
        return "holds %s: %s" % (tid, str(titles[tid])[:110])
    files = _files(row)[:3]
    if files:
        return "no focus declared; editing %s" % ", ".join(files)
    return "no focus declared and no files touched yet - in %s" % _where(row)


def reach(me: dict, other: dict) -> dict:
    """How THIS side reaches the other. On one machine a console NAME is an address only
    when it is unique there — two live sessions sharing a name make it an address that
    silently lands in the wrong console, so the session id is the fallback."""
    same_machine = bool(other.get("machine")) and other.get("machine") == me.get("machine")
    by_name = bool(same_machine and other.get("name") and not other.get("name_ambiguous"))
    return {"agent": other.get("agent"), "machine": other.get("machine"),
            "session": _sid(other), "name": other.get("name") if by_name else "",
            "hint": ("in-console by name %r (same machine)" % other["name"]) if by_name else
                    ("pinned directive: python -m hub_core.client directive --target %s "
                     "--session %s --title \"...\" --body \"...\"" % (other.get("agent") or "?", _sid(other)))}


def _identity(row: dict, other: dict) -> str:
    """Who this party IS for the purpose of not repeating a signal. It follows the ADDRESS:
    a peer on another machine is reached by agent, so its session churn must not
    re-announce; a peer on this machine is reached by console, so a new console is new."""
    same_machine = bool(other.get("machine")) and other.get("machine") == row.get("machine")
    if same_machine:
        return ("n:" + str(row["name"])) if (row.get("name") and not row.get("name_ambiguous")) \
            else ("s:" + _sid(row))
    return "a:" + str(row.get("agent") or "?").lower()


def _signal(kind: str, a: dict, b: dict, key: str, detail: str) -> dict:
    parties = sorted([_identity(a, b), _identity(b, a)])
    digest = hashlib.sha1(("%s|%s|%s|%s" % (kind, parties[0], parties[1], key))
                          .encode("utf-8", "replace")).hexdigest()[:16]
    return {"id": "ov-" + digest, "kind": kind, "key": key, "detail": detail,
            "a": a, "b": b, "strength": STRENGTH.get(kind, 9)}


def signals(rows: list, *, titles: dict | None = None, claims: dict | None = None) -> list:
    """Every crossover between distinct live consoles, strongest first. `rows` are consoles
    (agent, machine, session, name, project, cwd, focus, files, task_id, unattended)."""
    rows = [dict(r) for r in (rows or []) if _sid(r)]
    titles = titles or {}
    claims = claims or {}
    names = {}
    for r in rows:
        n = str(r.get("name") or "").strip()
        if n:
            names.setdefault((str(r.get("machine") or ""), n), set()).add(_sid(r))
    for r in rows:
        n = str(r.get("name") or "").strip()
        if n and len(names.get((str(r.get("machine") or ""), n)) or ()) > 1:
            r["name_ambiguous"] = True
    projects = {str(r.get("project") or "").strip() for r in rows if r.get("project")}
    term_sets = {_sid(r): _console_terms(r, projects) for r in rows}
    freq = {}
    for ts in term_sets.values():
        for t in ts:
            freq[t] = freq.get(t, 0) + 1
    # AMBIENT words (on a third of the live consoles) say nothing. The floor is THREE: a term
    # two consoles share is exactly the pair signal, so it can never be ambient by itself —
    # with a floor of two, any small fleet silenced every topic pair it had.
    common = {t for t, n in freq.items() if len(rows) >= 4 and n >= max(3, len(rows) / 3.0)}
    known = systems()
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if _sid(a) == _sid(b):
                continue
            same_person = (str(a.get("agent") or "").lower() == str(b.get("agent") or "").lower()
                           and str(a.get("machine") or "").lower() == str(b.get("machine") or "").lower())
            unattended = bool(a.get("unattended")) or bool(b.get("unattended"))
            if a.get("unattended") and b.get("unattended"):
                continue
            files = sorted(set(_files(a)) & set(_files(b)))
            if files:
                out.append(_signal("file", a, b, ",".join(files[:3]),
                                   "both edited %s in the last 10 min" % ", ".join(files[:3])))
                continue
            shared_p = _problem_subjects(a, titles, claims) & _problem_subjects(b, titles, claims)
            if shared_p:
                pids = sorted(shared_p)
                named = [x for x in pids if x != "queue-triage"]
                out.append(_signal("problem", a, b, ",".join(pids[:2]),
                                   "both triaging the error queue" if not named
                                   else "both on problem %s" % ", ".join(named)[:60]))
                continue
            ta, tb = str(a.get("task_id") or ""), str(b.get("task_id") or "")
            if ta and ta == tb:
                out.append(_signal("task", a, b, ta, "both working on task %s (%s)"
                                   % (ta, str(titles.get(ta) or ta)[:70])))
                continue
            if _same_project(a, b):
                if same_person or unattended:
                    continue
                own_words = _terms(str(a.get("project") or "")) | {
                    s for s in known if s in str(a.get("project") or "").lower()}
                subject = (((term_sets[_sid(a)] & term_sets[_sid(b)])
                            | (_subject_terms(a) & _subject_terms(b)))
                           - common - {str(a.get("project") or "")} - own_words)
                subs = _subsystems(a) & _subsystems(b)
                if subs:
                    out.append(_signal("project", a, b, ",".join(sorted(subs)[:2]),
                                       "both working in %s under %s" % (_where(a), ", ".join(sorted(subs)[:2]))))
                    continue
                if subject:
                    out.append(_signal("project", a, b, ",".join(sorted(subject)[:2]),
                                       "both in %s and both on %s" % (_where(a), ", ".join(sorted(subject)[:2]))))
                    continue
                for holder, other, tid, others_tid in ((a, b, ta, tb), (b, a, tb, ta)):
                    if not tid or others_tid or holder.get("agent") == other.get("agent"):
                        continue
                    title = str(titles.get(tid) or tid)
                    shared = (_terms(title) & _subject_terms(other)) - common - {str(other.get("project") or "")}
                    if not shared:
                        continue          # positive evidence only: co-location is not overlap
                    out.append(_signal("task", holder, other, tid,
                                       "%s holds %s (%s) about %s; %s is working on %s there with no task"
                                       % (_who(holder), tid, title[:70], _where(holder),
                                          _who(other), ", ".join(sorted(shared)[:2]))))
                    break
                continue
            if same_person or unattended or not a.get("project") or not b.get("project"):
                continue
            shared = (term_sets[_sid(a)] & term_sets[_sid(b)]) - common
            sys_shared = {t for t in shared if t in known}
            file_shared = _file_names(a) & _file_names(b)
            if sys_shared or file_shared:
                what = []
                if sys_shared:
                    what.append("both on %s" % "/".join(sorted(sys_shared)[:2]))
                if file_shared:
                    what.append("both touching %s" % ", ".join(sorted(file_shared)[:2]))
                out.append(_signal("topic", a, b, ",".join(sorted(sys_shared)[:2] + sorted(file_shared)[:2]),
                                   "%s (%s vs %s)" % ("; ".join(what), _where(a), _where(b))))
    out.sort(key=lambda s: (s["strength"], s["id"]))
    seen_topic, capped = {}, []
    for sig in out:
        if sig["kind"] != "topic":
            capped.append(sig)
            continue
        a_id, b_id = _sid(sig["a"]), _sid(sig["b"])
        if seen_topic.get(a_id, 0) >= TOPIC_CAP_PER_CONSOLE or seen_topic.get(b_id, 0) >= TOPIC_CAP_PER_CONSOLE:
            continue
        seen_topic[a_id] = seen_topic.get(a_id, 0) + 1
        seen_topic[b_id] = seen_topic.get(b_id, 0) + 1
        capped.append(sig)
    return capped


_ACTIONS = {
    "file": "Agree one editor for the shared files; the other takes a separate component, "
            "diagnosis or review. Share the patch before integration.",
    "problem": "Share the reproduction and evidence already gathered. Split diagnosis, "
               "implementation and focused verification; keep one owner for resolution.",
    "task": "Split the task into complementary deliverables and name an integrator. Keep the "
            "current task owner; contributors report their evidence back to that task.",
    "project": "Compare the interfaces you are changing, then divide components and review. "
               "Continue independently on the agreed parts.",
    "topic": "Exchange the relevant finding or reusable implementation. Team up only where it "
             "advances both current tasks.",
}
_LABELS = {"file": "COORDINATE EDITS", "problem": "SHARED PROBLEM", "task": "SHARED TASK",
           "project": "COORDINATE COMPONENTS", "topic": "SHARE FINDINGS"}


def items(sigs: list, *, agent: str = "", session: str = "", titles: dict | None = None) -> list:
    """The signals that concern ONE console (by session) or every console of one agent,
    phrased from that side, as addressed items (kind `overlap`). Never addressed to an
    unattended console: nobody is reading it."""
    agent = str(agent or "").strip().lower()
    sid = str(session or "")[:8]
    titles = titles or {}
    out = []
    for s in sigs:
        for me, other in ((s["a"], s["b"]), (s["b"], s["a"])):
            if sid and _sid(me) != sid:
                continue
            if not sid and str(me.get("agent") or "").lower() != agent:
                continue
            if me.get("unattended"):
                continue
            how = reach(me, other)
            out.append({
                "kind": "overlap", "id": s["id"] + ":" + _sid(me), "overlap_id": s["id"],
                "overlap_kind": s["kind"], "strength": s["strength"], "session": _sid(me),
                "from": "the hub",
                "title": "%s with %s: %s" % (_LABELS.get(s["kind"], s["kind"].upper()),
                                             _who(other), s["detail"]),
                "body": ("%s - %s\n%s is on: %s\nReach them: %s\nNext: %s"
                         % (_LABELS.get(s["kind"], ""), s["detail"], _who(other),
                            doing(other, titles), how["hint"], _ACTIONS.get(s["kind"], ""))),
                "with": {"agent": other.get("agent"), "machine": other.get("machine"),
                         "name": other.get("name"), "session": _sid(other),
                         "project": other.get("project"), "focus": doing(other, titles),
                         "files": _files(other)[:3], "task_id": other.get("task_id") or ""},
                "reach": how, "action": _ACTIONS.get(s["kind"], ""),
            })
    return out


def _seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "overlap-seen.json"


def _read_seen(hub_dir) -> dict:
    try:
        value = json.loads(_seen_path(hub_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def unseen(hub_dir, addressed: list, now: float | None = None) -> list:
    """The addressed items this side has not been told (or may be told again)."""
    now = now or time.time()
    seen = _read_seen(hub_dir)
    return [it for it in addressed
            if not seen.get(it["id"]) or (now - float(seen.get(it["id"]) or 0)) > REANNOUNCE_S]


def mark_seen(hub_dir, item_ids: list, now: float | None = None) -> int:
    """Record delivery of these addressed ids (a client calls this once it has put the
    signal in front of its console)."""
    now = now or time.time()
    ids = [str(i) for i in (item_ids or []) if str(i).startswith("ov-")][:200]
    if not ids:
        return 0
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".overlap-seen.lock", timeout=3):
        seen = _read_seen(hub_dir)
        for i in ids:
            seen[i] = now
        if len(seen) > _SEEN_MAX:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-_SEEN_MAX:])
        path = _seen_path(hub_dir)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(temp, path)
    return len(ids)


def consoles(live_sessions: list, leases: list, titles: dict) -> list:
    """Live consoles enriched with the task each holds. A lease names an agent, not a
    console, so a task is bound to a console only when the lease names its session or the
    agent has exactly one live console — never guessed across several windows."""
    per_agent: dict = {}
    for s in live_sessions or []:
        per_agent.setdefault(str(s.get("agent") or "").lower(), []).append(s)
    out = []
    for s in live_sessions or []:
        row = dict(s)
        agent = str(row.get("agent") or "").lower()
        for lease in leases or []:
            if str(lease.get("agent") or "").lower() != agent:
                continue
            lease_sid = str(lease.get("session") or "")[:8]
            if (lease_sid and lease_sid == _sid(row)) or (not lease_sid and len(per_agent.get(agent, [])) == 1):
                row["task_id"] = lease.get("task") or ""
                break
        # The project is only what the console DECLARED (the client derives it from the
        # repository it stands in). A console standing above every project has none, and is
        # never "aligned" with anybody — a directory leaf is not a project.
        row["task_title"] = str(titles.get(row.get("task_id") or "") or "")
        out.append(row)
    return out
