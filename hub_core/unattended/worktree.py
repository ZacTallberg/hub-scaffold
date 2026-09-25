"""A task runs in a git worktree of its own — never in a person's checkout.

Running an unattended session in a shared checkout is the single most dangerous thing a launcher
can do: that tree may be mid-edit, and ``git add`` there stages the person's uncommitted hunks
along with the session's. So:

* The repository is cloned ONCE per machine into ``<workspace>/_wt/_repos/<repo>`` (a person's
  checkout is only ever READ, for the address to clone from).
* Each task gets ``<workspace>/_wt/responder-<repo>-<n>`` on branch ``responder/<n>``, cut from the
  default branch. A second run on the same task KEEPS the tree the first left, and a branch that
  holds commits never pushed is checked out as it is, never reset.
* After the run the worktree is REMOVED only on proof it holds nothing: the task is done, nothing
  is uncommitted, and every commit on its branch is on the remote. Anything else is kept and
  named. Old trees whose task the BOARD says is done are pruned the same way. Doubt keeps.

Which repository: the task's title leads with ``<slug>:`` (``budget-app: add an export``). The
slug is matched to a checkout under the workspace by its origin's repository name, with hyphen
and underscore treated as one separator (people write both). An app slug that is not the repository
name (``budget`` for ``finance-budget``) is accepted only when EXACTLY ONE checkout carries it as
a whole word — guessing the wrong repository is worse than having no worktree. Then
``HUB_REPO_URL_TEMPLATE`` (``https://git.example.com/team/{slug}.git``), reached with
``git ls-remote`` before it is used.

When no worktree can be made the session falls back to the launcher's directory — and that
fallback is REPORTED to the board as an agent-error, because a hole in the lane that exists only in
one machine's log is a hole nobody knows about.

Untracked per-app agent notes (a ``CLAUDE.md`` that exists only in the person's
checkout, never pushed) are invisible to a worktree cut from a dedicated clone. They are carried
by IMPORT, not copy — a stub ``CLAUDE.md`` containing ``@<absolute path>`` (Claude Code resolves an
``@`` import even to a path outside the worktree; measured on a real session) — so they stay
single-sourced, and the stub is hidden by the clone's ``info/exclude`` (git reads exclude from the
COMMON git dir only; a per-worktree exclude hides nothing) so no session can commit it. A
repository that ships its own notes file is left alone; every failure mode is do nothing.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import board, log
from .runtime import git

ROOT = "_wt"
KEEP_S = 24 * 3600
# Only Claude Code notes: its loader resolves `@` imports. A copy of a notes file with no import
# mechanism would fork it from its source, so none is made.
NOTE_FILES = ("CLAUDE.md",)


def slug_of(title: str) -> str:
    head = str(title or "").split(":", 1)
    if len(head) != 2:
        return ""
    slug = head[0].strip()
    return slug if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,99}", slug) else ""


def _key(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def _repo_name(url: str) -> str:
    name = re.split(r"[/:\\]", str(url or "").rstrip("/\\"))[-1]
    return name[:-4] if name.endswith(".git") else name


def repo_url_for(slug: str, workspace: Path) -> tuple[str, str]:
    """(clone url, how it was found) for ``slug`` on THIS machine, or ("", why not)."""
    try:
        children = [workspace / slug] + sorted(p for p in workspace.iterdir() if p.is_dir())
    except OSError:
        children = [workspace / slug]
    partial = {}
    for child in children[:300]:
        if not (child / ".git").exists():
            continue
        result = git(["-C", child, "config", "--get", "remote.origin.url"], timeout=15)
        url = (result.stdout or "").strip() if result.returncode == 0 else ""
        if not url:
            continue
        name = _repo_name(url)
        if _key(name) == _key(slug):
            return url, "the checkout %s names it" % child.name
        if _key(slug) in re.split(r"[-_.]", _key(name)):
            partial.setdefault(name, url)
    if len(partial) == 1:
        name, url = next(iter(partial.items()))
        return url, "%s is the only checkout here whose name carries %r" % (name, slug)
    template = os.environ.get("HUB_REPO_URL_TEMPLATE", "").strip()
    if template and "{slug}" in template:
        url = template.replace("{slug}", slug)
        if git(["ls-remote", "--heads", url], timeout=60).returncode == 0:
            return url, "HUB_REPO_URL_TEMPLATE, reached with ls-remote"
        return "", "HUB_REPO_URL_TEMPLATE gave %s, which ls-remote could not reach" % url
    if len(partial) > 1:
        return "", "%r matches %d checkouts (%s); refusing to guess" % (
            slug, len(partial), ", ".join(sorted(partial))[:160])
    return "", "no checkout under %s names %r and no HUB_REPO_URL_TEMPLATE is set" % (workspace, slug)


def _redact_url(url: str) -> str:
    """A clone URL as it may appear in a log line or a board fault: any userinfo is dropped, so
    a credential embedded in a remote never propagates."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", str(url or ""), flags=re.I)


