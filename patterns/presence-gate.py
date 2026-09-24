"""presence-gate.py — tell the board what this console is editing, derived, never self-reported.

The hub's FILE crossover (two live consoles editing the same `<project>/<path>` right now) is
the strongest duplicate-work signal there is and the one nobody sees from inside either
console. It needs each console's recently edited files, and asking an agent to report them is
asking for the one thing it forgets. This hook derives them from the session transcript the
harness already writes and sends them on the presence heartbeat (`X-Hub-Files`).

Target harness: Claude Code hooks — wire it in ~/.claude/settings.json on UserPromptSubmit and
Stop (and optionally SessionStart), command `python /path/to/presence-gate.py`. It reads the
hook event on stdin (session_id, transcript_path, cwd), always exits 0 (a broken gate must
never block a prompt), and bounds every outbound call. Stdlib only. The PATTERN ports to any
harness that records tool calls: adapt `_tool_uses()` and keep the detector.

Configuration (environment):
  HUB_API_BASE / HUB_AGENT_TOKEN   where and as whom to report (the ordinary client settings)
  HUB_CLIENT_PYTHONPATH            a directory that contains hub_core/ (when not installed)
  HUB_AGENT_ID, HUB_MACHINE        the seat's identity (machine defaults to the host name)
  HUB_GATE_CODE_ROOT               the directory that holds your project checkouts; paths are
                                   reported as <project>/<path> relative to it (default: the
                                   parent of the git repository the console stands in)
  HUB_GATE_ENV_CHECK               optional command whose report lines look like
                                   `[check] OK|FIXED|WOULD FIX|NEEDS A PERSON|UNVERIFIED detail`;
                                   every NEEDS A PERSON row reaches the board as an agent error
  HUB_GATE_TERMINAL_SELECTION=1    opt-in: restore the terminal's native text selection (below)

The detector's rules, each paid for on the origin system:

* EDITS THROUGH THE SHELL COUNT. Sessions told to edit through Bash (sed -i, heredocs, python
  scripts) did most of the editing while a detector that saw only Edit/Write reported nothing.
* JUDGED PER SEGMENT, NEVER PER CALL. One write verb anywhere in a chained command turned every
  path in the sibling READ commands, the heredoc body and the commit message into an "edit";
  five of six "files touched" on one console were reads, and the board raised collisions over
  them. Heredoc bodies are lifted out, the rest splits on ; && || | and newlines outside
  quotes, and each segment is judged alone: a read contributes nothing.
* A REDIRECT EDITS ONLY ITS TARGET, and a redirect to /dev/null, &1, &2 or NUL edits nothing.
* A COMMIT CONTRIBUTES ITS PATHSPEC, NOT ITS MESSAGE. A COPY WRITES ITS DESTINATION ONLY.
* A SCRIPT EDITS WHAT IT OPENS FOR WRITING. A heredoc consumed by an interpreter, or a helper
  the Write tool authored outside the project tree, counts only string literals that ARE a
  path, and only when the script writes; a sentence that mentions a file is prose.
* SUBAGENTS ARE THIS CONSOLE. Their transcripts are scanned too.
* THE PROJECT STAYS ON THE PATH. Two consoles in different repos editing `src/views.py` are
  not editing one file. A token that does not exist on disk is dropped.

Terminal selection (opt-in). Claude Code captures the mouse in fullscreen rendering, so a
click-drag goes to the application and the terminal's own selection never sees it; people ask
"why can't I copy out of this window" and are told a Shift+drag workaround, per person, per
machine. The documented switch is an env key in settings.json, so the gate — the one artifact
that runs everywhere on every session — sets it: CLAUDE_CODE_DISABLE_MOUSE_CLICKS (clicks and
drag only, wheel scroll kept) on a client that knows it, the blunt CLAUDE_CODE_DISABLE_MOUSE
(wheel scroll LOST) only on an older one. The person's choice wins: if either key is already
set, including a deliberate "0", nothing is touched. A file that will not parse is left alone,
only our own key is written, and only on a difference. THE BLUNT PATH ANNOUNCES ITSELF on the
board with what it cost, the version that caused it and the way out, because the branch that
only runs on the machine nobody is watching is the one that must say the most; the narrow path
stays silent, since announcing what every machine does is noise.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

WINDOW_S = 600                 # "edited recently" == the hub's file-overlap window
TAIL_BYTES = 262144
FILES_MAX = 12
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

_NULL_REDIRECT = re.compile(r"\d?>{1,2}\s*(?:/dev/null|&\d|NUL\b)", re.I)
_REDIRECT_TARGET = re.compile(r"(?<![\d&])>{1,2}\s*([^\s|;&()<>]+)")
_WRITE_VERB = re.compile(r"(?:^|[\s;&|(])(?:sed\s+-i|tee|cp|mv|rm|touch|mkdir|install|"
                         r"git\s+(?:commit|add|rm|mv|checkout|apply|am)|cat\s*>|"
                         r"Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Copy-Item|Move-Item)")
_ANY_WRITE = re.compile(_WRITE_VERB.pattern[:-1] + r"|>{1,2})")
_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1")
_RUNNER = re.compile(r"(?:^|[\s;&|(])(?:python[0-9.]*|py|node|pwsh|powershell|ruby|perl)(?:\.exe)?\s")
_SCRIPT_WRITE = re.compile(r"open\([^)\n]*['\"][wax]b?\+?['\"]|\.write_text\(|\.write_bytes\(|"
                           r"os\.replace\(|shutil\.(?:copy|copy2|copyfile|move)\(|writeFileSync\(|"
                           r"Set-Content|Out-File")
_SOLE_PATH_LITERAL = re.compile(r"""(['"])([^'"\s]+)\1""")
_COMMIT_MSG = re.compile(r"""(?:-m|--message)(?:=|\s+)(?:"(?:[^"\\]|\\.)*"|'[^']*')""", re.S)
_GIT_COMMIT = re.compile(r"(?:^|[\s;&|(])git\s+commit\b")
_CONTENT_WRITE = re.compile(r"(?:^|[\s;&|(])(?:cat\s*>|tee\b|>{1,2})")
_CD = re.compile(r"^\s*(?:cd|Set-Location|pushd)\s+(\S+)\s*$", re.I)
_COPY = re.compile(r"^\s*(?:sudo\s+)?(?:cp|install|Copy-Item)(?=\s)", re.I)
_PATHISH = re.compile(r"""(?<![\w/.:-])((?:[A-Za-z]:[\\/]|/)?(?:[\w.-]+[\\/])+[\w.-]+\.\w{1,8})(?![\w/])""")


# ── Where the projects live, and the <project>/<path> form ──

def _git_root(cwd: str) -> Path | None:
    p = Path(cwd or ".").resolve()
    for cand in (p, *p.parents):
        if (cand / ".git").exists():
            return cand
    return None


def code_root(cwd: str) -> Path | None:
    configured = os.environ.get("HUB_GATE_CODE_ROOT")
    if configured:
        return Path(configured).resolve()
    repo = _git_root(cwd)
    return repo.parent if repo else None


def _native(path: str) -> str:
    """A shell path as the OS sees it: /c/x (Git Bash) -> C:/x on Windows."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", path)
    if os.name == "nt" and m:
        return "%s:/%s" % (m.group(1).upper(), m.group(2))
    return path


def rel_path(token: str, cwd: str, root: Path | None) -> str:
    """`<project>/<path inside it>` for a token that names an existing file under the code
    root, else ''. Executables are run, never edited."""
    if not root or not token or "://" in token:
        return ""
    raw = _native(str(token).strip().strip("\"'"))
    p = Path(raw)
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        p = p.resolve()
        rel = p.relative_to(root).as_posix()
    except (OSError, ValueError):
        return ""
    if "/" not in rel or rel.lower().endswith((".exe", ".dll", ".so", ".pyd")):
        return ""
    return rel[:200] if p.is_file() else ""


# ── The shell detector ──

def segments(cmd: str) -> list[tuple[str, str]]:
    """(segment, heredoc body or '') for every top-level command. Heredoc bodies are lifted out
    by their terminator line first; the rest splits on ; && || | and newlines OUTSIDE quotes,
    so a multi-line commit message stays one segment with its pathspec."""
    lines, kept, bodies, i = str(cmd).split("\n"), [], {}, 0
    while i < len(lines):
        kept.append(lines[i])
        m = _HEREDOC_OPEN.search(lines[i])
        i += 1
        if m:
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            bodies[len(kept) - 1] = "\n".join(body)
    text, segs, cur, quote, esc, line_no = "\n".join(kept), [], [], "", False, 0
    cur_lines = {0}
    j = 0
    while j < len(text):
        ch = text[j]
        if quote:
            cur.append(ch)
            if quote == '"' and ch == "\\" and not esc:
                esc = True
            elif ch == quote and not esc:
                quote = ""
            else:
                esc = False
            if ch == "\n":
                line_no += 1
                cur_lines.add(line_no)
            j += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif text[j:j + 2] in ("&&", "||"):
            segs.append(("".join(cur), cur_lines))
            cur, cur_lines = [], {line_no}
            j += 1
        elif ch in (";", "|"):
            segs.append(("".join(cur), cur_lines))
            cur, cur_lines = [], {line_no}
        elif ch == "\n":
            segs.append(("".join(cur), cur_lines))
            line_no += 1
            cur, cur_lines = [], {line_no}
        else:
            cur.append(ch)
        j += 1
    segs.append(("".join(cur), cur_lines))
    out = []
    for seg, owned in segs:
        seg = seg.strip()
        if not seg:
            continue
        body = ""
        if _HEREDOC_OPEN.search(seg):
            for k in sorted(owned):
                if k in bodies:
                    body = bodies.pop(k)
                    break
        out.append((seg, body))
    return out


def paths_in_script(body: str, cwd: str, root) -> list[str]:
    """What a script edits: string literals that ARE a path, only when the script writes."""
    if not body or not _SCRIPT_WRITE.search(body):
        return []
    return [r for r in (rel_path(m.group(2), cwd, root) for m in _SOLE_PATH_LITERAL.finditer(body)) if r]


def _copy_destination(args_text: str) -> str:
    try:
        toks = shlex.split(args_text, posix=True)
    except ValueError:
        toks = args_text.split()
    operands, i = [], 0
    while i < len(toks):
        if toks[i] in ("-t", "--target-directory", "-Destination") and i + 1 < len(toks):
            return toks[i + 1]
        if not toks[i].startswith("-"):
            operands.append(toks[i])
        i += 1
    return operands[-1] if len(operands) >= 2 else ""


def paths_in_segment(seg: str, body: str, cwd: str, root) -> list[str]:
    if body and _RUNNER.search(seg + " ") and not _CONTENT_WRITE.search(seg):
        return paths_in_script(body, cwd, root)          # a script on stdin
    if not _ANY_WRITE.search(seg):
        return []                                        # a read contributes nothing
    if not _WRITE_VERB.search(seg):                      # only a redirect: its target alone
        return [r for r in (rel_path(t, cwd, root) for t in _REDIRECT_TARGET.findall(seg)) if r]
    if _GIT_COMMIT.search(seg):
        seg = _COMMIT_MSG.sub(" ", seg)
        seg = seg.split(" -- ", 1)[1] if " -- " in seg else ""
    copy = _COPY.match(seg)
    if copy and not _CONTENT_WRITE.search(seg):
        seg = _copy_destination(seg[copy.end():])        # the source of a copy is read
    return [r for r in (rel_path(m.group(1), cwd, root) for m in _PATHISH.finditer(seg)) if r]


def paths_in_command(cmd: str, cwd: str, root) -> list[str]:
    cmd = _NULL_REDIRECT.sub(" ", str(cmd or ""))
    if not cmd or not (_ANY_WRITE.search(cmd) or _SCRIPT_WRITE.search(cmd)):
        return []
    out = []
    for seg, body in segments(cmd):
        moved = _CD.match(seg)
        if moved:                                        # `cd repo && sed -i ... src/x.py`
            target = Path(_native(moved.group(1).strip("\"'")))
            cwd = str(target if target.is_absolute() else Path(cwd) / target)
            continue
        out.extend(paths_in_segment(seg, body, cwd, root))
    return out


# ── The transcript ──

def _tool_uses(path: str, since: float):
    """(tool name, input) for every tool call after `since` in a transcript's tail."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return
    for line in chunk.splitlines()[1:]:
        if '"tool_use"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = str(rec.get("timestamp") or "")
        try:
            when = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) if ts else 0
        except (ValueError, OverflowError):
            when = 0
        if when and when < since:
            continue
        content = (rec.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                yield c.get("name"), c.get("input") or {}


def _subagent_transcripts(path: str) -> list[str]:
    base = path[:-len(".jsonl")] if path.endswith(".jsonl") else ""
    d = Path(base) / "subagents" if base else None
    try:
        return [str(x) for x in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)[-6:]] \
            if d and d.is_dir() else []
    except OSError:
        return []


