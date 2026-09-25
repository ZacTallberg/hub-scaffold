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
Each addressed item carries evidence, not an assignment: the peer's focus, task, latest
checkpoint (labelled a PEER REPORT — a claim to read, not proof), how to reach them, and a
suggested split for its kind. The coordination protocol is ``patterns/coordination.md``.

Words the HUB itself writes into a focus ("finished <id>", "holds <id>", "claimed") are a status,
not a subject, and a relative path is only the same FILE inside the same project.

Two call shapes are served: ``signals(rows, tasks)`` + ``items_for_agent``/``public`` (every
attended console of one agent, the roster view) and ``signals(rows, titles=, claims=)`` +
``items`` (one console, problems included). Both read the same comparison.
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
org example merge task tasks step steps finish held claimed claim released started start idle
""".split())
_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")
# Filenames every repository has: two consoles editing their OWN copy is not a signal.
_COMMON_FILES = {"__init__.py", "settings.py", "urls.py", "views.py", "models.py", "admin.py",
                 "tests.py", "apps.py", "readme.md", "main.py", "utils.py", "forms.py",
                 "config.py", "index.html", "base.html", "app.css", "app.js", "conftest.py",
                 "requirements.txt", "package.json", ".gitignore", ".env", "manage.py",
                 "claude.md", "agents.md", "changelog.md", "notes.md", "dockerfile",
                 "makefile", "pyproject.toml", "serializers.py", "signals.py", "setup.py",
                 "setup.cfg"}
# A console's scratch area is private by construction, and a throwaway worktree folder is
# not a project: neither can be shared work, and a worktree NAMED after a subject would
# otherwise become a standing false subject for everyone who later works inside it.
_PRIVATE_PATH = re.compile(r"(?i)(^|/)(?:temp|tmp|appdata|scratchpad|__pycache__|node_modules|"
                           r"\.git|\.venv|venv)/")
_WORKTREE_SEG = re.compile(r"(?i)(^|/)(?:_?wt[-_.][^/]*|_?wt|\.worktrees?|worktrees?|_tmp[^/]*)(?=/)")
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


def _wt_split(rel: str) -> tuple:
    """(worktree root, repo-relative rest) for a path inside a worktree, else ('', rel).
    ``_wt/fix-x/hub/x.py`` -> ('_wt/fix-x', 'hub/x.py'); ``wt-feature/x.py`` -> ('wt-feature', 'x.py')."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    for i, seg in enumerate(parts):
        if re.fullmatch(r"(?i)_?wt[-_.].*|_tmp.*", seg):
            return "/".join(parts[:i + 1]), "/".join(parts[i + 1:])
        if re.fullmatch(r"(?i)_?wt|\.worktrees?|worktrees?", seg) and i + 1 < len(parts):
            return "/".join(parts[:i + 2]), "/".join(parts[i + 2:])
    return "", "/".join(parts)


def _worktree_projects(rows: list) -> dict:
    """{worktree root: project} from the consoles STANDING in a worktree: their cwd says which
    worktree, their project (read from the git remote by the gate) says what it is a checkout of."""
    out = {}
    for r in rows or []:
        cwd = str(r.get("cwd") or "").replace("\\", "/")
        m = _WORKTREE_SEG.search("/" + cwd)
        if not m:
            continue
        root, _rest = _wt_split(cwd[m.start():].lstrip("/") if m.start() else cwd)
        proj = str(r.get("project") or "").strip().lower()
        if root and proj:
            out.setdefault(root.lower(), proj)
    return out