def telling_line(text: str, rc) -> str:
    """The line of git's output that names the failure. git ends every SSH failure with the same
    "Please make sure you have the correct access rights / and the repository exists." trailer,
    which reads as an answer and names nothing."""
    lines = [ln.strip() for ln in str(text or "").strip().splitlines() if ln.strip()]
    for ln in lines:
        low = ln.lower()
        if low.startswith(("ssh:", "permission denied", "host key", "fatal: unable", "timed out",
                           "error:")) or "could not resolve" in low:
            return ln[:200]
    generic = ("please make sure", "and the repository exists", "fatal: could not read")
    rest = [ln for ln in lines if not ln.lower().startswith(generic)]
    return (rest or lines or ["rc %s" % rc])[0][:200]


def clone_failure(text: str, rc) -> tuple[str, str]:
    """(kind, what a person should do) for a failed clone. The kinds have OPPOSITE fixes -- a
    refused key is a setting on the code host, an unreachable port is the network, a prompt is
    one interactive connection -- so the report names which one it was instead of guessing."""
    low = str(text or "").lower()
    if "permission denied" in low or "publickey" in low or "authentication failed" in low:
        return "auth", "the code host refused this machine's credential: register its key"
    if ("host key verification failed" in low or "passphrase" in low or "batchmode" in low
            or "terminal prompts disabled" in low or "could not read username" in low):
        return "prompt", ("git needed an interactive answer (a host key, a key passphrase or a "
                          "username): connect once from a terminal on this machine")
    if (rc == 124 or "timed out" in low or "could not resolve" in low
            or "connection refused" in low or "no route to host" in low
            or "network is unreachable" in low or "connection closed" in low):
        return "network", "the code host is not reachable from this machine: not a key problem"
    return "other", "see the git output"


