"""The publish hand-off, publisher side: a machine that CAN push ships what one that cannot left.

A task is done only when its commit is on the protected branch. An unattended run whose machine
cannot push (a key nobody registered, a credential manager that cannot prompt with nobody there)
used to stop at a patch file on its own disk, and the work was redone by hand elsewhere. The
charter now tells that run to hand its commits to the hub as a git bundle
(``python -m hub_core.client handoff <task>``, served by ``POST /hub/api/handoff``). THIS module is
the other half, and it is the one thing the launcher DOES itself rather than delegating to a model
session, so it is deliberately mechanical and narrow:

* It acts only when this machine has PROVED it can push to the host: ``git push --dry-run`` of a
  throwaway commit to a ref that never exists. A dry run authenticates to the receive side (a
  missing key fails here) and sends no objects. The verdict is cached per host for six hours
  either way. A PROBE NEVER SENDS A PASSWORD: a directory-backed git host counts an expired
  stored password toward the account's lockout, and this runs on every armed machine. So SSH is
  keys only (``BatchMode``, no password or keyboard-interactive auth), the credential helper and
  askpass are emptied, and an HTTPS transport is reported "not proven" -- it fails closed.
* The URL is BUILT here from the record's project PATH and a transport this machine already uses
  (a checkout whose origin is on an allowed host, or ``HUB_PUBLISH_URL_TEMPLATE``), never taken
  from the hand-off, and only for a host in ``HUB_PUBLISH_HOSTS``. A record can therefore never
  steer a publisher at some other host; with no allowed host the publisher does nothing at all.
  No URL it builds or reports carries a credential.
* It leases ONE hand-off (``POST /hub/api/handoff/claim``: a random token and an increasing
  fence), downloads the bundle under that lease and checks its sha256, and in a scratch clone
  under ``<home>/publish`` -- never a person's checkout -- verifies the bundle, fetches its head,
  rebases onto the current branch and pushes ``HEAD:<branch>`` WITHOUT force. When the branch
  moved under it, it fetches and rebases again (three rounds). It reads the branch back before
  reporting the pushed sha (``POST /hub/api/handoff/result``); the hub then writes a ``pushed``
  checkpoint on the task, which is what lets the deploy record close it.
* A conflict reports ``failed`` with the conflicting paths and leaves everything alone: it never
  resolves a conflict and never forces. An auth refusal RELEASES the lease to another publisher
  and caches "cannot push" for this host. Every failure reaches the board.
* One publish per machine (an ``O_EXCL`` lock holding the pid). From the scheduled tick the pass
  runs as its own process under a hard ceiling, and its process tree is reaped and proven gone.
  The scratch directory is removed by its literal path, checked to sit directly under
  ``<home>/publish``.

Environment: ``HUB_PUBLISH_HOSTS`` (comma-separated git hosts this machine may push to; empty =
the publisher is off), ``HUB_PUBLISH_URL_TEMPLATE`` (``git@git.example.com:{project}.git``),
``HUB_PUBLISH_BRANCH`` (default ``main``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import client
from ..process_lock import _pid_alive
from . import board, home, log, machine, read_json, write_json
from .runtime import NO_WINDOW, git, reap, worker_env
from .worktree import no_userinfo, redact

PUBLISH_MAX_RUN_S = 600          # the whole publish pass, hard (process tree killed)
PUBLISH_LEASE_S = 900            # > PUBLISH_MAX_RUN_S: the lease outlives the pass
PUSH_VERDICT_OK_S = 6 * 3600     # re-prove push rights every six hours
PUSH_VERDICT_NO_S = 6 * 3600     # an auth refusal is never retried sooner (account lockout)
AUTH_REFUSAL = re.compile(r"(?i)permission denied|publickey|authentication failed|"
                          r"could not read username|terminal prompts disabled|"
                          r"not allowed to push|http (?:basic: )?access denied|"
                          r"\b40[13]\b|could not read from remote repository")
MOVED = re.compile(r"(?i)non-fast-forward|fetch first|rejected.*\(stale|failed to update ref")
_SCP = re.compile(r"^(?P<user>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<path>.+?)(?:\.git)?/?$")
_SCHEME = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.\-]*)://(?:[^@/\s]*@)?(?P<host>[^/:\s]+)"
                     r"(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$", re.I)


def _root() -> Path:
    return home() / "publish"


def _lock_path() -> Path:
    return home() / "publisher.lock"


def _state_path() -> Path:
    return home() / "publisher-state.json"


def hosts() -> list[str]:
    return [h.strip().lower() for h in (os.environ.get("HUB_PUBLISH_HOSTS") or "").split(",")
            if h.strip()]


def branch() -> str:
    value = (os.environ.get("HUB_PUBLISH_BRANCH") or "main").strip()
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", value) else "main"


def parse_remote(url: str) -> tuple[str, str, str]:
    """(transport prefix, host, project path) of a remote, or ("", "", "")."""
    url = no_userinfo(url)
    m = _SCP.match(url)
    if m:
        return "%s@%s:" % (m.group("user"), m.group("host")), m.group("host").lower(), \
            m.group("path").strip("/")
    m = _SCHEME.match(url)
    if m:
        prefix = url[:m.start("path")]
        return prefix, m.group("host").lower(), m.group("path").strip("/")
    return "", "", ""


def _is_ssh(url: str) -> bool:
    url = no_userinfo(url)
    return bool(_SCP.match(url)) or url.lower().startswith("ssh://")


# ---------------------------------------------------------------- push-ability, proved

def _verdicts() -> dict:
    return read_json(_state_path(), {}).get("hosts") or {}


def _save_verdict(host: str, ok: bool, why: str) -> None:
    state = read_json(_state_path(), {})
    state.setdefault("hosts", {})[host] = {"ok": bool(ok), "at": time.time(), "why": redact(why)[:300]}
    write_json(_state_path(), state)


def cached_verdict(host: str):
    """True/False while the cached verdict is fresh, None when it must be proved again."""
    row = _verdicts().get(host) or {}
    if not row:
        return None
    age = time.time() - float(row.get("at") or 0)
    if row.get("ok") and age < PUSH_VERDICT_OK_S:
        return True
    if not row.get("ok") and age < PUSH_VERDICT_NO_S:
        return False
    return None


def push_state() -> str:
    """This machine's freshest push verdict over the allowed hosts: ok | no | unknown. Carried on
    every run record, so a run that could not push is visible as such afterwards."""
    rows = [row for host, row in _verdicts().items() if host in hosts()]
    if not rows:
        return "unknown"
    row = max(rows, key=lambda r: float(r.get("at") or 0))
    return "ok" if row.get("ok") else "no"


def _rmtree_literal(path: Path) -> None:
    """Remove ONE scratch directory this publisher made -- checked to sit directly under the
    publish root, never a glob and never anything above it. Git marks pack files read-only on
    Windows; clear the bit and retry."""
    path = Path(path)
    try:
        if not path.exists() or path.resolve().parent != _root().resolve():
            return
    except OSError:
        return

    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    shutil.rmtree(str(path), onerror=_onerror)


def probe_push(url: str) -> tuple[bool, str]:
    """Can this machine PUSH to ``url``'s host? Proved with a dry run, keys only, never a
    password; an HTTPS transport is "not proven" and fails closed."""
    if not _is_ssh(url):
        return False, ("not proven: an HTTPS push probe would send a stored password, which a "
                       "probe never does; this machine needs an SSH key for the host")
    probe = _root() / ("probe-%d" % os.getpid())
    ref = "refs/heads/push-probe-%s-%s" % (
        re.sub(r"[^a-z0-9-]", "-", machine())[:30],
        hashlib.sha1(("%s|%s" % (os.getpid(), time.time())).encode("utf-8")).hexdigest()[:12])
    try:
        probe.mkdir(parents=True, exist_ok=True)
        steps = (["init", "-q", str(probe)],
                 ["-C", str(probe), "-c", "user.name=push-probe",
                  "-c", "user.email=push-probe@invalid", "commit", "-q", "--allow-empty",
                  "-m", "push probe"],
                 ["-c", "credential.helper=", "-c", "core.askPass=",
                  "-c", "core.sshCommand=ssh -o BatchMode=yes -o PasswordAuthentication=no "
                        "-o KbdInteractiveAuthentication=no -o ConnectTimeout=15",
                  "-C", str(probe), "push", "--dry-run", "--porcelain", url, "HEAD:" + ref])
        for args in steps:
            result = git(args, timeout=90)
            if result.returncode != 0:
                lines = ((result.stderr or "") + (result.stdout or "")).strip().splitlines()
                return False, redact(lines[-1] if lines else "exit %s" % result.returncode)[:300]
        return True, "push --dry-run authenticated"
    finally:
        _rmtree_literal(probe)


def project_url(project: str, workspace: str) -> str:
    """Where THIS machine reaches ``project``, in a transport it already uses, on an allowed
    host -- or ''. A checkout whose origin IS the project wins; otherwise any allowed-host
    checkout's transport is re-pointed at the path, then the template. Reached with ls-remote."""
    want = project.strip("/").lower()
    allowed = hosts()
    if not want or not allowed:
        return ""
    prefixes: list[str] = []
    try:
        children = sorted(p for p in Path(workspace).iterdir() if p.is_dir())
    except OSError:
        children = []
    for child in children[:300]:
        if not (child / ".git").exists():
            continue
        result = git(["-C", child, "config", "--get", "remote.origin.url"], timeout=15)
        url = no_userinfo((result.stdout or "").strip() if result.returncode == 0 else "")
        prefix, host, path = parse_remote(url)
        if host not in allowed:
            continue
        if path.lower() == want:
            return url
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    candidates = [prefix + project.strip("/") + ".git" for prefix in prefixes]
    template = (os.environ.get("HUB_PUBLISH_URL_TEMPLATE") or "").strip()
    if template and "{project}" in template:
        candidates.append(no_userinfo(template.replace("{project}", project.strip("/"))))
    for url in candidates:
        if parse_remote(url)[1] not in allowed:
            continue
        if git(["ls-remote", "--heads", url], timeout=60).returncode == 0:
            return url
    return ""


