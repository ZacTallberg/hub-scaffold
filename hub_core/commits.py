"""Does this commit exist, and is it contained in that one? Three answers, never two.

Several board surfaces ask a question about a commit: strict completion evidence ("is this sha a
real commit?"), the promotion lane ("can anybody but its author fetch this held commit?"), and a
task's lineage ("is the commit this task shipped inside the build that is serving now?"). They
all used to ask ONE repository -- the Hub's own work root -- so a task about another project that
finished with that project's own sha was refused as "not a commit in this repo", and an
unattended worker had nobody to re-send it.

THE TASK'S OWN PROJECT COUNTS. A task names the project it is about (its ``project`` field, or
the ``<project>: ...`` prefix of its title when that prefix is a configured project), and every
commit question is put to THAT project's repository as well as the Hub's own.

"COULD NOT ASK" IS NOT "NO". Every function here answers ``True`` / ``False`` / ``None``:

    True   the repository was asked and has it
    False  the repository was asked, is readable, and does not have it
    None   nothing could be asked -- no checkout, git missing, a remote resolver unreachable

``None`` must never be stored or displayed as "no": the first day a checkout is missing would
otherwise write a permanent refusal. Callers name it ("could not be asked") instead.

Where a project's commits live is the adopter's business, so it arrives as configuration:

* ``repos``: ``{project_slug: local_checkout_path}`` -- read with plain ``git``;
* ``remote``: an optional callable ``remote(project, sha) -> True | False | None`` for a project
  that has no local checkout (a Git forge's commits API, say). Its own ``None`` is honoured.

Standard library only; the adapter supplies the configuration.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
FULL = 40


def is_sha(value) -> bool:
    return bool(SHA_RE.match(str(value or "").strip().lower()))


def _git(path, *args, timeout=10):
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _readable_repo(path) -> bool:
    if not path:
        return False
    try:
        if not Path(path).exists():
            return False
    except OSError:
        return False
    r = _git(path, "rev-parse", "--git-dir", timeout=5)
    return bool(r is not None and r.returncode == 0)


def local_has(path, sha: str):
    """True / False / None for one local checkout."""
    sha = str(sha or "").strip().lower()
    if not is_sha(sha) or not _readable_repo(path):
        return None
    r = _git(path, "cat-file", "-e", sha + "^{commit}")
    if r is None:
        return None
    return r.returncode == 0


def local_fetchable(path, sha: str):
    """Can somebody other than this disk FETCH `sha`? Asked of one checkout's remote-tracking refs.

    True   the commit is contained in at least one ``refs/remotes/*`` ref: it is on a server
    False  the checkout HAS the commit and no remote-tracking ref contains it: it lives on this
           disk only -- exactly the case a held-commit record exists to catch
    None   the checkout lacks the commit (its remote refs may simply be stale), or git could not
           be asked; neither is evidence either way

    Existence is not reachability: ``cat-file -e`` answers yes for a commit made in a worktree
    and never pushed, so it must never stand in for this question."""
    sha = str(sha or "").strip().lower()
    if local_has(path, sha) is not True:
        return None
    r = _git(path, "for-each-ref", "--contains", sha, "--format=%(refname)", "refs/remotes")
    if r is None or r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def local_full(path, sha: str) -> str:
    """The full 40-hex name of `sha` in that checkout, or "" when it cannot be resolved."""
    if not is_sha(sha) or not _readable_repo(path):
        return ""
    r = _git(path, "rev-parse", "--verify", "--quiet", str(sha).lower() + "^{commit}")
    out = (r.stdout.strip().lower() if r is not None and r.returncode == 0 else "")
    return out if len(out) == FULL else ""


def local_contains(path, candidate: str, target: str):
    """Is `candidate` an ancestor of (or equal to) `target` in that checkout?"""
    cand, tgt = str(candidate or "").lower(), str(target or "").lower()
    if not is_sha(cand) or not is_sha(tgt) or not _readable_repo(path):
        return None
    if local_has(path, cand) is not True or local_has(path, tgt) is not True:
        return None          # a commit this checkout lacks is unknown here, never "not contained"
    r = _git(path, "merge-base", "--is-ancestor", cand, tgt)
    if r is None or r.returncode not in (0, 1):
        return None
    return r.returncode == 0


class Resolver:
    """One place every commit question is put, per project, with its answer's provenance."""

    def __init__(self, work_root=None, repos=None, remote=None):
        self.work_root = work_root
        self.repos = {str(k).strip().lower(): v for k, v in (repos or {}).items()
                      if str(k).strip()}
        self.remote = remote

    def projects(self) -> list:
        return sorted(self.repos)

    def project_of(self, task: dict) -> str:
        """The project a task is ABOUT, or "" when it names none this Hub knows.

        An explicit ``project`` field wins. A ``<slug>: title`` prefix counts only when the slug
        is a configured project: guessing a repository from free text is how a sha gets checked
        against the wrong one."""
        if not isinstance(task, dict):
            return ""
        explicit = str(task.get("project") or "").strip().lower()
        if explicit:
            return explicit
        title = str(task.get("title") or "")
        head = title.split(":", 1)[0].strip().lower() if ":" in title else ""
        if head and PROJECT_RE.match(head) and (head in self.repos):
            return head
        return ""

    def has(self, sha: str, project: str = "") -> tuple:
        """``(verdict, searched)`` -- is `sha` a commit of this Hub's repository or of
        `project`'s? `searched` names every place asked, so a refusal says where it looked.

        True if ANY place has it; False only when every place was asked and said no; None when
        no place said yes and at least one could not be asked."""
        sha = str(sha or "").strip().lower()
        searched, answers = [], []
        own = local_has(self.work_root, sha) if self.work_root else None
        searched.append("this hub's repository" + ("" if own is not None else " (not readable)"))
        answers.append(own)
        project = str(project or "").strip().lower()
        if project and own is not True:
            path = self.repos.get(project)
            if path:
                found = local_has(path, sha)
                searched.append("%s checkout%s" % (project, "" if found is not None
                                                   else " (not readable)"))
                answers.append(found)
            if self.remote is not None and True not in answers:
                try:
                    found = self.remote(project, sha)
                except Exception:                            # noqa: BLE001 - unreachable is not "no"
                    found = None
                found = found if found in (True, False) else None
                searched.append("%s remote%s" % (project, "" if found is not None
                                                 else " (could not be asked)"))
                answers.append(found)
            if not path and self.remote is None:
                searched.append("%s (no checkout or resolver configured)" % project)
                answers.append(None)
        if True in answers:
            return True, searched
        if None in answers:
            return None, searched
        return False, searched

    def fetchable(self, sha: str, project: str = "") -> tuple:
        """``(verdict, searched)`` -- could anybody but its author FETCH `sha`? The promotion
        lane's question, which ``has`` cannot answer: a commit present only in a local worktree
        exists, and is precisely what must not read as safe.

        Asked of the project's checkout (its remote-tracking refs), the Hub's own repository,
        and the configured remote resolver (a forge answers "the server has it", which is
        fetchability by definition). True if any place shows it on a server; False when a place
        holds it locally on no remote and nothing shows it pushed; None otherwise."""
        sha = str(sha or "").strip().lower()
        searched, answers = [], []
        project = str(project or "").strip().lower()
        places = []
        if project and self.repos.get(project):
            places.append(("%s checkout" % project, self.repos[project]))
        if self.work_root:
            places.append(("this hub's repository", self.work_root))
        for label, path in places:
            found = local_fetchable(path, sha)
            searched.append(label + {True: " (on a remote-tracking branch)",
                                     False: " (present locally, on NO remote-tracking branch)",
                                     None: " (does not have it, or not readable)"}[found])
            answers.append(found)
        if project and self.remote is not None and True not in answers:
            try:
                found = self.remote(project, sha)
            except Exception:                                # noqa: BLE001 - unreachable is not "no"
                found = None
            found = found if found in (True, False) else None
            searched.append("%s remote%s" % (project, {True: " (the server has it)",
                                                       False: " (the server does not have it)",
                                                       None: " (could not be asked)"}[found]))
            answers.append(found)
        if project and not self.repos.get(project) and self.remote is None:
            searched.append("%s (no checkout or resolver configured)" % project)
        if True in answers:
            return True, searched
        if False in answers:
            return False, searched
        return None, searched

    def contains(self, candidate: str, target: str, project: str = ""):
        """Is `candidate` contained in `target`, asked of the project's checkout and then the
        Hub's own? None when neither can answer."""
        cand, tgt = str(candidate or "").lower(), str(target or "").lower()
        if cand and tgt and (tgt.startswith(cand) or cand.startswith(tgt)):
            return True
        places = []
        if project and self.repos.get(str(project).lower()):
            places.append(self.repos[str(project).lower()])
        if self.work_root:
            places.append(self.work_root)
        for path in places:
            verdict = local_contains(path, cand, tgt)
            if verdict is not None:
                return verdict
        return None

    def full(self, sha: str, project: str = "") -> str:
        places = []
        if project and self.repos.get(str(project).lower()):
            places.append(self.repos[str(project).lower()])
        if self.work_root:
            places.append(self.work_root)
        for path in places:
            out = local_full(path, sha)
            if out:
                return out
        return ""