def _usable_clone(repo: Path) -> bool:
    """A .git whose HEAD resolves to a commit. A clone that died (a timeout, a dropped network)
    leaves a .git with no commit; read as a checkout it would be kept forever and every later run
    would fail after it, never cloning again."""
    return git(["-C", repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
               timeout=15).returncode == 0


def _clear_target(repo: Path) -> str:
    """Make room for a clone of the launcher's OWN repository copy. An empty target is removed;
    anything else is ARCHIVED beside it (renamed, never deleted). Returns what it did."""
    if not repo.exists():
        return ""
    if repo.is_dir() and not any(repo.iterdir()):
        repo.rmdir()
        return "removed an empty %s" % repo.name
    dest = repo.with_name("%s.partial-%s" % (repo.name, time.strftime("%Y%m%d-%H%M%S")))
    repo.rename(dest)
    return "archived an unusable %s to %s" % (repo.name, dest.name)


def default_branch(repo: Path) -> str:
    result = git(["-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=15)
    ref = (result.stdout or "").strip() if result.returncode == 0 else ""
    return ref.split("/", 1)[1] if ref.startswith("origin/") else "main"


def _fallback(item_id: str, slug: str, why: str, workspace: str):
    log("worktree: %s" % why)
    board.report_fault(
        "unattended_worktree_unavailable",
        "an unattended run fell back to the launcher's directory: no worktree for %s" % slug,
        "item=%s slug=%s\n%s\nThe session runs in %s. If that is a person's checkout, staging "
        "there takes their uncommitted work with it." % (item_id, slug, why, workspace))
    return workspace, {}


def carry_notes(workspace: Path, name: str, repo: Path, tree: Path) -> list[str]:
    """Import untracked per-app agent notes into the worktree; returns what was carried."""
    carried = []
    try:
        if repo.resolve().parent != (workspace / ROOT / "_repos").resolve():
            return carried                   # only ever touch the launcher's OWN clone
        for note in NOTE_FILES:
            beside = workspace / name / note
            if (tree / note).exists() or not beside.is_file():
                continue                     # the repository ships one, or there is none
            (tree / note).write_text(
                "# %s - carried into this worktree by the unattended launcher\n\n"
                "This worktree is cut from a dedicated clone, so the checkout beside it is not\n"
                "visible from here. The notes are imported rather than copied so they stay\n"
                "single-sourced. This file is not part of the repository and is excluded from git.\n\n"
                "@%s\n" % (name, beside.as_posix()), encoding="utf-8")
            exclude = repo / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            body = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
            if ("/" + note) not in body.splitlines():
                exclude.write_text(body.rstrip("\n") + "\n/" + note + "\n", encoding="utf-8")
            carried.append(str(beside))
    except OSError:
        pass
    return carried


def prepare(item_id: str, title: str, workspace: str, repo_url: str = ""):
    """(the directory the session runs in, what was made). Never raises."""
    slug = slug_of(title) or (_repo_name(repo_url) if repo_url else "")
    root = Path(workspace)
    if not slug:
        log("worktree: %s names no repository in its title; running in %s" % (item_id, workspace))
        return workspace, {}
    number = item_id.rsplit(":", 1)[-1]
    how = "given" if repo_url else ""
    url = repo_url
    if not url and not (root / ROOT / "_repos" / slug / ".git").exists():
        url, how = repo_url_for(slug, root)
        if url and _repo_name(url) != slug:
            log("worktree: %s is the repository behind %r" % (_repo_name(url), slug))
            slug = _repo_name(url)
    repo = root / ROOT / "_repos" / slug
    tree = root / ROOT / ("responder-%s-%s" % (slug, number))
    branch = "responder/%s" % number
    try:
        if (repo / ".git").exists() and not _usable_clone(repo):
            # An interrupted clone: repaired by cloning again, never trusted as a checkout. Only
            # ever the launcher's own copy under _repos, so nobody's work is in it.
            if not url:
                url = (git(["-C", repo, "config", "--get", "remote.origin.url"], timeout=15).stdout
                       or "").strip()
            log("worktree: %s" % _clear_target(repo))
        if not (repo / ".git").exists():
            if not url:
                return _fallback(item_id, slug, how or "no reachable repository", workspace)
            repo.parent.mkdir(parents=True, exist_ok=True)
            _clear_target(repo)
            result = git(["clone", "--no-checkout", url, repo], timeout=1800)
            if result.returncode != 0:
                out = (result.stderr or "") + (result.stdout or "")
                kind, fix = clone_failure(out, result.returncode)
                try:
                    _clear_target(repo)          # a failed clone leaves an empty or partial target
                except OSError:
                    pass
                return _fallback(item_id, slug, "clone of %s failed (%s: %s): %s"
                                 % (_redact_url(url), kind, telling_line(out, result.returncode),
                                    fix), workspace)
        else:
            url = (git(["-C", repo, "config", "--get", "remote.origin.url"], timeout=15).stdout
                   or "").strip()
        fetched = git(["-C", repo, "fetch", "--prune", "origin"], timeout=900).returncode == 0
        base = default_branch(repo)
        kept = (tree / ".git").exists()
        if not kept:
            git(["-C", repo, "worktree", "prune"], timeout=60)
            exists = git(["-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                         timeout=15).returncode == 0
            unpushed = 0
            if exists:
                count = git(["-C", repo, "rev-list", "--count", "origin/%s..%s" % (base, branch)],
                            timeout=30)
                text = (count.stdout or "").strip()
                unpushed = int(text) if count.returncode == 0 and text.isdigit() else 1
            if exists and unpushed:
                result = git(["-C", repo, "worktree", "add", tree, branch], timeout=600)
                kept = True
            else:
                result = git(["-C", repo, "worktree", "add", "-B", branch, tree, "origin/" + base],
                             timeout=600)
            if result.returncode != 0:
                return _fallback(item_id, slug, "worktree for %s failed: %s"
                                 % (item_id, (result.stderr or "").strip()[-300:]), workspace)
            git(["-C", tree, "branch", "--set-upstream-to", "origin/" + base], timeout=30)
        head = (git(["-C", tree, "rev-parse", "--short=12", "HEAD"], timeout=15).stdout or "").strip()
        carried = carry_notes(root, slug, repo, tree)
        made = {"path": str(tree), "repo": str(repo), "slug": slug, "url": url, "how": how,
                "branch": branch, "tracks": "origin/" + base, "base": head, "kept": kept,
                "fetched": fetched, "carried_notes": carried}
        log("worktree: %s runs in %s (%s, %s)" % (item_id, tree, branch,
                                                  "kept" if kept else "fresh"))
        return str(tree), made
    except Exception as exc:  # noqa: BLE001
        return _fallback(item_id, slug, "worktree for %s failed: %s: %s"
                         % (item_id, type(exc).__name__, exc), workspace)


def brief(made: dict) -> str:
    """The worktree's facts for the prompt: data, not charter."""
    if not made:
        return ""
    state = ("KEPT from an earlier run on this task, with whatever that run left in it"
             if made.get("kept") else "fresh from %s%s" % (
                 made["tracks"], "" if made.get("fetched") else " as of the last successful fetch"))
    return ("\nYOUR WORKTREE (made for this run; it is your working directory):\n"
            "path: %s\nrepository: %s (%s)\nbranch: %s, tracking %s, at %s\nstate: %s\n"
            % (made["path"], made["slug"], made.get("url") or "?", made["branch"], made["tracks"],
               made.get("base") or "?", state))


def settle(made: dict, state: str) -> str:
    """Remove the worktree only on proof it holds nothing; otherwise keep it and say why."""
    if not made:
        return ""
    tree, repo = Path(made["path"]), Path(made["repo"])
    try:
        git(["-C", repo, "fetch", "origin"], timeout=600)
        status = git(["-C", tree, "status", "--porcelain"], timeout=60)
        dirty = (len([ln for ln in (status.stdout or "").splitlines() if ln.strip()])
                 if status.returncode == 0 else -1)
        count = git(["-C", tree, "rev-list", "--count", "%s..HEAD" % made["tracks"]], timeout=30)
        text = (count.stdout or "").strip()
        ahead = int(text) if count.returncode == 0 and text.isdigit() else -1
        if state == "cleared" and dirty == 0 and ahead == 0:
            if git(["-C", repo, "worktree", "remove", tree], timeout=300).returncode == 0:
                git(["-C", repo, "branch", "-d", made["branch"]], timeout=30)
                return "removed its worktree %s" % tree
        why = []
        if dirty:
            why.append("%s uncommitted" % ("unknown" if dirty < 0 else dirty))
        if ahead:
            why.append("%s commit(s) not on %s" % ("unknown" if ahead < 0 else ahead, made["tracks"]))
        if state != "cleared":
            why.append("task not done")
        return "kept its worktree %s (%s)" % (tree, ", ".join(why) or "removal refused")
    except Exception as exc:  # noqa: BLE001
        return "kept its worktree %s (%s)" % (tree, type(exc).__name__)


def prune(workspace: str, project_key_prefix: str, skip: str = "") -> list[str]:
    """Old responder worktrees whose task the BOARD says is done, holding nothing, are removed."""
    done = []
    base = Path(workspace) / ROOT
    try:
        trees = sorted(p for p in base.glob("responder-*") if p.is_dir() and str(p) != skip)
    except OSError:
        return done
    for tree in trees[:20]:
        try:
            if time.time() - tree.stat().st_mtime < KEEP_S or not (tree / ".git").is_file():
                continue
            name, _, number = tree.name[len("responder-"):].rpartition("-")
            if not name or not number.isdigit():
                continue
            if board.task_status("%s:task:%s" % (project_key_prefix, number)) != "done":
                continue
            repo = base / "_repos" / name
            made = {"path": str(tree), "repo": str(repo), "branch": "responder/%s" % number,
                    "tracks": "origin/" + default_branch(repo)}
            done.append("%s: %s" % (tree.name, settle(made, "cleared")))
        except Exception:  # noqa: BLE001
            continue
    return done