def files_touched(transcript: str, cwd: str, since: float) -> list[str]:
    root = code_root(cwd)
    if not transcript or not root:
        return []
    out = []
    for src in [transcript] + _subagent_transcripts(transcript):
        for name, inp in _tool_uses(src, since):
            if name in EDIT_TOOLS:
                rel = rel_path(inp.get("file_path") or inp.get("notebook_path") or "", cwd, root)
                if rel:
                    out.append(rel)
                elif name == "Write":                    # a helper script authored elsewhere
                    out.extend(paths_in_script(inp.get("content") or "", cwd, root))
            elif name in ("Bash", "PowerShell"):
                out.extend(paths_in_command(inp.get("command") or "", cwd, root))
    seen, files = set(), []
    for rel in reversed(out):                            # newest first
        if rel not in seen:
            seen.add(rel)
            files.append(rel)
        if len(files) >= FILES_MAX:
            break
    return files


# ── Environment report rows ──

_ENV_LINE = re.compile(r"^\s*\[(?P<check>[^\]]+)\]\s+(?P<verdict>OK|FIXED|WOULD FIX|NEEDS A PERSON|"
                       r"UNVERIFIED)\b\s*(?P<detail>.*)$")


def env_report_rows(text: str) -> list[tuple[str, str, str]]:
    """(check, verdict, detail) rows. An INDENTED line under a row continues its detail and is
    folded in: a check that prints the thing a person must act on (a key to register, a line to
    add) on its own line otherwise reaches the board as "add this line:" with no line. A banner
    or blank line ends the row. UNVERIFIED is a verdict too, or its continuation would fold
    into the previous row."""
    rows, open_row = [], False
    for line in str(text or "").splitlines():
        m = _ENV_LINE.match(line)
        if m:
            rows.append([m.group("check").strip(), m.group("verdict"), m.group("detail").strip()])
            open_row = True
        elif open_row and line[:1].isspace() and line.strip():
            rows[-1][2] = (rows[-1][2] + " " + line.strip()).strip()
        else:
            open_row = False
    return [tuple(r) for r in rows]


