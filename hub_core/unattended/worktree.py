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

NO CREDENTIAL EVER TRAVELS IN A URL. A person's checkout can carry one in its origin
(``https://user:<token>@git.example.com/...``); copying that origin into every clone the launcher
makes spreads the token into each of their configs, into the prompt's worktree brief and into the
board's fault rows. Every URL this module reads or builds -- a checkout's origin, the template, the
clone's own remote -- passes through ``no_userinfo`` first, and every reason string that could echo
one through ``redact``. Git gets credentials from its helper, never from a remote URL, so nothing
that works is lost by stripping. An ssh URL keeps its user (``ssh://git@host/...`` names the
account the key logs in as) and loses only a password; an scp-style SSH remote (``git@host:path``)
has no scheme and is left exactly as it is.

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


_USERINFO = re.compile(r"^([a-z][a-z0-9+.\-]*)://([^/@\s]*)@", re.I)
_USERINFO_ANYWHERE = re.compile(r"\b([a-z][a-z0-9+.\-]*)://([^/@\s]*)@", re.I)


def _strip(match) -> str:
    """An ssh transport keeps its USER (``ssh://git@host`` names the account the key logs in as)
    and loses only a ``:password``; every other scheme loses the whole userinfo, because a token
    is as often the user part (``https://<token>@``) as the password part."""
    scheme, userinfo = match.group(1), match.group(2)
    if scheme.lower() in ("ssh", "git+ssh", "ssh+git"):
        user = userinfo.split(":", 1)[0]
        return "%s://%s@" % (scheme, user) if user else "%s://" % scheme
    return "%s://" % scheme


def no_userinfo(url: str) -> str:
    """The URL with any credential removed; an scp-style remote is returned unchanged."""
    return _USERINFO.sub(_strip, str(url or "").strip())


def redact(text: str) -> str:
    """Any scheme URL inside free text (git's stderr, a reason) with its credential removed."""
    return _USERINFO_ANYWHERE.sub(_strip, str(text or ""))


def project_of(url: str) -> str:
    """The repository's name from any remote form, lowercased -- the task's ``project`` slug."""
    name = _repo_name(no_userinfo(url)).strip().lower()
    return name if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", name) else ""


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
        url = no_userinfo((result.stdout or "").strip() if result.returncode == 0 else "")
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
        url = no_userinfo(template.replace("{slug}", slug))
        if git(["ls-remote", "--heads", url], timeout=60).returncode == 0:
            return url, "HUB_REPO_URL_TEMPLATE, reached with ls-remote"
        return "", "HUB_REPO_URL_TEMPLATE gave %s, which ls-remote could not reach" % url
    if len(partial) > 1:
        return "", "%r matches %d checkouts (%s); refusing to guess" % (
            slug, len(partial), ", ".join(sorted(partial))[:160])
    return "", "no checkout under %s names %r and no HUB_REPO_URL_TEMPLATE is set" % (workspace, slug)


def default_branch(repo: Path) -> str:
    result = git(["-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=15)
    ref = (result.stdout or "").strip() if result.returncode == 0 else ""
    return ref.split("/", 1)[1] if ref.startswith("origin/") else "main"


def _fallback(item_id: str, slug: str, why: str, workspace: str):
    why = redact(why)
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
    repo_url = no_userinfo(repo_url)
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
        if not (repo / ".git").exists():
            if not url:
                return _fallback(item_id, slug, how or "no reachable repository", workspace)
            repo.parent.mkdir(parents=True, exist_ok=True)
            result = git(["clone", "--no-checkout", url, repo], timeout=1800)
            if result.returncode != 0:
                return _fallback(item_id, slug, "clone of %s failed: %s"
                                 % (url, redact((result.stderr or "").strip())[-300:]), workspace)
        else:
            raw = (git(["-C", repo, "config", "--get", "remote.origin.url"], timeout=15).stdout
                   or "").strip()
            url = no_userinfo(raw)
            if raw and raw != url:
                # A clone made before this rule carries the credential in its own config. It is
                # the launcher's scratch clone (never a person's checkout): heal it in place.
                if git(["-C", repo, "remote", "set-url", "origin", url], timeout=15).returncode == 0:
                    log("worktree: removed a credential from the origin URL of %s" % repo)
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
                                 % (item_id, redact((result.stderr or "").strip())[-300:]),
                                 workspace)
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
                         % (item_id, type(exc).__name__, redact(str(exc))), workspace)


def brief(made: dict) -> str:
    """The worktree's facts for the prompt: data, not charter."""
    if not made:
        return ""
    state = ("KEPT from an earlier run on this task, with whatever that run left in it"
             if made.get("kept") else "fresh from %s%s" % (
                 made["tracks"], "" if made.get("fetched") else " as of the last successful fetch"))
    return ("\nYOUR WORKTREE (made for this run; it is your working directory):\n"
            "path: %s\nrepository: %s (%s)\nbranch: %s, tracking %s, at %s\nstate: %s\n"
            % (made["path"], made["slug"], no_userinfo(made.get("url") or "") or "?", made["branch"],
               made["tracks"],
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