def can_push(project: str, workspace: str) -> tuple[bool, str, str]:
    """(ok, url, why): the cached per-host verdict, proved again when stale."""
    url = project_url(project, workspace)
    if not url:
        return False, "", ("no transport on an allowed host (HUB_PUBLISH_HOSTS) reaches %s from "
                           "this machine" % project)
    host = parse_remote(url)[1]
    cached = cached_verdict(host)
    if cached is not None:
        return cached, url, "cached verdict for %s" % host
    ok, why = probe_push(url)
    _save_verdict(host, ok, why)
    log("publisher: push probe to %s: %s (%s)" % (host, "CAN push" if ok else "cannot push", why))
    return ok, url, why


# ---------------------------------------------------------------- the hub's hand-off lane

def _listing() -> list[dict]:
    try:
        rows = board._read("handoffs.json").get("data") or []
    except RuntimeError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def waiting() -> list[dict]:
    """Open hand-offs nobody holds a live lease on, oldest first."""
    return [r for r in _listing() if r.get("status") == "pending"
            and not (r.get("claim") or {}).get("expires_in_s")]


def _bundle(rec: dict) -> tuple[int, bytes | str]:
    """Download the bundle under the lease: (status, bytes | error text). Binary, so it goes
    straight through urllib with the client's own headers rather than the JSON transport."""
    body = json.dumps({"id": rec["id"], "token": rec["token"],
                       "fence": rec["fence"]}).encode("utf-8")
    headers = {"Content-Type": "application/json", **client._common_headers(),
               **client._auth_headers(), "X-Hub-Machine": machine()}
    last: tuple[int, bytes | str] = (0, "hub unreachable")
    for base in client._base_urls(None):
        try:
            request = urllib.request.Request(base.rstrip("/") + "/api/handoff/bundle", data=body,
                                             method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()[:500].decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = (0, "%s: %s" % (type(exc).__name__, str(exc)[:200]))
    return last


def _result(rec: dict, outcome: str, **fields) -> tuple[int, dict]:
    """Report once; a result is idempotent per fence, so a resend after a blip is safe."""
    body = {"id": rec["id"], "token": rec["token"], "fence": rec["fence"], "outcome": outcome,
            "machine": machine(), **fields}
    if "reason" in body:
        body["reason"] = redact(body["reason"])[:1000]
    status, out = 0, {}
    for _attempt in range(3):
        status, out = board._write("handoff/result", body)
        if status:
            break
        time.sleep(2)
    log("publisher: %s %s -> hub %s %s" % (rec["id"], outcome, status,
                                           json.dumps(out, default=str)[:300]))
    return status, out


def publish_one(rec: dict, url: str, deadline: float) -> str:
    """Replay one leased hand-off onto the branch. Returns the outcome it reported."""
    work = _root() / ("%s-%d" % (rec["id"], os.getpid()))
    target = branch()

    def left() -> int:
        return max(10, int(deadline - time.time()))

    def g(*args, timeout=None):
        return git(["-C", str(work / "repo")] + list(args), timeout=min(timeout or 300, left()))

    def tail(result) -> str:
        lines = ((result.stderr or "") + "\n" + (result.stdout or "")).strip().splitlines()
        return redact(" | ".join(lines[-3:]))[:600]

    try:
        work.mkdir(parents=True, exist_ok=True)
        status, data = _bundle(rec)
        if status != 200 or not isinstance(data, bytes):
            why = "could not download the bundle: %s %s" % (status, str(data)[:200])
            if status == 409 and "missing" in str(data):
                _result(rec, "failed", reason=why)          # releasing would loop forever
                return "failed"
            _result(rec, "released", reason=why)
            return "released"
        if rec.get("sha256") and hashlib.sha256(data).hexdigest() != rec["sha256"]:
            _result(rec, "failed", reason="the downloaded bundle does not match its recorded "
                                          "sha256; refusing to publish it")
            return "failed"
        bundle = work / "handoff.bundle"
        bundle.write_bytes(data)
        result = git(["clone", "-q", "--no-tags", "--single-branch", "--branch", target,
                      "--filter=blob:none", url, str(work / "repo")], timeout=min(300, left()))
        if result.returncode != 0:
            _result(rec, "released", reason="clone failed here: " + tail(result))
            return "released"                               # not this hand-off's fault
        result = g("bundle", "verify", str(bundle))
        if result.returncode != 0:
            _result(rec, "failed", reason="git bundle verify failed against the current %s: %s"
                    % (target, tail(result)))
            return "failed"
        result = g("fetch", "-q", str(bundle), "HEAD:refs/heads/handoff")
        head = (g("rev-parse", "refs/heads/handoff").stdout or "").strip()
        if result.returncode != 0 or head != rec.get("head"):
            _result(rec, "failed", reason="the bundle's head is %s, the record says %s (%s)"
                    % (head[:12], str(rec.get("head"))[:12], tail(result)))
            return "failed"
        identity = []
        if not (git(["config", "--global", "user.email"], timeout=15).stdout or "").strip():
            identity = ["-c", "user.name=hub publisher (%s)" % machine(),
                        "-c", "user.email=publisher@%s.invalid" % re.sub(r"[^a-z0-9-]", "-",
                                                                         machine())]
        result = g("checkout", "-q", "handoff")
        if result.returncode != 0:
            _result(rec, "failed", reason="checkout of the handed-off head failed: " + tail(result))
            return "failed"
        pushed, base_sha = "", ""
        for attempt in range(3):
            result = g(*(identity + ["rebase", "origin/" + target]))
            if result.returncode != 0:
                conflicts = [p for p in (g("diff", "--name-only", "--diff-filter=U").stdout
                                         or "").splitlines() if p.strip()]
                g("rebase", "--abort")
                _result(rec, "failed", conflicts=conflicts,
                        reason=("the commits do not rebase cleanly onto the current " + target
                                if conflicts else "rebase onto the current %s failed: %s"
                                % (target, tail(result))))
                return "failed"
            new_head = (g("rev-parse", "HEAD").stdout or "").strip()
            base_sha = (g("rev-parse", "origin/" + target).stdout or "").strip()
            if new_head == base_sha:
                # Every commit is already on the branch (a double publish after a lapsed lease,
                # or the author's work landed another way). Nothing to push; say so.
                _result(rec, "published", pushed_sha=new_head,
                        note="every handed-off commit was already on %s; nothing pushed" % target)
                return "published"
            # NEVER FORCED: a plain push is a fast-forward or it is refused.
            result = g("push", "--porcelain", "origin", "HEAD:refs/heads/" + target)
            if result.returncode == 0:
                pushed = new_head
                break
            text = (result.stderr or "") + (result.stdout or "")
            if AUTH_REFUSAL.search(text):
                _save_verdict(parse_remote(url)[1], False, tail(result))
                _result(rec, "released", reason="this machine's push was refused for auth: "
                        + tail(result))
                return "released"
            if MOVED.search(text) and attempt < 2:
                g("fetch", "-q", "origin", "%s:refs/remotes/origin/%s" % (target, target))
                continue                                    # the branch moved: rebase again
            _result(rec, "failed", reason="push to %s was refused: %s" % (target, tail(result)))
            return "failed"
        if not pushed:
            _result(rec, "failed", reason="%s kept moving; three rebase-and-push rounds lost"
                    % target)
            return "failed"
        # READ BACK what the remote now has before calling it published.
        g("fetch", "-q", "origin", "%s:refs/remotes/origin/%s" % (target, target))
        if g("merge-base", "--is-ancestor", pushed, "origin/" + target).returncode != 0:
            _result(rec, "failed", reason="the push reported success but %s is not on %s when "
                    "read back" % (pushed[:12], target))
            return "failed"
        subject = (g("log", "-1", "--format=%s", pushed).stdout or "").strip()
        _result(rec, "published", pushed_sha=pushed,
                note="rebased onto %s %s; %s" % (target, base_sha[:12], subject[:160]))
        return "published"
    finally:
        _rmtree_literal(work)


def _acquire_lock() -> bool:
    path = _lock_path()
    try:
        holder = int((path.read_text(encoding="ascii").strip() or "0").split()[0])
    except (OSError, ValueError):
        holder = 0
    if path.exists() and not _pid_alive(holder):
        try:
            path.unlink()                                   # its publisher is gone
        except OSError:
            return False
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(str(os.getpid()))
    return True


def publish(workspace: str) -> dict:
    """The publish pass: at most ONE hand-off, then exit."""
    if not hosts():
        return {"outcome": "off", "why": "HUB_PUBLISH_HOSTS names no host this machine may push to"}
    if (home() / "DISABLED").exists():
        return {"outcome": "off", "why": "kill switch: %s exists" % (home() / "DISABLED")}
    if not _acquire_lock():
        return {"outcome": "busy", "why": "another publish is running on this machine"}
    deadline = time.time() + PUBLISH_MAX_RUN_S - 30
    try:
        _root().mkdir(parents=True, exist_ok=True)
        rows = waiting()
        if not rows:
            return {"outcome": "idle", "why": "no hand-off is waiting"}
        project = str(rows[0].get("project") or "")
        ok, url, why = can_push(project, workspace)
        if not ok:
            log("publisher: %d hand-off(s) waiting but this machine cannot push (%s)"
                % (len(rows), why))
            return {"outcome": "cannot-push", "waiting": len(rows), "why": why}
        status, out = board._write("handoff/claim", {"machine": machine(),
                                                     "ttl_s": PUBLISH_LEASE_S})
        rec = (out or {}).get("data") if status == 200 else None
        if not rec:
            return {"outcome": "nothing-claimed", "status": status}
        if rec.get("project") != project:
            # The claim hands out the OLDEST; transport is per project, so resolve it again.
            ok, url, why = can_push(str(rec.get("project") or ""), workspace)
            if not ok:
                _result(rec, "released", reason="this machine cannot reach %s: %s"
                        % (rec.get("project"), why))
                return {"outcome": "released", "handoff": rec.get("id"), "why": why}
        log("publisher: claimed %s (%s, %s -> %s, fence %s) for %s"
            % (rec["id"], rec.get("project"), str(rec.get("base"))[:12],
               str(rec.get("head"))[:12], rec.get("fence"), rec.get("task")))
        try:
            outcome = publish_one(rec, url, deadline)
        except Exception as exc:  # noqa: BLE001 - the publisher's own crash is reported
            outcome = "error"
            _result(rec, "released", reason="publisher crashed: %s: %s"
                    % (type(exc).__name__, str(exc)[:200]))
            board.report_fault("handoff_publisher_crashed",
                               "the hand-off publisher crashed on %s" % rec["id"],
                               "%s: %s" % (type(exc).__name__, redact(str(exc))))
        if outcome == "failed":
            board.report_fault("handoff_publish_failed",
                               "hand-off %s for %s could not be published" % (rec["id"],
                                                                             rec.get("task")),
                               "GET /hub/handoffs.json?status=failed names the reason; the "
                               "author is told by the hub", severity="warning")
        return {"outcome": outcome, "handoff": rec["id"], "task": rec.get("task")}
    finally:
        try:
            _lock_path().unlink()
        except OSError:
            pass


def tick(workspace: str) -> dict:
    """From the scheduled scan: one cheap read, and only when something is waiting the publish
    pass as its OWN process -- bounded, and its whole tree proven dead afterwards. Not paced like
    a model session: finished work waiting on a push is the most expensive thing on the tick."""
    if not hosts():
        return {"publisher": "off"}
    rows = waiting()
    if not rows:
        return {"publisher": "idle"}
    try:
        holder = int((_lock_path().read_text(encoding="ascii").strip() or "0").split()[0])
    except (OSError, ValueError):
        holder = 0
    if holder and _pid_alive(holder):
        return {"publisher": "busy", "waiting": len(rows)}
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("pythonw.exe"):
        python = python[:-len("pythonw.exe")] + "python.exe"
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW) if os.name == "nt" else 0
    proc = subprocess.Popen([python, "-m", "hub_core.unattended", "publish", "--workspace",
                             workspace], cwd=str(Path(__file__).resolve().parents[2]),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=worker_env(), creationflags=flags,
                            start_new_session=(os.name != "nt"))
    try:
        proc.wait(timeout=PUBLISH_MAX_RUN_S)
    except subprocess.TimeoutExpired:
        log("publisher: the publish pass hit its %ds ceiling; killing its process tree"
            % PUBLISH_MAX_RUN_S)
        board.report_fault("handoff_publisher_timed_out",
                           "the hand-off publisher was killed at its %ds ceiling" % PUBLISH_MAX_RUN_S,
                           "%d hand-off(s) waiting; the lease lapses and another publisher takes it"
                           % len(rows), severity="warning")
    state = reap(proc)
    if state != "reaped":
        board.report_fault("handoff_publisher_survived",
                           "the hand-off publisher's process tree SURVIVED its reap",
                           "pid %d" % proc.pid)
    return {"publisher": "ran", "waiting": len(rows), "rc": proc.returncode, "reaped": state}
