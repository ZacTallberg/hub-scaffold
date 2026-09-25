"""Which agent runtime runs a pass, how it is started, and the bounded wait around it.

Two runtimes: Claude Code and Codex. ``HUB_WORKER_RUNTIME`` is ``claude``, ``codex`` or ``auto``
(the default: Claude Code where it is installed, else Codex). Both run under the same lanes, clock,
outcome checks and fault classification; only the command line and the output shape differ.

* Claude Code: ``claude -p <prompt> --permission-mode auto --allowedTools <tools> --output-format
  json``. Never a permission bypass: an unattended session must not be able to skip every check,
  and in print mode a tool it cannot auto-allow is DENIED rather than prompting a person who is not
  there.
* Codex: ``codex exec --json ... <prompt>``. Its approval flags vary by version and install, so
  they are configurable (``HUB_WORKER_CODEX_ARGS``); check ``codex exec --help`` on the machine.
  Codex owns its own hook trust, auth, model and MCP configuration — the launcher never edits them.

The environment a pass inherits is the launcher's, with three deliberate changes:

* the PARENT's runtime identity is removed (``CLAUDE_CODE_SESSION_ID``, ``CODEX_THREAD_ID``,
  ``CODEX_SESSION_ID``): a newly launched runtime owns its identity and must never report under
  the task of the process that spawned it;
* it is NON-INTERACTIVE: git never prompts on a terminal or raises a credential dialog, ssh runs in
  batch mode. A prompt nobody is watching is not a pause, it is a hang to the ceiling;
* the run is attributable: ``HUB_UNATTENDED=1`` plus the run id, item and escalation hop.

stdin is always the null device: a runtime reading piped input from a console nobody types into
blocks before its first model call and is killed at the ceiling with zero tokens.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from ..process_lock import _pid_alive
from . import forensics, log

NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW, for leaf tools
BEAT_S = 60
SUSPEND_BEAT_OVERRUN_S = 30
PARENT_IDENTITY = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")

# The tools an unattended Claude Code session may use without asking. Wide enough to actually
# ship a fix (edit, shell); network fetch is left out because shipping work rarely needs egress.
CLAUDE_TOOLS = ["Read", "Grep", "Glob", "TodoWrite", "Write", "Edit", "NotebookEdit",
                "Bash", "PowerShell"]
CODEX_DEFAULT_ARGS = "exec --json --skip-git-repo-check"


def claude_executable() -> str:
    explicit = os.environ.get("HUB_WORKER_CLAUDE", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    local = Path(os.path.expanduser("~")) / ".local" / "bin" / (
        "claude.exe" if os.name == "nt" else "claude")
    if local.is_file():
        return str(local)
    return shutil.which("claude") or ""


def codex_executable() -> str:
    explicit = os.environ.get("HUB_WORKER_CODEX", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path(os.path.expanduser("~")) / "AppData/Local")
        candidates = list((root / "OpenAI" / "Codex" / "bin").glob("**/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return ""


def selected() -> str:
    value = (os.environ.get("HUB_WORKER_RUNTIME") or "auto").strip().lower()
    if value == "auto":
        return "claude" if claude_executable() else "codex"
    if value not in ("claude", "codex"):
        raise ValueError("HUB_WORKER_RUNTIME must be auto, claude or codex")
    return value


def executable(runtime: str | None = None) -> str:
    return codex_executable() if (runtime or selected()) == "codex" else claude_executable()


def command(prompt: str, runtime: str | None = None) -> list[str]:
    runtime = runtime or selected()
    exe = executable(runtime)
    if runtime == "codex":
        extra = shlex.split(os.environ.get("HUB_WORKER_CODEX_ARGS") or CODEX_DEFAULT_ARGS,
                            posix=os.name != "nt")
        return [exe, *extra, prompt]
    return [exe, "-p", prompt, "--permission-mode", "auto", "--allowedTools", *CLAUDE_TOOLS,
            "--output-format", "json"]


def worker_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    for key in PARENT_IDENTITY:
        env.pop(key, None)
    env["HUB_UNATTENDED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    # ssh must never wait on a person: BatchMode turns a host-key or passphrase prompt into an
    # immediate, named failure, and ConnectTimeout bounds a dead path (a clone that "timed out
    # after 900 s" was an unanswered prompt on the origin system, reported as a bad key).
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20 "
                                      "-o ServerAliveInterval=15 -o ServerAliveCountMax=8")
    for key, value in (extra or {}).items():
        env[key] = str(value)
    return env


def git(args, *, timeout=120, cwd=None):
    """git with the worker's non-interactive environment, no window, no stdin. On Windows it
    selects the OS certificate store (``http.sslBackend=schannel``): a fresh clone inherits no
    per-repository config, and git's bundled trust store does not carry an enterprise root CA the
    OS does. Selecting the OS store VERIFIES the chain; it never disables verification. Never
    raises: a failure is a non-zero returncode with the reason in stderr."""
    trust = ["-c", "http.sslBackend=schannel"] if os.name == "nt" else []
    try:
        return subprocess.run(["git", *trust, *[str(a) for a in args]], cwd=cwd,
                              capture_output=True, text=True, errors="replace", timeout=timeout,
                              env=worker_env(), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(args, 1, "", "%s: %s" % (type(exc).__name__, exc))


def _kill_tree(proc) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           timeout=30, creationflags=NO_WINDOW)
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        os.killpg(proc.pid, 9)            # the session leader's whole group
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def reap(proc) -> str:
    """Guarantee the pass is GONE, on the normal path too. A returned parent is not a dead tree:
    tool children can outlive it and keep acting with this machine's rights. Sweep the group, then
    PROVE the pid is not running. ``SURVIVED`` is loud on purpose."""
    try:
        _kill_tree(proc)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(10):
        if proc.poll() is not None and not _pid_alive(proc.pid):
            return "reaped"
        time.sleep(0.5)
    return "SURVIVED"


class _WakeLock:
    """Hold the machine awake for the length of a run (Windows). A machine that sleeps under a
    run advances every wall-clock deadline across the gap and fires them all on wake: the pass is
    killed having run a fraction of its bound, and every timeout it hit looks like a real fault."""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(
                    self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            except Exception:  # noqa: BLE001
                pass
        return self

    def __exit__(self, *_exc):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except Exception:  # noqa: BLE001
                pass


def run_bounded(argv, *, workspace: str, bound_s: int, env: dict, on_beat=None,
                watch_transcript: bool = True):
    """Run one pass, bounded. Returns ``(rc, stdout, timing)``; rc is None when killed.

    The wait is in heartbeats: every ``BEAT_S`` the caller's ``on_beat`` runs (the lane lock and
    the task lease are renewed there) and the ceiling is still the ceiling. Each heartbeat asks
    for a bounded wait, so one that returns far later than it asked was not waiting — the machine
    was asleep — and that overrun is summed as ``suspended_s``: a measurement, not a guess.

    ``timing``: ``session_s`` (start to exit or kill), ``suspended_s``, ``kill_s`` (kill to
    reaped), ``reaped`` (the reaper's own verdict) and, when the session finished but would not
    exit, ``finished_hung_s``."""
    startupinfo, flags = None, 0
    if os.name == "nt":
        # NOT CREATE_NO_WINDOW: that leaves the runtime with no console, so every shell tool it
        # runs allocates a fresh VISIBLE one on the owner's desktop. A new console, hidden, is
        # inherited by every descendant; a new process group keeps the tree killable.
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    timing = {"session_s": 0, "suspended_s": 0, "kill_s": 0, "reaped": ""}
    started = time.time()
    with _WakeLock():
        proc = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", creationflags=flags, startupinfo=startupinfo,
                                start_new_session=(os.name != "nt"))
        rc, out, killed_at = None, "", 0.0
        deadline = started + bound_s
        finished, finished_at = {}, 0.0
        try:
            while True:
                asked = min(BEAT_S, max(1, deadline - time.time()))
                if finished_at:
                    asked = min(asked, max(1, finished_at + forensics.FINISHED_GRACE_S - time.time()))
                beat_at = time.time()
                try:
                    out, _err = proc.communicate(timeout=asked)
                    rc = proc.returncode
                    break
                except subprocess.TimeoutExpired:
                    overrun = time.time() - beat_at - asked
                    if overrun > SUSPEND_BEAT_OVERRUN_S:
                        timing["suspended_s"] += int(overrun)
                    if watch_transcript and not finished_at:
                        path, _began = forensics.pass_transcript(workspace, started)
                        finished = forensics.session_finished(path) if path else {}
                        if finished:
                            finished_at = time.time()
                            log("run: the session finished at %s but has not exited; %ds for a "
                                "clean exit, then ending it" % (finished.get("at") or "?",
                                                               forensics.FINISHED_GRACE_S))
                    elif finished_at and time.time() - finished_at >= forensics.FINISHED_GRACE_S:
                        timing["finished_hung_s"] = int(time.time() - finished_at)
                        killed_at = time.time()
                        _kill_tree(proc)
                        try:
                            out, _err = proc.communicate(timeout=15)
                        except Exception:  # noqa: BLE001
                            out = ""
                        if not (out or "").strip():
                            import json
                            out = json.dumps({"result": finished.get("result") or "",
                                              "num_turns": 1,
                                              "usage": {"output_tokens": int(finished.get("tokens") or 0)}})
                        rc = 0                       # the work finished; the exit hung
                        break
                    if time.time() >= deadline:
                        killed_at = time.time()
                        _kill_tree(proc)
                        try:
                            out, _err = proc.communicate(timeout=15)
                        except Exception:  # noqa: BLE001
                            out = ""
                        rc = None
                        break
                    if on_beat is not None:
                        try:
                            on_beat()
                        except Exception:  # noqa: BLE001 - a heartbeat never kills the run
                            pass
        finally:
            timing["session_s"] = int((killed_at or time.time()) - started)
            timing["reaped"] = reap(proc)
            timing["kill_s"] = int(time.time() - killed_at) if killed_at else 0
            if timing["reaped"] != "reaped":
                log("WARNING: worker pid %d %s after its pass ended" % (proc.pid, timing["reaped"]))
    return rc, out or "", timing
