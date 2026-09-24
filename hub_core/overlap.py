"""Shared work between live consoles, computed ONCE on the hub and addressed to the consoles
it concerns — so agents share evidence, divide useful work and coordinate edits, and nobody
pays for a roster of everyone else's work on every prompt.

The hub already holds every console's project, recently edited files, focus and held task
(``presence`` + leases). Comparing them belongs here; the answer belongs in the console it is
about (the inbox delivers kind ``overlap``), and the full pair list is one read away
(``/hub/consoles.json``).

Kinds, strongest first:

* ``file``     two consoles edited the same file in the last activity window
* ``task``     both consoles hold the same task (across repositories: a task is the unit), or
               one holds a task whose words match what the other is doing untracked
* ``project``  two consoles in the same project AND the same subsystem (first directory below
               the project) or the same subject — co-location alone is silent
* ``topic``    different projects that name the same configured system or the same distinctive
               file — alignment worth a message, never a collision; capped per console

Each signal carries evidence, not an assignment: the peer's focus, task, latest checkpoint
(labelled a PEER REPORT — a claim to read, not proof), how to reach them, and a suggested split
for its kind. The coordination protocol is ``patterns/coordination.md``.

Rules this module holds, each paid for once:

* A worktree or scratch path is not a project and its directory name is not a subject. A
  console reviewing code under ``_wt/fix-billing/`` was paired "both on billing" with a console
  actually on billing, because the worktree folder named it.
* Words the HUB itself writes into a focus ("finished <id>", "holds <id>", "claimed") are a
  status, not a subject. Two consoles that had each just finished unrelated tasks were paired
  "both on finished".
* One person's own windows in one repo is how they work: only the same FILE or TASK between
  them fires. An unattended run cannot coordinate, so it is never addressed, and its pairs reach
  the attended side only on the strongest kinds.
* A signal has a stable id while its condition holds, so it is announced once per side and
  again only when it changes or REANNOUNCE_S has passed. Delivery state lives in a sidecar,
  never the ledger.

Stdlib only; every function takes plain rows (``presence.attribute_leases`` output).
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
TOPIC_CAP_PER_CONSOLE = 2
_SEEN_MAX = 2000
STRENGTH = {"file": 0, "task": 1, "project": 2, "topic": 3}
LABELS = {"file": "COORDINATE EDITS", "task": "SHARED TASK", "project": "COORDINATE COMPONENTS",
          "topic": "SHARE FINDINGS"}
ACTIONS = {
    "file": "Agree one editor for the shared files; the other takes a separate component, "
            "diagnosis or review. Share the patch before integration.",
    "task": "Split the task into complementary deliverables and name an integrator. Keep the "
            "current task owner; contributors report their evidence back to that task.",
    "project": "Compare the interfaces you are changing, then divide components or "
               "implementation and review. Continue independently on the agreed parts.",
    "topic": "Exchange the relevant finding or reusable implementation. Team up only where it "
             "advances both current tasks.",
}

_STOP = set("""
the a an and or of to in on for with from by at is are was were be been being it its this that
these those i we you they he she them our your their my me us make made making do does did done
doing get got go going need needs want wants please just also only still very some any all more
most less then than there here into out over under about after before again once when where why
how what which who whom while because until unless not no yes can could should would will shall
may might must let lets like new old one two three first next last now today up down off
add fix update change check run test try use using used set see look show find keep work working
app apps code file files thing things stuff something everything anything way ways
https http www com net org example local html json yaml index main self none true false null
repo repos branch commit deploy deployed push pushed pull merge task tasks step steps
finished finish holds held claimed claim released started start longer problem idle
""".split())

_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")
# Filenames every repository has: two consoles editing their OWN copy is not alignment.
_COMMON_FILES = {"__init__.py", "settings.py", "urls.py", "views.py", "models.py", "admin.py",
                 "tests.py", "apps.py", "readme.md", "main.py", "utils.py", "forms.py",
                 "config.py", "index.html", "base.html", "app.css", "app.js", "conftest.py",
                 "requirements.txt", "package.json", ".gitignore", ".env", "manage.py",
                 "agents.md", "claude.md", "changelog.md", "notes.md", "dockerfile", "makefile",
                 "pyproject.toml", "setup.py", "setup.cfg"}
# A scratch/temp/vendored path can never be shared work.
_PRIVATE_PATH = re.compile(r"(?i)(?:^|[\\/])(?:temp|tmp|appdata|scratchpad|__pycache__|"
                           r"node_modules|\.git|\.venv|venv)[\\/]")
# A throwaway worktree is not a project, and its folder name is not a subject.
_WORKTREE_SEG = re.compile(r"(?i)(?:^|[\\/])(?:_?wt[-_.][^\\/]*|_?wt|\.worktrees?|"
                           r"worktrees?|_tmp[^\\/]*)(?=[\\/])")


def systems() -> tuple:
    """Named external systems that make two different projects ALIGNED when both name one
    (e.g. an ERP, a ticket tracker). Deployment-specific, so configured, never baked in:
    HUB_OVERLAP_SYSTEMS is a comma list. Generic plumbing every console touches (the forge,
    the web framework) belongs nowhere near it — it pairs everyone with everyone."""
    raw = os.environ.get("HUB_OVERLAP_SYSTEMS") or ""
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _sid(row: dict) -> str:
    return str((row or {}).get("session") or "")[:8]


def _terms(text) -> set:
    out = set()
    for w in _WORD.findall(str(text or "").lower()):
        w = w.strip("_-")
        if len(w) >= 4 and w not in _STOP and not w.isdigit():
            out.add(w)
    return out


def project_files(row: dict) -> list:
    """Files this console touched that could POSSIBLY be shared with another console."""
    out = []
    for f in row.get("files") or []:
        path = str(f or "").replace("\\", "/")
        if path and not _PRIVATE_PATH.search(path) and not _WORKTREE_SEG.search(path):
            out.append(path)
    return out


def _norm(s) -> str:
    return str(s or "").strip().lower().replace("_", "-")


def subsystems(row: dict) -> set:
    """The parts of the repository this console works IN: the first segment below the project.
    Two consoles in one repo on different subtrees are co-located, not colliding."""
    out = set()
    project = _norm(row.get("project"))
    for f in project_files(row):
        parts = [x for x in f.split("/") if x and x not in (".", "..")]
        if parts and _norm(parts[0]) == project:
            parts = parts[1:]
        if len(parts) > 1:
            out.add(parts[0].lower())
        elif parts:
            out.add("(root)")
    return out


def file_names(row: dict) -> set:
    out = set()
    for f in project_files(row):
        base = f.rsplit("/", 1)[-1].lower().strip()
        if base and base not in _COMMON_FILES:
            out.add(base)
    return out


def _systems_named(row: dict) -> set:
    hay = " ".join([str(row.get("focus") or ""), " ".join(project_files(row)),
                    str(row.get("project") or "")]).lower()
    return {s for s in systems() if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", hay)}


def subject_terms(row: dict) -> set:
    """What a console is working ON as comparable words: its focus, its file stems (without
    extensions, so "billing_export" matches a task titled "billing_export fails") and systems."""
    out = _terms(row.get("focus"))
    for base in file_names(row):
        out.add(base)
        stem = base.rsplit(".", 1)[0]
        if len(stem) >= 4:
            out.add(stem)
    return out | _systems_named(row)


def _same_project(a: dict, b: dict) -> bool:
    pa, pb = _norm(a.get("project")), _norm(b.get("project"))
    return bool(pa) and pa == pb


def _who(row: dict) -> str:
    return "%s%s (console %s)" % (row.get("agent") or "?",
                                  ("@" + row["machine"]) if row.get("machine") else "", _sid(row))


def _where(row: dict) -> str:
    project = str(row.get("project") or "").strip()
    if project:
        return project
    cwd = str(row.get("cwd") or "").replace("\\", "/").rstrip("/")
    return cwd.rsplit("/", 1)[-1] if cwd else "no project"


def doing(row: dict, by_id: dict | None = None) -> str:
    """The best available answer to "what is that console doing?": its focus, else the task it
    holds, else the files it edits — a signal that says someone is here but not what they are
    doing costs an interruption and leaves nothing to decide on."""
    focus = str(row.get("focus") or row.get("doing") or "").strip()
    if focus:
        return focus[:140]
    tid = str(row.get("task_id") or "")
    title = str(((by_id or {}).get(tid) or {}).get("title") or "").strip()
    if title:
        return "holds %s: %s" % (tid, title[:110])
    files = project_files(row)[:3]
    if files:
        return "no focus declared; editing %s" % ", ".join(files)
    return "no focus declared and no files touched yet — in %s" % _where(row)


def reach(me: dict, other: dict) -> str:
    """How THIS side reaches the other. The session is part of the address: an agent may run
    several consoles, and a message addressed by name alone can land in the wrong one."""
    return ('python -m hub_core.client directive --target %s --title "re: %s" --body "..."   '
            "(names console %s in the body; needs directive:write) — or reply on the shared task"
            % (other.get("agent") or "?", str(other.get("task_id") or "your work")[:40], _sid(other)))


def _identity(row: dict, other: dict) -> str:
    """Who a signal is ABOUT, for not repeating it: a peer on another machine is the agent (its
    session churn must not re-announce); on this machine a different console is a different
    address and deserves its own signal."""
    same_machine = bool(other.get("machine")) and other.get("machine") == row.get("machine")
    return ("s:" + _sid(row)) if same_machine else ("a:" + str(row.get("agent") or "?").lower())


def _signal(kind: str, a: dict, b: dict, key: str, detail: str) -> dict:
    parties = sorted([_identity(a, b), _identity(b, a)])
    ident = hashlib.sha1(("%s|%s|%s|%s" % (kind, parties[0], parties[1], key))
                         .encode("utf-8", "replace")).hexdigest()[:16]
    return {"id": "ov-" + ident, "kind": kind, "key": key, "detail": detail,
            "a": a, "b": b, "strength": STRENGTH.get(kind, 9)}


def signals(rows: list, tasks: list | None = None) -> list:
    """Every crossover between distinct live consoles, strongest first."""
    rows = [dict(r) for r in (rows or []) if _sid(r)]
    by_id = {t.get("id"): t for t in (tasks or []) if isinstance(t, dict) and t.get("id")}
    # A word on a third of the live consoles is ambient, not a shared subject.
    freq = {}
    subj = {_sid(r): subject_terms(r) for r in rows}
    for terms in subj.values():
        for t in terms:
            freq[t] = freq.get(t, 0) + 1
    common = {t for t, n in freq.items() if len(rows) >= 4 and n >= max(2, len(rows) / 3.0)}
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if _sid(a) == _sid(b):
                continue
            same_person = (str(a.get("agent") or "").lower() == str(b.get("agent") or "").lower()
                           and str(a.get("machine") or "").lower() == str(b.get("machine") or "").lower())
            if a.get("unattended") and b.get("unattended"):
                continue
            unattended = bool(a.get("unattended") or b.get("unattended"))
            files = sorted(set(project_files(a)) & set(project_files(b)))
            # A relative path is only the same FILE inside the same project: `hub/views.py` in
            # two repositories is two files.
            if files and (_same_project(a, b) or not (a.get("project") or b.get("project"))):
                out.append(_signal("file", a, b, ",".join(files[:3]),
                                   "both edited %s in the last few minutes" % ", ".join(files[:3])))
                continue
            ta, tb = str(a.get("task_id") or ""), str(b.get("task_id") or "")
            if ta and ta == tb:
                title = str((by_id.get(ta) or {}).get("title") or ta)[:70]
                out.append(_signal("task", a, b, ta, "both working on %s (%s)" % (ta, title)))
                continue
            if _same_project(a, b):
                if same_person or unattended:
                    continue            # one person's windows / a run nobody reads: silent
                own = _terms(a.get("project"))
                subject = (subj[_sid(a)] & subj[_sid(b)]) - common - own - {_norm(a.get("project"))}
                subs = subsystems(a) & subsystems(b)
                if subs:
                    out.append(_signal("project", a, b, ",".join(sorted(subs)[:2]),
                                       "both working in %s under %s"
                                       % (_where(a), ", ".join(sorted(subs)[:2]))))
                    continue
                if subject:
                    out.append(_signal("project", a, b, ",".join(sorted(subject)[:2]),
                                       "both in %s and both on %s"
                                       % (_where(a), ", ".join(sorted(subject)[:2]))))
                    continue
                for holder, other, tid, others in ((a, b, ta, tb), (b, a, tb, ta)):
                    if not tid or others:
                        continue
                    title = str((by_id.get(tid) or {}).get("title") or tid)
                    shared = (_terms(title) & subject_terms(other)) - common - own
                    if not shared:
                        continue        # positive evidence only: "someone is in this repo" is noise
                    out.append(_signal("task", holder, other, tid,
                                       "%s holds %s (%s); %s is working on %s there with no task"
                                       % (_who(holder), tid, title[:70], _who(other),
                                          ", ".join(sorted(shared)[:2]))))
                    break
                continue
            if same_person or unattended or not a.get("project") or not b.get("project"):
                continue
            sys_shared = _systems_named(a) & _systems_named(b)
            file_shared = (file_names(a) & file_names(b)) - common
            if sys_shared or file_shared:
                what = []
                if sys_shared:
                    what.append("both on %s" % "/".join(sorted(sys_shared)[:2]))
                if file_shared:
                    what.append("both touching %s" % ", ".join(sorted(file_shared)[:2]))
                out.append(_signal("topic", a, b,
                                   ",".join(sorted(sys_shared)[:2] + sorted(file_shared)[:2]),
                                   "%s (%s vs %s)" % ("; ".join(what), _where(a), _where(b))))
    out.sort(key=lambda s: (s["strength"], s["id"]))
    capped, seen_topic = [], {}
    for sig in out:
        if sig["kind"] == "topic":
            ia, ib = _sid(sig["a"]), _sid(sig["b"])
            if max(seen_topic.get(ia, 0), seen_topic.get(ib, 0)) >= TOPIC_CAP_PER_CONSOLE:
                continue
            seen_topic[ia] = seen_topic.get(ia, 0) + 1
            seen_topic[ib] = seen_topic.get(ib, 0) + 1
        capped.append(sig)
    for sig in capped:
        sig["tasks_by_id"] = by_id
    return capped


def coordination(sig: dict, other: dict) -> dict:
    """A proposed next action plus the peer's own evidence — never an automatic assignment. The
    latest checkpoint is quoted verbatim and labelled a peer report: a claim to read, not proof."""
    by_id = sig.get("tasks_by_id") or {}
    task_id = str(other.get("task_id") or "")
    task = by_id.get(task_id) or {}
    notes = [(n, p) for n, p in enumerate(task.get("plan") or [], 1)
             if isinstance(p, dict) and p.get("note")]
    out = {"action": ACTIONS.get(sig["kind"], ""), "task_id": task_id,
           "task_title": str(task.get("title") or ""),
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
        return {k: r.get(k) for k in ("agent", "machine", "session", "project", "focus",
                                      "task_id", "unattended") if k in r}
    return {"id": sig["id"], "kind": sig["kind"], "detail": sig["detail"],
            "strength": sig["strength"], "a": side(sig["a"]), "b": side(sig["b"])}


def items_for_agent(sigs: list, agent: str) -> list:
    """Addressed inbox items (kind ``overlap``) for every ATTENDED console of one agent. Each
    carries ``session`` so a supervisor can land it in the right console."""
    agent = str(agent or "").strip().lower()
    out = []
    for sig in sigs or []:
        by_id = sig.get("tasks_by_id") or {}
        for me, other in ((sig["a"], sig["b"]), (sig["b"], sig["a"])):
            if str(me.get("agent") or "").lower() != agent or me.get("unattended"):
                continue
            label = LABELS.get(sig["kind"], sig["kind"].upper())
            plan = coordination(sig, other)
            context = ""
            if plan["task_id"]:
                context = "\nPeer task: %s — %s\nRead: %s" % (plan["task_id"], plan["task_title"],
                                                               plan["recall"])
            if plan.get("checkpoint"):
                cp = plan["checkpoint"]
                context += "\nPeer checkpoint %s (%s, %s): %s" % (
                    cp["number"], cp["at"] or "undated", cp["label"], cp["note"])
            out.append({
                "kind": "overlap", "id": "%s:%s" % (sig["id"], _sid(me)), "overlap_id": sig["id"],
                "overlap_kind": sig["kind"], "session": _sid(me), "from": "the hub",
                "title": "%s with %s: %s" % (label, _who(other), sig["detail"]),
                "body": ("%s — %s\n%s is on: %s%s\nReach them: %s\nNext: %s\nSend your proposed "
                         "split and current evidence; agree file ownership and who integrates. "
                         "Record the split on the existing task's checkpoints. Keep moving on "
                         "your agreed part while the peer works theirs."
                         % (label, sig["detail"], _who(other), doing(other, by_id), context,
                            reach(me, other), plan["action"])),
                "coordination": plan, "reply_cmd": reach(me, other), "at": ""})
    return out


# ------------------------------------------------------------------ announcement state

def _seen_path(hub_dir) -> Path:
    return Path(hub_dir) / "overlap-seen.json"


def read_seen(hub_dir) -> dict:
    try:
        data = json.loads(_seen_path(hub_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def unseen(hub_dir, items: list, now: float | None = None) -> list:
    """The addressed items this side has not been told (or may be told again)."""
    now = time.time() if now is None else now
    seen = read_seen(hub_dir)
    out = []
    for it in items or []:
        # Delivery may be recorded per console (the item id) or for the signal as a whole.
        at = max(float(seen.get(it["id"]) or 0), float(seen.get(it.get("overlap_id") or "") or 0))
        if not at or now - at > REANNOUNCE_S:
            out.append(it)
    return out


def mark_seen(hub_dir, item_ids, now: float | None = None) -> int:
    """Record delivery of these addressed overlap ids (a supervisor calls this once it has put
    the signal in front of the console). Sidecar only; never the ledger."""
    now = time.time() if now is None else now
    ids = [str(i)[:64] for i in (item_ids or []) if str(i).startswith("ov-")]
    if not ids:
        return 0
    path = _seen_path(hub_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(path.parent, name=".overlap-seen.lock", timeout=5):
        seen = read_seen(hub_dir)
        for i in ids:
            seen[i] = now
        if len(seen) > _SEEN_MAX:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-_SEEN_MAX:])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, path)
    return len(ids)