def _file_keys(row: dict, wt_projects: dict) -> dict:
    """{(project, repo-relative path): the path as reported} for this console's shareable files.

    THE SAME FILE IN A WORKTREE AND IN THE MAIN CHECKOUT IS ONE FILE. A path under a worktree
    used to be dropped outright, so two consoles editing ``hub/x.py`` -- one in the main
    checkout, one in a worktree of the same repository -- never collided. Both now normalise to
    (project, path inside the repo): a worktree's project comes from any console standing in
    that worktree, else from this console's own project; a main-checkout path's project is its
    first segment (the gate reports ``<project>/<path>``). A worktree nothing resolves keeps a
    key of its own, so two consoles in the same worktree still pair."""
    out = {}
    own = str(row.get("project") or "").strip().lower()
    # The folders this console stands in: with the gate's default code root (the parent of the
    # checkout), a worktree's files arrive as ``<worktree folder>/<path>`` -- the folder is
    # this console's checkout, so it names this console's PROJECT, not a project of its own.
    here = {seg.lower() for seg in str(row.get("cwd") or "").replace("\\", "/").split("/") if seg}
    for f in row.get("files") or []:
        raw = str(f or "").replace("\\", "/")
        if not raw or _PRIVATE_PATH.search("/" + raw):
            continue
        root, rest = _wt_split(raw)
        parts = [x for x in raw.split("/") if x]
        if root:
            key = (wt_projects.get(root.lower()) or own or "wt:" + root.lower(), rest)
        elif len(parts) > 1 and own and parts[0].lower() in here:
            key = (own, "/".join(parts[1:]))
        elif len(parts) > 1:
            key = (parts[0].lower(), "/".join(parts[1:]))
        else:
            key = ("", raw)
        if key[1]:
            out[key] = raw
    return out


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


def signals(rows: list, tasks: list | None = None, *, titles: dict | None = None,
            claims: dict | None = None) -> list:
    """Every crossover between distinct live consoles, strongest first. `rows` are consoles
    (agent, machine, session, name, project, cwd, focus, files, task_id, unattended); `tasks`
    (task entities) supply titles and the peer checkpoint the coordination block quotes."""
    rows = [dict(r) for r in (rows or []) if _sid(r)]
    by_id = {t.get("id"): t for t in (tasks or []) if isinstance(t, dict) and t.get("id")}
    titles = dict({tid: t.get("title") for tid, t in by_id.items()}, **(titles or {}))
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
    wt_projects = _worktree_projects(rows)
    file_keys = {_sid(r): _file_keys(r, wt_projects) for r in rows}
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
            # ONE FILE IS (project, path inside the repo), wherever it is checked out: a
            # worktree and the main checkout of one repository share their files.
            ka, kb = file_keys.get(_sid(a)) or {}, file_keys.get(_sid(b)) or {}
            shared = sorted(k for k in set(ka) & set(kb) if k[0])
            files = ["%s/%s" % k for k in shared]
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
    for sig in capped:
        sig["tasks_by_id"] = by_id
        sig["titles"] = titles
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
ACTIONS = _ACTIONS
LABELS = _LABELS


def coordination(sig: dict, other: dict) -> dict:
    """A proposed next action plus the peer's own evidence — never an automatic assignment. The
    latest checkpoint is quoted verbatim and labelled a peer report: a claim to read, not proof."""
    by_id = sig.get("tasks_by_id") or {}
    task_id = str(other.get("task_id") or "")
    task = by_id.get(task_id) or {}
    notes = [(n, p) for n, p in enumerate(task.get("plan") or [], 1)
             if isinstance(p, dict) and p.get("note")]
    out = {"action": _ACTIONS.get(sig["kind"], ""), "task_id": task_id,
           "task_title": str(task.get("title") or (sig.get("titles") or {}).get(task_id) or ""),
           "recall": ("python -m hub_core.client recall %s" % task_id) if task_id else ""}
    if notes:
        number, step = max(notes, key=lambda item: (str(item[1].get("note_at") or ""), item[0]))
        out["checkpoint"] = {"number": number, "note": str(step["note"])[:600],
                             "at": str(step.get("note_at") or ""), "sha": str(step.get("sha") or ""),
                             "label": "peer report — read its evidence before treating it as verified"}
    return out