# ── Terminal selection (opt-in) ──

MOUSE_CLICKS_ENV = "CLAUDE_CODE_DISABLE_MOUSE_CLICKS"
MOUSE_ALL_ENV = "CLAUDE_CODE_DISABLE_MOUSE"
MOUSE_CLICKS_MIN_VERSION = (2, 1, 195)


def _cli_version():
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15,
                             errors="replace").stdout
    except Exception:                                    # noqa: BLE001
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def mouse_env_choice(doc, version):
    """(key, value) still needed, or None. The person's choice wins; an unreported version gets
    the narrow key (an unknown key leaves an old client as it is; guessing the blunt one would
    cost it scrolling on a guess)."""
    env = doc.get("env", {}) if isinstance(doc, dict) else None
    if not isinstance(env, dict) or MOUSE_CLICKS_ENV in env or MOUSE_ALL_ENV in env:
        return None
    if version is not None and version < MOUSE_CLICKS_MIN_VERSION:
        return (MOUSE_ALL_ENV, "1")
    return (MOUSE_CLICKS_ENV, "1")


def reconcile_terminal_selection(settings: Path, report) -> str:
    try:
        doc = json.loads(settings.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "unreadable"                              # never touch a file that will not parse
    if mouse_env_choice(doc, None) is None:
        return "nothing to do"                           # the common tick: one read, no subprocess
    version = _cli_version()
    choice = mouse_env_choice(doc, version)
    if choice is None:
        return "nothing to do"
    key, value = choice
    doc.setdefault("env", {})[key] = value
    tmp = settings.with_suffix(".gate-tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, settings)
    if key == MOUSE_ALL_ENV:
        report("native text selection was restored with the BLUNT switch (%s), so wheel scrolling "
               "is now off in this terminal. This client is %s, below %s, the first version with "
               "the narrow click-only switch. Updating the client and removing the key from "
               "settings.json gives scrolling back; setting it to \"0\" keeps the mouse as it was."
               % (MOUSE_ALL_ENV, ".".join(map(str, version)) if version else "an unreported version",
                  ".".join(map(str, MOUSE_CLICKS_MIN_VERSION))),
               "mouse_blunt_switch_applied")
    return "set %s" % key


# ── Reporting ──

def _client(args: list[str], env: dict) -> int:
    try:
        return subprocess.run([sys.executable, "-m", "hub_core.client", *args], env=env,
                              capture_output=True, timeout=45).returncode
    except Exception:                                    # noqa: BLE001
        return -1


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except Exception:                                    # noqa: BLE001
        return 0
    cwd = str(event.get("cwd") or os.getcwd())
    session = str(event.get("session_id") or "")
    env = dict(os.environ, HUB_CWD=cwd, HUB_SESSION_ID=session[:64])
    extra = os.environ.get("HUB_CLIENT_PYTHONPATH")
    if extra:
        env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
    machine = os.environ.get("HUB_MACHINE") or socket.gethostname().lower()
    agent = os.environ.get("HUB_AGENT_ID") or ""
    ident = (["--agent", agent] if agent else []) + ["--machine", machine]

    def report(message: str, code: str, severity: str = "warning", details: str = "") -> None:
        _client(["agent-error", "--message", message[:780], "--code", code, "--severity", severity,
                 "--source", "gate", "--details", details[:1900], *ident], env)

    if os.environ.get("HUB_API_BASE"):
        files = files_touched(str(event.get("transcript_path") or ""), cwd, time.time() - WINDOW_S)
        _client(["presence", *ident, "--files", *files], env)
        check = os.environ.get("HUB_GATE_ENV_CHECK")
        if check:
            try:
                text = subprocess.run(check, shell=True, capture_output=True, text=True,
                                      timeout=60, errors="replace").stdout
            except Exception:                            # noqa: BLE001
                text = ""
            for name, verdict, detail in env_report_rows(text):
                if verdict == "NEEDS A PERSON":
                    report("%s on %s needs a person: %s" % (name, machine, detail),
                           "env_needs_a_person", "error")
    if os.environ.get("HUB_GATE_TERMINAL_SELECTION") == "1":
        settings = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
        try:
            reconcile_terminal_selection(settings, report)
        except OSError as exc:
            report("could not restore native terminal selection", "mouse_env_write_failed",
                   details=type(exc).__name__)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                    # noqa: BLE001 - a gate never blocks a prompt
        sys.exit(0)