def public(sig: dict) -> dict:
    """A signal as the roster shows it: both sides, no internal bookkeeping."""
    def side(r):
        return {k: r.get(k) for k in ("agent", "machine", "session", "name", "project", "focus",
                                      "task_id", "unattended") if k in r}
    return {"id": sig["id"], "kind": sig["kind"], "detail": sig["detail"],
            "strength": sig["strength"], "a": side(sig["a"]), "b": side(sig["b"])}


def items_for_agent(sigs: list, agent: str) -> list:
    """Addressed inbox items (kind ``overlap``) for every ATTENDED console of one agent, each
    with the coordination block (suggested split, the peer's task and latest checkpoint)."""
    out = []
    for item in items(sigs, agent=agent):
        sig = next((s for s in sigs if s["id"] == item["overlap_id"]), None)
        if sig is None:
            continue
        other = sig["b"] if _sid(sig["a"]) == item["session"] else sig["a"]
        plan = coordination(sig, other)
        context = ""
        if plan["task_id"]:
            context = "\nPeer task: %s — %s\nRead: %s" % (plan["task_id"], plan["task_title"],
                                                           plan["recall"])
        if plan.get("checkpoint"):
            cp = plan["checkpoint"]
            context += "\nPeer checkpoint %s (%s, %s): %s" % (
                cp["number"], cp["at"] or "undated", cp["label"], cp["note"])
        item = dict(item, coordination=plan, reply_cmd=item["reach"]["hint"], at="")
        item["body"] = (item["body"] + context + "\nSend your proposed split and current "
                        "evidence; agree file ownership and who integrates. Record the split on "
                        "the existing task's checkpoints. Keep moving on your agreed part while "
                        "the peer works theirs.")
        out.append(item)
    return out


def items(sigs: list, *, agent: str = "", session: str = "", titles: dict | None = None) -> list:
    """The signals that concern ONE console (by session) or every console of one agent,
    phrased from that side, as addressed items (kind `overlap`). Never addressed to an
    unattended console: nobody is reading it."""
    agent = str(agent or "").strip().lower()
    sid = str(session or "")[:8]
    out = []
    for s in sigs:
        titles_s = dict(s.get("titles") or {}, **(titles or {}))
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
                            doing(other, titles_s), how["hint"], _ACTIONS.get(s["kind"], ""))),
                "session_id": str(me.get("session_id") or _sid(me))[:64],
                "with": {"agent": other.get("agent"), "machine": other.get("machine"),
                         "name": other.get("name"), "session": _sid(other),
                         "session_id": str(other.get("session_id") or _sid(other))[:64],
                         "unattended": bool(other.get("unattended")),
                         "project": other.get("project"), "focus": doing(other, titles_s),
                         "files": _files(other)[:3], "task_id": other.get("task_id") or ""},
                "reach": how, "action": _ACTIONS.get(s["kind"], ""),
            })
    return out


def _seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "overlap-seen.json"


def read_seen(hub_dir) -> dict:
    try:
        value = json.loads(_seen_path(hub_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def unseen(hub_dir, addressed: list, now: float | None = None) -> list:
    """The addressed items this side has not been told (or may be told again)."""
    now = now or time.time()
    seen = read_seen(hub_dir)
    out = []
    for it in addressed or []:
        # Delivery may be recorded per console (the item id) or for the signal as a whole.
        at = max(float(seen.get(it["id"]) or 0), float(seen.get(it.get("overlap_id") or "") or 0))
        if not at or now - at > REANNOUNCE_S:
            out.append(it)
    return out


def mark_seen(hub_dir, item_ids: list, now: float | None = None) -> int:
    """Record delivery of these addressed ids (a client calls this once it has put the
    signal in front of its console)."""
    now = now or time.time()
    ids = [str(i) for i in (item_ids or []) if str(i).startswith("ov-")][:200]
    if not ids:
        return 0
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".overlap-seen.lock", timeout=3):
        seen = read_seen(hub_dir)
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


# Public names for the comparison's building blocks (the roster and the attention list read them).
project_files = _files
subsystems = _subsystems
file_names = _file_names
subject_terms = _subject_terms
