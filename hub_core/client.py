#!/usr/bin/env python3
"""Small, dependency-free client for the Hub's literal-realtime write seam.

This module deliberately has no EventStore import.  An active Hub is mutated through its served
HTTP API so the durable append, lease fencing, authorization, and realtime publication happen as
one operation.  Direct ledger access is reserved for an offline recovery boundary.

Examples::

    HUB_API_BASE=https://project.example/hub HUB_AGENT_TOKEN=... \
      python -m hub_core.client create --title "Ship export" \
      --acceptance "The live export succeeds" --priority P1

    python -m hub_core.client claim project:task:0042 --agent worker-1
    HUB_LEASE_TOKEN=... python -m hub_core.client complete project:task:0042 \
      --agent worker-1 --accept-note "Live export returned the artifact" \
      --evidence https://project.example/export/latest

The delivery loop rides the same seam. Blocked on a fact only the operator has? Never stall
in silence::

    python -m hub_core.client ask --agent worker-1 \
      --question "which queue drains the nightly import backlog" --context "stalls at step 3"
    python -m hub_core.client inbox --agent operator          # what is addressed to me now
    python -m hub_core.client wait --agent operator --follow  # block; print arrivals (a notifier)
    python -m hub_core.client answer project:note:q-worker-1-1a2b3c4d \
      --text "the retry queue; requeue stalled items"         # needs directive:write
    python -m hub_core.client ack project:directive:0001 --agent worker-1

`presence` is the seat heartbeat between tasks (focus/cwd/machine/session ride HUB_MACHINE,
HUB_SESSION_ID, or flags), and `app-error` / `agent-error` / `ack-error` / `ci-event` feed the
operational error stream.

The error stream is worked as PROBLEMS - one line per thing somebody fixes::

    python -m hub_core.client errors --mine               # the folded queue (--app, --all)
    python -m hub_core.client errors --trace p-0123456789ab   # one problem, full stored trace
    python -m hub_core.client claim p-0123456789ab --note "checking the importer"
    python -m hub_core.client resolve p-0123456789ab --note "<root cause>" --evidence <sha|url>
    python -m hub_core.client escalate p-0123456789ab --blocked-on q-worker-1-1a2b3c4d
    python -m hub_core.client health                       # every service: observed/partial/dark
    python -m hub_core.client doctor budget-app            # one service, BLOCKED vs WAITING
    python -m hub_core.client consoles                     # live consoles + crossover pairs
    python -m hub_core.client ack <any id>                 # routed by the id's own type

A call that finds the hub unreachable opens a BLIND WINDOW in local state; the first call that
succeeds afterwards reports it (agent-error `hub_unreachable_span`) - graded error when a write
was stranded inside it or a console was working through it, warning otherwise - so a gap in what
the hub saw is itself on record.

The worker LOOP rides the same seam — the converged core of two adopter fleets::

    python -m hub_core.client next                       # top ready + needs-spec + snoozed
    python -m hub_core.client start proj:task:0042 --agent worker-1
    python -m hub_core.client step  proj:task:0042 --agent worker-1 --note "schema landed"
    HUB_LEASE_TOKEN=... python -m hub_core.client finish proj:task:0042 --agent worker-1 \
      --accept-note "export live" --evidence https://app.example/export --charter-sha <sha>
    python -m hub_core.client reground                   # after context compaction

When the board ships CHARTER-CORE.md, `finish` refuses a completion whose held charter sha is
stale or absent — compaction drift is detected at the gate, never discovered later in the work.
A task that declared a critical-boundary verification_command has it run BY `finish` on this
worker through hub_core.verifier hardening, and the typed receipt rides the completion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; HubLiveClient/1.0)"


def _base_url(value: str | None) -> str:
    raw = (value or os.environ.get("HUB_API_BASE") or "").strip().rstrip("/")
    if not raw:
        raise ValueError("set HUB_API_BASE to the served Hub URL, for example https://app.example/hub")
    if raw.endswith("/api"):
        raw = raw[:-4]
    return raw


def _auth_headers() -> dict[str, str]:
    agent_token = os.environ.get("HUB_AGENT_TOKEN", "").strip()
    if agent_token:
        return {"X-Agent-Token": agent_token}
    write_token = os.environ.get("HUB_WRITE_TOKEN", "").strip()
    if write_token:
        return {"X-Write-Token": write_token}
    raise ValueError("set HUB_AGENT_TOKEN (preferred) or HUB_WRITE_TOKEN in the process environment")


# -- The blind window: a span in which this client could not reach the hub --
# Every failure inside it is invisible to the board by construction (the report channel IS
# the hub), so the span is remembered locally - across restarts - and reported on the first
# call that succeeds. Byte-locked so two processes on one machine cannot tear the state, and
# read with a retry so a reader never mistakes a half-written file for "no window".

_REPORTING = {"active": False}


def _state_dir() -> str:
    return os.environ.get("HUB_CLIENT_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".hub-client")


def _window_path() -> str:
    return os.path.join(_state_dir(), "blind-window.json")


class _StateLock:
    """A tiny O_EXCL byte lock (stdlib-only; this client must stay copy-able on its own)."""

    def __init__(self, path: str, timeout: float = 2.0):
        self.path, self.timeout, self.held = path + ".lock", timeout, False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 30:
                        os.unlink(self.path)          # a crashed holder; the state is tiny
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    return self                      # fail OPEN: an unlocked write beats none
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _read_window() -> dict:
    for _attempt in range(3):
        try:
            with open(_window_path(), "r", encoding="utf-8") as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.05)                        # a torn read: the writer is mid-replace
    return {}


def _write_window(value: dict) -> None:
    os.makedirs(_state_dir(), exist_ok=True)
    temp = _window_path() + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(value, fh)
    os.replace(temp, _window_path())


def _window_failed(base: str, write: bool, reason) -> None:
    try:
        with _StateLock(_window_path()):
            state = _read_window()
            now = time.time()
            if not state.get("opened_at"):
                state = {"opened_at": now, "base": base, "calls": 0, "stranded": 0}
            state["calls"] = int(state.get("calls") or 0) + 1
            if write:
                state["stranded"] = int(state.get("stranded") or 0) + 1
            state["last_failed_at"] = now
            state["reason"] = str(reason)[:200]
            _write_window(state)
    except OSError:
        pass


def _window_closed(base: str) -> None:
    """The hub answered: if a blind window was open, close it and REPORT it. The report is
    itself a write; if it fails the window stays open for the next success to try again."""
    if _REPORTING["active"]:
        return
    try:
        with _StateLock(_window_path()):
            state = _read_window()
            if not state.get("opened_at"):
                return
            _write_window({})
    except OSError:
        return
    now = time.time()
    opened = float(state.get("opened_at") or now)
    span = max(0, int(now - opened))
    calls = int(state.get("calls") or 0)
    stranded = int(state.get("stranded") or 0)
    working = bool(os.environ.get("HUB_SESSION_ID"))
    # GRADED: a window matters as an error only when something was LOST in it - a write that
    # could not be delivered, or a console working through it. Otherwise it is a warning.
    # Either way the read-time bar keeps it off the queue (it had already closed when it
    # reported, so nobody can pick it up); it is counted, and retired by later presence.
    payload = {
        "agent": os.environ.get("HUB_AGENT_ID") or "agent",
        "source": "client", "code": "hub_unreachable_span",
        "severity": "error" if (stranded or working) else "warning",
        "message": "hub unreachable for %ds (%d call%s failed, %d write%s stranded)" % (
            span, calls, "" if calls == 1 else "s", stranded, "" if stranded == 1 else "s"),
        "details": "opened %s, closed %s, last reason: %s" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(opened)),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), state.get("reason") or "?"),
    }
    if os.environ.get("HUB_MACHINE"):
        payload["machine"] = os.environ["HUB_MACHINE"]
    _REPORTING["active"] = True
    try:
        _post(base, "agent-error", payload)
    except (RuntimeError, ValueError):
        try:
            with _StateLock(_window_path()):
                if not _read_window().get("opened_at"):
                    _write_window(state)            # keep it for the next success
        except OSError:
            pass
    finally:
        _REPORTING["active"] = False


def _post(base: str, operation: str, payload: dict[str, Any],
          extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Some production edges reject Python urllib's default signature before the request can
        # reach Hub authentication. Keep a stable browser-compatible identity while allowing an
        # adopter to name its own operational client at the edge.
        "User-Agent": os.environ.get("HUB_CLIENT_USER_AGENT", DEFAULT_USER_AGENT),
        **_auth_headers(),
        **(extra_headers or {}),
    }
    request = urllib.request.Request(
        f"{base}/api/{operation}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        _window_closed(base)                        # the hub ANSWERED, even if it said no
        detail = error.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(detail)
        except json.JSONDecodeError:
            body = detail
        raise RuntimeError(json.dumps({"status": error.code, "response": body})) from error
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        reason = getattr(error, "reason", error)
        if not _REPORTING["active"]:
            _window_failed(base, True, reason)
        raise RuntimeError(f"Hub is unreachable at {base}: {reason}") from error
    _window_closed(base)
    return result


def _optional_auth_headers() -> dict[str, str]:
    try:
        return _auth_headers()
    except ValueError:
        return {}          # reads are public; whoami simply reports no credential


def _presence_headers(arguments: argparse.Namespace | None = None) -> dict[str, str]:
    """The observed-presence headers every write may carry. Environment first, flags win —
    the board's live-console view is only as true as what the seats send."""
    values = {
        "X-Hub-Machine": os.environ.get("HUB_MACHINE", ""),
        "X-Hub-Session": os.environ.get("HUB_SESSION_ID", ""),
        "X-Hub-Cwd": os.environ.get("HUB_CWD") or os.getcwd(),
        "X-Hub-Focus": os.environ.get("HUB_FOCUS", ""),
        # What crossover detection compares: the project this console stands in (declared,
        # or the repository's own folder name), the files it edited, its display name, and
        # whether it is an unattended process nobody is reading.
        "X-Hub-Project": os.environ.get("HUB_PROJECT") or _repo_name(),
        "X-Hub-Files": os.environ.get("HUB_FILES", ""),
        "X-Hub-Console-Name": os.environ.get("HUB_CONSOLE_NAME", ""),
        "X-Hub-Unattended": os.environ.get("HUB_UNATTENDED", ""),
    }
    if arguments is not None:
        if getattr(arguments, "machine", None):
            values["X-Hub-Machine"] = arguments.machine
        if getattr(arguments, "focus", None):
            values["X-Hub-Focus"] = arguments.focus
        if getattr(arguments, "project", None):
            values["X-Hub-Project"] = arguments.project
        if getattr(arguments, "file", None):
            values["X-Hub-Files"] = ",".join(arguments.file)
        if getattr(arguments, "name", None):
            values["X-Hub-Console-Name"] = arguments.name
        if getattr(arguments, "unattended", None):
            values["X-Hub-Unattended"] = "1"
    return {name: value for name, value in values.items() if value}


def _repo_name() -> str:
    """The repository this process stands in, by its top-level folder name — never a guess
    from a directory leaf: a console standing above every project declares none."""
    path = os.path.abspath(os.environ.get("HUB_CWD") or os.getcwd())
    while True:
        marker = os.path.join(path, ".git")
        if os.path.isfile(marker):
            # A linked worktree: its folder is a throwaway name; the project is the
            # repository the worktree belongs to ("gitdir: <repo>/.git/worktrees/<name>").
            try:
                with open(marker, "r", encoding="utf-8") as fh:
                    gitdir = fh.read().split("gitdir:", 1)[-1].strip().replace("\\", "/")
                if "/.git/worktrees/" in gitdir:
                    return gitdir.split("/.git/worktrees/", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
            except OSError:
                pass
            return os.path.basename(path).lower()
        if os.path.isdir(marker):
            return os.path.basename(path).lower()
        parent = os.path.dirname(path)
        if parent == path:
            return ""
        path = parent


def _get(base: str, path: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base}/{path.lstrip('/')}",
        headers={"Accept": "application/json",
                 "User-Agent": os.environ.get("HUB_CLIENT_USER_AGENT", DEFAULT_USER_AGENT),
                 **_optional_auth_headers()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        _window_closed(base)
        detail = error.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(detail)
        except json.JSONDecodeError:
            body = detail
        raise RuntimeError(json.dumps({"status": error.code, "response": body})) from error
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        reason = getattr(error, "reason", error)
        _window_failed(base, False, reason)
        raise RuntimeError(f"Hub is unreachable at {base}: {reason}") from error
    _window_closed(base)
    return result


def _agent(arguments: argparse.Namespace) -> str:
    return arguments.agent or os.environ.get("HUB_AGENT_ID") or "agent"


def _payload_create(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "title": arguments.title,
        "acceptance": arguments.acceptance,
        "priority": arguments.priority,
        "agent": _agent(arguments),
    }
    if arguments.phase:
        payload["phase"] = arguments.phase
    if arguments.touch:
        payload["touches"] = arguments.touch
    if arguments.plan_item:
        payload["plan"] = [
            {"step": step, "done": False} for step in arguments.plan_item
        ]
    return "task", payload


_PROBLEM_ID = re.compile(r"(?:problem:)?(p-[0-9a-f]{12})")
_FINGERPRINT_ID = re.compile(r"[0-9a-f]{16}")


def _problem_id(value: str) -> str:
    """The p-<12 hex> id inside `value` (bare, or as an inbox item id), or ""."""
    m = _PROBLEM_ID.fullmatch(str(value or "").strip().lower())
    return m.group(1) if m else ""


def _console_fields() -> dict[str, str]:
    """Who is acting, as a CONSOLE: a problem's holder is one console, not a whole agent."""
    out = {"machine": os.environ.get("HUB_MACHINE", ""),
           "session": os.environ.get("HUB_SESSION_ID", ""),
           "name": os.environ.get("HUB_CONSOLE_NAME", "")}
    return {k: v for k, v in out.items() if v}


def _payload_claim(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    pid = _problem_id(arguments.task_id)
    if pid:
        # One verb, routed by the id's own type: a problem id claims the PROBLEM for this
        # console (409 names the live holder; --take displaces it on the record).
        payload: dict[str, Any] = {"problem": pid, "agent": _agent(arguments),
                                   **_console_fields()}
        if arguments.note:
            payload["note"] = arguments.note
        if arguments.take:
            payload["take"] = True
        return "problem/claim", payload
    payload = {"id": arguments.task_id, "agent": _agent(arguments)}
    if arguments.ttl_s is not None:
        payload["ttl_s"] = arguments.ttl_s
    return "claim", payload


def _payload_heartbeat(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")
    payload: dict[str, Any] = {
        "id": arguments.task_id,
        "token": token,
        "agent": _agent(arguments),
    }
    if arguments.ttl_s is not None:
        payload["ttl_s"] = arguments.ttl_s
    return "heartbeat", payload


def _payload_complete(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")
    payload: dict[str, Any] = {
        "id": arguments.task_id,
        "token": token,
        "agent": _agent(arguments),
        "accept_note": arguments.accept_note,
        "evidence_uri": arguments.evidence,
    }
    if arguments.expected_version is not None:
        payload["expected_version"] = arguments.expected_version
    return "complete", payload


def _payload_ask(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "agent": _agent(arguments),
        "question": arguments.question,
    }
    if arguments.context:
        payload["context"] = arguments.context
    if arguments.relates_to:
        payload["relates_to"] = arguments.relates_to
    if arguments.anyway:
        payload["anyway"] = True
    return "ask", payload


def _payload_answer(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"question": arguments.question_id, "text": arguments.text}
    if arguments.crystallize:
        payload["crystallize"] = True
    return "answer", payload


def _payload_directive(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": arguments.title, "body_md": arguments.body}
    if arguments.target:
        payload["targets"] = arguments.target
    if arguments.remediation_cmd:
        payload["remediation_cmd"] = arguments.remediation_cmd
    # PINNED delivery: one computer, and/or one console (session id or its display NAME —
    # the hub refuses a console that is not live, rather than delivering nowhere).
    if arguments.pin_machine:
        payload["machine"] = arguments.pin_machine
    if arguments.session:
        payload["session"] = arguments.session
    return "directive", payload


def _ack_any(base: str, item_id: str, agent: str, note: str = "",
             evidence: str = "") -> dict[str, Any]:
    """Acknowledge ANY addressed id — the caller should not have to know which store it
    lives in. Routed by the id's own type, with the directive store asked first for anything
    else, and a fingerprint considered only when the directive store does not know the id:

      ov-...                  a crossover signal -> recorded as seen for this side
      p-<12 hex> / problem:p- a problem -> RESOLVED (acks every row behind it; needs a note)
      anything else           a directive or an answer -> api/ack
      16 hex, not a directive an error signature -> api/ack-error

    Only a 404 unknown_directive falls through, and only for a fingerprint-SHAPED id: a 403
    means the right endpoint said no, and retrying elsewhere would turn a permissions answer
    into a confusing one; a mistyped directive id must fail loudly, never mint an ack."""
    item_id = str(item_id or "").strip()
    if item_id.startswith("ov-"):
        body = _post(base, "overlap/seen", {"agent": agent, "ids": [item_id]})
        return {"routed": "overlap", "response": body}
    pid = _problem_id(item_id)
    if pid:
        if not note:
            raise ValueError("a problem is closed by RESOLVING it: pass --note with the root "
                             "cause (and --evidence), or `claim` it if you are only picking it up")
        body = _post(base, "problem/resolve", {"problem": pid, "agent": agent, "note": note,
                                               "evidence": evidence, **_console_fields()})
        return {"routed": "problem", "response": body}
    payload: dict[str, Any] = {"agent": agent, "directive": item_id}
    if note:
        payload["note"] = note
    try:
        return {"routed": "directive", "response": _post(base, "ack", payload)}
    except RuntimeError as error:
        text = str(error)
        if '"status": 404' not in text or "unknown_directive" not in text \
                or not _FINGERPRINT_ID.fullmatch(item_id.lower()):
            raise
    body = _post(base, "ack-error", {"agent": agent, "fingerprint": item_id.lower(),
                                     "note": note})
    return {"routed": "error-signature", "response": body}


def _run_ack(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _ack_any(base, arguments.directive_id, _agent(arguments), arguments.note or "",
                    arguments.evidence or "")


def _payload_presence(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    return "presence", {"agent": _agent(arguments)}


def _payload_forget_presence(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {}
    if arguments.machine:
        payload["machine"] = arguments.machine
    if arguments.target:
        payload["target"] = arguments.target
    return "forget-presence", payload


def _payload_app_error(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"app": arguments.app, "message": arguments.message}
    for name in ("kind", "severity", "code", "details", "component", "operation", "path", "host"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    return "app-error", payload


def _payload_agent_error(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "message": arguments.message}
    for name in ("source", "severity", "code", "details", "machine"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    return "agent-error", payload


def _payload_ack_error(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"fingerprint": arguments.fingerprint, "agent": _agent(arguments)}
    if arguments.reopen:
        payload["reopen"] = True
    if arguments.note:
        payload["note"] = arguments.note
    return "ack-error", payload


def _where_query() -> str:
    """?machine=&session= for addressed reads — a directive PINNED to one computer or console
    is delivered only to a caller that says it is there."""
    from urllib.parse import quote
    out = ""
    if os.environ.get("HUB_MACHINE"):
        out += "&machine=" + quote(os.environ["HUB_MACHINE"])
    if os.environ.get("HUB_SESSION_ID"):
        out += "&session=" + quote(os.environ["HUB_SESSION_ID"])
    return out


def _run_inbox(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    if arguments.ack:
        return _ack_any(base, arguments.ack, arguments.agent, arguments.note or "",
                        arguments.evidence or "")
    return _get(base, f"inbox.json?agent={quote(arguments.agent)}{_where_query()}")


def _run_wait(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Block until something is addressed to the agent. With --follow, loop forever and
    print each CHANGED addressed set as one JSON line — the building block for a desktop
    notifier or a supervisor hook: pipe it to whatever raises attention on your platform.
    The fingerprint round-trips so unrelated board traffic never produces output."""
    from urllib.parse import quote
    import time as _time
    fingerprint = arguments.fp or ""
    backoff = 1.0
    while True:
        try:
            payload = _get(
                base,
                f"inbox/wait?agent={quote(arguments.agent)}&fp={quote(fingerprint)}"
                f"&wait={arguments.wait}{_where_query()}",
                timeout=arguments.wait + 15,
            )
        except RuntimeError as error:
            # A notifier that dies on the first transient outage is a notifier that is
            # silently off exactly when the hub comes back with news. In --follow mode,
            # back off and retry (capped); a one-shot wait still reports the failure.
            if not arguments.follow:
                raise
            print(json.dumps({"transient": str(error)[:200], "retry_in_s": backoff}),
                  file=sys.stderr, flush=True)
            _time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        backoff = 1.0
        data = payload.get("data") or {}
        changed = data.get("fingerprint") != fingerprint
        fingerprint = data.get("fingerprint") or fingerprint
        if not arguments.follow:
            return payload
        if changed:
            print(json.dumps(data, sort_keys=True), flush=True)


def _run_search(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"search.json?q={quote(arguments.query)}&limit={arguments.limit}")


def _run_questions(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "questions.json")


def _run_whoami(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "whoami.json")


# -- Problems: the error stream folded into owned, reachable work --

def _age(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


def _problem_line(p: dict[str, Any]) -> str:
    """One queue line: state, severity, where, title, count, RECENCY — and the cause, so the
    reader names the failure without a second call."""
    esc = p.get("escalation") or {}
    state = (p.get("holder_phrase") or p.get("state") or "") if p.get("holder") else (
        ("escalated, waiting on %s" % esc.get("blocked_on")) if p.get("state") == "escalated"
        else str(p.get("state") or "").replace("_", " "))
    line = "%s [%s] %s - %s  x%s, last %s ago - %s" % (
        p.get("id"), str(p.get("severity") or "").upper(), p.get("where"),
        str(p.get("title") or "")[:140], p.get("count"), _age(p.get("since_last_s")), state)
    if p.get("bar") == "deferred" and p.get("defer_reason"):
        line += "  (off the queue: %s)" % p["defer_reason"]
    if p.get("cause"):
        line += "\n    cause: %s" % str(p["cause"])[:240]
    return line


def _run_errors(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """The QUEUE, folded into problems (never raw rows): --mine (owned by or held by me),
    --app <slug>, --all (include what the bar holds back), --resolved; --trace <p-id> prints
    one problem with its full stored trace (head AND tail)."""
    from urllib.parse import quote
    if arguments.trace:
        pid = _problem_id(arguments.trace)
        if not pid:
            raise ValueError("--trace takes a problem id (p-<12 hex>)")
        body = _get(base, f"problems.json?id={pid}")
        p = body.get("data") or {}
        return {"line": _problem_line(p), "trace": p.get("details") or "(no trace stored)",
                "problem": p}
    query = []
    if arguments.all:
        query.append("include=all")
    elif arguments.resolved:
        query.append("include=resolved")
    if arguments.app:
        query.append("app=" + quote(arguments.app))
    body = _get(base, "problems.json" + ("?" + "&".join(query) if query else ""))
    probs = body.get("data") or []
    if arguments.mine:
        me = _agent(arguments).lower()
        probs = [p for p in probs if me in [str(o).lower() for o in (p.get("owners") or [])]
                 or str((p.get("holder") or {}).get("agent") or "").lower() == me]
    return {"lines": [_problem_line(p) for p in probs] or ["(nothing on the queue)"],
            "counts": (body.get("metadata") or {}).get("counts"),
            "data": probs if arguments.json else None}


def _run_resolve(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    pid = _problem_id(arguments.problem_id)
    if not pid:
        raise ValueError("resolve takes a problem id (p-<12 hex>); `errors` lists them")
    return _post(base, "problem/resolve", {"problem": pid, "agent": _agent(arguments),
                                           "note": arguments.note,
                                           "evidence": arguments.evidence or "",
                                           **_console_fields()},
                 extra_headers=_presence_headers(arguments))


def _run_release(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    pid = _problem_id(arguments.item_id)
    if pid:
        return _post(base, "problem/release", {"problem": pid, "agent": _agent(arguments),
                                               **_console_fields()})
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("releasing a TASK needs its lease token (--lease-token or HUB_LEASE_TOKEN)")
    return _post(base, "release", {"id": arguments.item_id, "agent": _agent(arguments),
                                   "token": token})


def _run_escalate(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    pid = _problem_id(arguments.problem_id)
    if not pid:
        raise ValueError("escalate takes a problem id (p-<12 hex>)")
    return _post(base, "problem/escalate", {"problem": pid, "agent": _agent(arguments),
                                            "blocked_on": arguments.blocked_on,
                                            "note": arguments.note or "", **_console_fields()})


def _run_health(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    body = _get(base, "app_health.json")
    lines = []
    for r in body.get("data") or []:
        lines.append("%-8s %s  forwarder=%s ci=%s chat=%s live=%s  open=%s%s" % (
            r.get("verdict"), r.get("slug"), (r.get("forwarder") or {}).get("state"),
            (r.get("ci") or {}).get("state"), (r.get("chat") or {}).get("state"),
            (r.get("live") or {}).get("state"), (r.get("problems") or {}).get("open"),
            ("\n    - " + "\n    - ".join(r.get("gaps") or [])) if r.get("gaps") else ""))
    return {"lines": lines, "counts": (body.get("metadata") or {}).get("counts"),
            "sources_seen": (body.get("metadata") or {}).get("sources_seen")}


def _run_doctor(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"doctor.json?app={quote(arguments.app)}")


def _run_consoles(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Every live console and every crossover pair — the roster, paid for only when asked.
    With --mine, only the signals that concern THIS console (HUB_SESSION_ID), and --seen
    records them as delivered so each is announced once per side."""
    from urllib.parse import quote
    if arguments.mine:
        session = os.environ.get("HUB_SESSION_ID", "")
        query = ("session=" + quote(session)) if session else ("agent=" + quote(_agent(arguments)))
        body = _get(base, "overlap.json?" + query)
        items = body.get("data") or []
        if arguments.seen and items:
            _post(base, "overlap/seen", {"agent": _agent(arguments),
                                         "ids": [it["id"] for it in items if it.get("unseen")]})
        return {"lines": [it.get("title") for it in items] or
                ["no crossover with this console (%s other live console(s))"
                 % max(0, int((body.get("metadata") or {}).get("consoles") or 0) - 1)],
                "data": items}
    return _get(base, "overlap.json")


def _run_ci_event(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    jobs = []
    for item in arguments.job or []:
        name, _, status = item.partition(":")
        jobs.append({"name": name, "status": status or arguments.status})
    payload: dict[str, Any] = {"project": arguments.project, "status": arguments.status,
                               "ref": arguments.ref, "jobs": jobs}
    for name in ("sha", "actor", "source", "details"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if arguments.link:
        payload["url"] = arguments.link
    return _post(base, "ci-event", payload)


# -- Enrollment: a machine's credential, its state, and its environment --

def _run_leave(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Un-enroll THIS machine: the scoped credential revokes itself and the machine's
    presence rows are dropped. --dry-run reports what would happen and changes nothing."""
    # The credential names the seat that leaves; an agent label is sent only when given, so
    # a scoped credential is never refused for a default label that is not its subject.
    payload: dict[str, Any] = {}
    agent = arguments.agent or os.environ.get("HUB_AGENT_ID")
    if agent:
        payload["agent"] = agent
    if arguments.dry_run:
        payload["dry_run"] = True
    return _post(base, "leave", payload, extra_headers=_presence_headers(arguments))


def _run_enroll_status(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    credential = arguments.credential
    if not credential:
        who = _get(base, "whoami.json").get("data") or {}
        credential = (who.get("auth") or {}).get("credential_id") or who.get("credential_id") or ""
    if not credential:
        raise ValueError("pass --credential <id> (or present an agent token so whoami can name it)")
    return _get(base, f"enroll/status.json?credential={quote(credential)}")


def _run_check_env(base: str | None, arguments: argparse.Namespace) -> dict[str, Any]:
    """Is this machine able to work with the hub? Each check prints OK, FIXED (repaired in
    this process) or NEEDS A PERSON, with the reason. --report files every NEEDS A PERSON line
    as this machine's own problem on the board (agent-error, component env), so a machine that
    cannot work is visible without anybody asking it."""
    import platform
    checks = []

    def add(name, state, detail):
        checks.append({"check": name, "state": state, "detail": detail})

    add("python", "OK" if sys.version_info >= (3, 9) else "NEEDS A PERSON",
        "%s (%s)" % (platform.python_version(), sys.executable))
    base_url = os.environ.get("HUB_API_BASE", "").strip()
    add("HUB_API_BASE", "OK" if base_url else "NEEDS A PERSON",
        base_url or "not set: point it at the served hub, e.g. https://app.example/hub")
    try:
        _auth_headers()
        add("credential", "OK", "an agent (or write) token is present in the environment")
    except ValueError as error:
        add("credential", "NEEDS A PERSON", str(error))
    for var, why in (("HUB_AGENT_ID", "names this seat on the board"),
                     ("HUB_MACHINE", "tells pinned deliveries and the device roster which computer this is")):
        add(var, "OK" if os.environ.get(var) else "NEEDS A PERSON",
            os.environ.get(var) or "not set: " + why)
    state = _state_dir()
    existed = os.path.isdir(state)
    try:
        os.makedirs(state, exist_ok=True)
        probe = os.path.join(state, ".write-check")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
        add("client state dir", "OK" if existed else "FIXED",
            state if existed else "created %s (the blind-window record lives here)" % state)
    except OSError as error:
        add("client state dir", "NEEDS A PERSON",
            "%s is not writable (%s): the blind-window record cannot be kept" % (state, error))
    window = _read_window()
    if window.get("opened_at"):
        add("blind window", "OK", "a window opened %s ago is waiting to be reported" %
            _age(time.time() - float(window["opened_at"])))
    reachable = False
    if base_url:
        try:
            _get(_base_url(base_url), "whoami.json", timeout=10)
            reachable = True
            add("hub reachable", "OK", base_url)
        except RuntimeError as error:
            add("hub reachable", "NEEDS A PERSON", str(error)[:200])
    needs = [c for c in checks if c["state"] == "NEEDS A PERSON"]
    reported = 0
    if arguments.report and needs and reachable:
        for c in needs:
            try:
                _post(_base_url(base_url), "agent-error", {
                    "agent": _agent(arguments), "source": "env", "component": "env",
                    "code": "env_" + re.sub(r"[^a-z0-9]+", "_", c["check"].lower()).strip("_"),
                    "severity": "error",
                    "message": "environment check failed: %s - %s" % (c["check"], c["detail"])})
                reported += 1
            except RuntimeError:
                pass
    return {"lines": ["%-15s %-15s %s" % (c["state"], c["check"], c["detail"]) for c in checks],
            "needs_a_person": len(needs), "reported": reported}


# ── The worker LOOP: next -> start -> step -> finish, with compaction-proof regrounding ──
# Extracted from two adopter fleets that each rebuilt this loop independently; the converged
# core belongs to the template. The deployment-specific halves those tools also carried
# (offbox ledger sync, credential minting, transcript capture) stay with their instances.

def _charter_file() -> str:
    return os.environ.get("HUB_CHARTER_FILE") or "CHARTER-CORE.md"


def _charter_sha() -> str | None:
    """sha256 of CHARTER-CORE.md (EOL-normalized), or None when the adopter does not ship one."""
    import hashlib
    try:
        with open(_charter_file(), "rb") as fh:
            return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def _task_local(task_id: str) -> str:
    return str(task_id).rsplit(":", 1)[-1]


def _fetch_task(base: str, task_id: str) -> dict[str, Any]:
    payload = _get(base, f"task/{_task_local(task_id)}.json")
    entity = payload.get("data")
    if not isinstance(entity, dict):
        raise RuntimeError(f"no task entity came back for {task_id}")
    return entity


def _run_reground(base: str | None, arguments: argparse.Namespace) -> dict[str, Any]:
    """Print the charter core and its sha — the re-entry point after context compaction."""
    sha = _charter_sha()
    if sha is None:
        return {"charter_file": _charter_file(), "present": False,
                "note": "no charter core here — the finish regrounding gate is not in force"}
    with open(_charter_file(), "r", encoding="utf-8") as fh:
        body = fh.read()
    return {"charter_file": _charter_file(), "present": True, "sha256": sha,
            "carry": "pass this sha to `finish --charter-sha` — a mismatch there means your "
                     "context drifted past a charter change and you must re-ground first",
            "body": body}


def _run_next(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """DISCOVER: the top ready tasks, with needs-spec and snoozed reported honestly beside
    them. Answers 429 body verbatim at WIP saturation — a refusal, never silence."""
    return _get(base, f"next.json?n={max(1, int(arguments.n))}")


def _run_start(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Claim the task, return its full entity + plan + the charter sha to carry to finish."""
    payload: dict[str, Any] = {"id": arguments.task_id, "agent": _agent(arguments)}
    if arguments.ttl_s is not None:
        payload["ttl_s"] = arguments.ttl_s
    claim = _post(base, "claim", payload, extra_headers=_presence_headers(arguments))
    token = claim.get("token") or (claim.get("data") or {}).get("token") or ""
    entity = _fetch_task(base, arguments.task_id)
    sha = _charter_sha()
    return {"claim": claim, "lease_token": token, "task": entity,
            "charter": ({"sha256": sha, "carry": "pass to `finish --charter-sha`"} if sha else
                        {"present": False,
                         "note": "no charter core here — the finish regrounding gate is not in force"})}


def _run_step(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Mark one plan step done, with the checkpoint note the fleet card surfaces. The write is
    a full-entity upsert under OCC (the fold replaces payloads; a partial write would drop the
    fields it omitted)."""
    import datetime as _dt
    entity = _fetch_task(base, arguments.task_id)
    plan = [dict(s) for s in (entity.get("plan") or []) if isinstance(s, dict)]
    if not plan:
        raise RuntimeError(f"{arguments.task_id} has no plan — record one first (progress on a "
                           f"planless task is invisible to the whole board)")
    target = None
    if arguments.step:
        wanted = arguments.step.strip()
        if wanted.isdigit() and 1 <= int(wanted) <= len(plan):
            target = plan[int(wanted) - 1]
        else:
            target = next((s for s in plan if wanted.lower() in str(s.get("step", "")).lower()), None)
        if target is None:
            raise RuntimeError(f"no plan step matches {wanted!r}")
    else:
        target = next((s for s in plan if not s.get("done")), None)
        if target is None:
            raise RuntimeError("every plan step is already done — use `finish`")
    target["done"] = True
    if arguments.note:
        target["note"] = arguments.note[:600]
        target["note_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # A MINIMAL delta, exactly like the claim seam's own in_progress append: the fold merges
    # payloads last-write-wins per key, so echoing the whole entity back would both trip the
    # status guards and clobber concurrent field changes this client never read.
    body: dict[str, Any] = {"id": entity["id"], "plan": plan, "agent": _agent(arguments),
                            "expected_version": entity.get("version")}
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if token:
        body["token"] = token
    result = _post(base, "task", body, extra_headers=_presence_headers(arguments))
    done = sum(1 for s in plan if s.get("done"))
    return {"updated": result, "step": target.get("step"),
            "progress": f"{done}/{len(plan)}"}


def _run_finish(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Complete the loop's task through the real gate.

    Two disciplines ride this verb beyond a bare `complete`:
    * REGROUNDING GATE — when the adopter ships CHARTER-CORE.md, finish requires the sha the
      worker has carried since `start` and refuses a mismatch: a compacted context that lost or
      outdated its charter is detected here, not discovered later in drifted work.
    * DECLARED PROBE — when the task itself froze a verification_command, finish runs it HERE,
      on the worker, through hub_core.verifier's hardening (argv-form, scrubbed env, exfil
      refusal), and submits the typed receipt. A non-zero exit refuses the completion.
    """
    entity = _fetch_task(base, arguments.task_id)
    current = _charter_sha()
    charter_note = ""
    if current is not None:
        held = (arguments.charter_sha or "").strip().lower()
        if not held:
            raise RuntimeError(
                "this board ships a charter core: pass --charter-sha with the value `start` "
                "gave you (or run `reground` to re-read the charter and retry)")
        if held != current:
            raise RuntimeError(
                "charter drift detected: the sha you hold does not match the current charter "
                "core — your context predates a charter change. Run `reground`, re-read it, "
                "and retry with the current sha.")
        charter_note = f" · charter-core sha256:{current[:12]}"

    payload: dict[str, Any] = {
        "id": arguments.task_id,
        "token": arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN") or "",
        "agent": _agent(arguments),
        "accept_note": arguments.accept_note + charter_note,
        "evidence_uri": arguments.evidence,
    }
    if not payload["token"]:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")

    command = str(entity.get("verification_command") or "").strip()
    receipt = None
    if command:
        import hashlib
        import subprocess
        from . import verifier
        problem = verifier.exfil_problem(command)
        if problem:
            raise RuntimeError(f"the task's declared probe was refused before running: {problem}")
        argv, use_shell = verifier.build_exec(command)
        run = subprocess.run(argv, shell=use_shell, env=verifier.hardened_env(),
                             capture_output=True, timeout=900)
        output = (run.stdout or b"") + (run.stderr or b"")
        receipt = {"command": command, "exit_code": int(run.returncode),
                   "output_sha256": hashlib.sha256(output).hexdigest(),
                   "ran_by": _agent(arguments)}
        if run.returncode != 0:
            return {"refused": "the declared critical probe recorded a non-zero exit — the "
                               "task is not done; fix the work (or the probe) and retry",
                    "verification_run": receipt,
                    "output_tail": output[-2000:].decode("utf-8", errors="replace")}
        payload["verification_run"] = receipt

    result = _post(base, "complete", payload, extra_headers=_presence_headers(arguments))
    return {"completed": result,
            **({"verification_run": receipt} if receipt else
               {"note": "no critical probe was declared — done stands on the real operation"})}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mutate a running Hub through the same HTTP seam that publishes realtime state."
    )
    parser.add_argument("--url", help="served Hub URL; defaults to HUB_API_BASE")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a task on the live board")
    create.add_argument("--title", required=True)
    create.add_argument("--acceptance", required=True)
    create.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P1")
    create.add_argument("--phase")
    create.add_argument("--touch", action="append", default=[])
    create.add_argument("--plan-item", action="append", default=[])
    create.add_argument("--agent")
    create.set_defaults(payload=_payload_create)

    claim = commands.add_parser("claim", help="claim a task (fencing token) or a problem "
                                              "(p-<12 hex>: your console's name on it)")
    claim.add_argument("task_id")
    claim.add_argument("--agent")
    claim.add_argument("--ttl-s", type=int)
    claim.add_argument("--note", help="problem claims: what you are checking")
    claim.add_argument("--take", action="store_true",
                       help="problem claims: displace a live holder (recorded on the claim)")
    claim.set_defaults(payload=_payload_claim)

    heartbeat = commands.add_parser("heartbeat", help="renew a held task lease")
    heartbeat.add_argument("task_id")
    heartbeat.add_argument("--agent")
    heartbeat.add_argument("--lease-token")
    heartbeat.add_argument("--ttl-s", type=int)
    heartbeat.set_defaults(payload=_payload_heartbeat)

    complete = commands.add_parser("complete", help="complete a claimed task with real evidence")
    complete.add_argument("task_id")
    complete.add_argument("--agent")
    complete.add_argument("--lease-token")
    complete.add_argument("--accept-note", required=True)
    complete.add_argument("--evidence", action="append", required=True)
    complete.add_argument("--expected-version", type=int)
    complete.set_defaults(payload=_payload_complete)

    ask = commands.add_parser("ask", help="ask the operator a question that actually DELIVERS")
    ask.add_argument("--agent")
    ask.add_argument("--question", required=True)
    ask.add_argument("--context")
    ask.add_argument("--relates-to", action="append", default=[], dest="relates_to")
    ask.add_argument("--anyway", action="store_true",
                     help="file even though the board already has a matching question")
    ask.set_defaults(payload=_payload_ask)

    answer = commands.add_parser("answer",
                                 help="reply to a question AND retire it (directive:write scope)")
    answer.add_argument("question_id")
    answer.add_argument("--text", required=True)
    answer.add_argument("--crystallize", action="store_true",
                        help="also mint a standing knowledge note (only when the NEXT person "
                             "would otherwise re-derive this; most answers are one-offs)")
    answer.set_defaults(payload=_payload_answer)

    directive = commands.add_parser("directive",
                                    help="issue an operator instruction (directive:write scope)")
    directive.add_argument("--title", required=True)
    directive.add_argument("--body", required=True)
    directive.add_argument("--target", action="append", default=[])
    directive.add_argument("--remediation-cmd", dest="remediation_cmd")
    directive.add_argument("--machine", dest="pin_machine",
                           help="pin delivery to one computer")
    directive.add_argument("--session",
                           help="pin delivery to one LIVE console: its session id or its name")
    directive.set_defaults(payload=_payload_directive)

    ack = commands.add_parser("ack", help="acknowledge any addressed id, routed by its type "
                                          "(directive/answer, crossover, problem, error signature)")
    ack.add_argument("directive_id")
    ack.add_argument("--agent")
    ack.add_argument("--note")
    ack.add_argument("--evidence", help="problem ids: the sha/url that proves the fix")
    ack.set_defaults(runner=_run_ack)

    presence = commands.add_parser("presence",
                                   help="seat heartbeat; sends X-Hub-* headers from env/flags")
    presence.add_argument("--agent")
    presence.add_argument("--machine")
    presence.add_argument("--focus")
    presence.add_argument("--project", help="the project this console works in")
    presence.add_argument("--file", action="append", default=[],
                          help="a file this console just edited (repeatable)")
    presence.add_argument("--name", help="this console's display name")
    presence.add_argument("--unattended", action="store_true",
                          help="an unattended process nobody is reading")
    presence.set_defaults(payload=_payload_presence)

    forget = commands.add_parser("forget-presence",
                                 help="drop a phantom/retired seat row (presence:manage scope)")
    forget.add_argument("--machine")
    forget.add_argument("--target", help="the agent name")
    forget.set_defaults(payload=_payload_forget_presence)

    app_error = commands.add_parser("app-error",
                                    help="forward a satellite service's server failure")
    app_error.add_argument("--app", required=True)
    app_error.add_argument("--message", required=True)
    for name in ("kind", "severity", "code", "details", "component", "operation", "path", "host"):
        app_error.add_argument("--" + name)
    app_error.set_defaults(payload=_payload_app_error)

    agent_error = commands.add_parser("agent-error",
                                      help="report a worker-side operational failure")
    agent_error.add_argument("--message", required=True)
    agent_error.add_argument("--agent")
    for name in ("source", "severity", "code", "details", "machine"):
        agent_error.add_argument("--" + name)
    agent_error.set_defaults(payload=_payload_agent_error)

    ack_error = commands.add_parser("ack-error",
                                    help="claim (or --reopen) one error signature")
    ack_error.add_argument("fingerprint")
    ack_error.add_argument("--agent")
    ack_error.add_argument("--note")
    ack_error.add_argument("--reopen", action="store_true")
    ack_error.set_defaults(payload=_payload_ack_error)

    inbox = commands.add_parser("inbox", help="what is addressed to an agent right now")
    inbox.add_argument("--agent", required=True)
    inbox.add_argument("--ack", help="acknowledge one item by its id (routed by its type)")
    inbox.add_argument("--note")
    inbox.add_argument("--evidence")
    inbox.set_defaults(runner=_run_inbox)

    errors = commands.add_parser("errors", help="the operational queue, folded into PROBLEMS")
    errors.add_argument("--agent")
    errors.add_argument("--mine", action="store_true", help="problems I own or hold")
    errors.add_argument("--app", help="one service's problems")
    errors.add_argument("--all", action="store_true",
                        help="include what the read-time bar holds back (with the reason)")
    errors.add_argument("--resolved", action="store_true", help="include resolved problems")
    errors.add_argument("--trace", metavar="P_ID", help="one problem with its full stored trace")
    errors.add_argument("--json", action="store_true", help="also return the problem objects")
    errors.set_defaults(runner=_run_errors)

    resolve = commands.add_parser("resolve", help="resolve a problem: ack every row behind it "
                                                  "and record the root cause")
    resolve.add_argument("problem_id")
    resolve.add_argument("--agent")
    resolve.add_argument("--note", required=True, help="the ROOT CAUSE")
    resolve.add_argument("--evidence", help="the sha or url that proves the fix")
    resolve.set_defaults(runner=_run_resolve)

    release = commands.add_parser("release", help="hand back a problem (p-id) or a task lease")
    release.add_argument("item_id")
    release.add_argument("--agent")
    release.add_argument("--lease-token", dest="lease_token")
    release.set_defaults(runner=_run_release)

    escalate = commands.add_parser("escalate", help="park a DIAGNOSED problem on the ask or task "
                                                    "it waits for, until that closes")
    escalate.add_argument("problem_id")
    escalate.add_argument("--blocked-on", required=True, dest="blocked_on",
                          help="an open ask (q-...) or task id")
    escalate.add_argument("--agent")
    escalate.add_argument("--note")
    escalate.set_defaults(runner=_run_escalate)

    health = commands.add_parser("health", help="every service: observed / partial / dark / "
                                                "unbuilt, with the gap named")
    health.set_defaults(runner=_run_health)

    doctor = commands.add_parser("doctor", help="one service diagnosed: BLOCKED on unclaimed "
                                                "problems vs WAITING on held ones")
    doctor.add_argument("app")
    doctor.set_defaults(runner=_run_doctor)

    consoles = commands.add_parser("consoles", help="live consoles and crossover pairs")
    consoles.add_argument("--agent")
    consoles.add_argument("--mine", action="store_true",
                          help="only the crossovers that concern this console")
    consoles.add_argument("--seen", action="store_true",
                          help="with --mine: record them as delivered (announced once per side)")
    consoles.set_defaults(runner=_run_consoles)

    ci_event = commands.add_parser("ci-event", help="report one CI pipeline outcome "
                                                    "(the neutral shape a CI adapter posts)")
    ci_event.add_argument("--project", required=True)
    ci_event.add_argument("--status", required=True, choices=("failed", "success"))
    ci_event.add_argument("--ref", default="main")
    ci_event.add_argument("--job", action="append", default=[],
                          help="name[:status] (repeatable); status defaults to --status")
    for name in ("sha", "actor", "source", "details"):
        ci_event.add_argument("--" + name)
    # NOT --url: that is the global flag naming the hub itself.
    ci_event.add_argument("--link", help="the pipeline's own URL")
    ci_event.set_defaults(runner=_run_ci_event)

    leave = commands.add_parser("leave", help="un-enroll THIS machine: its credential revokes "
                                              "itself (--dry-run to preview)")
    leave.add_argument("--agent")
    leave.add_argument("--machine")
    leave.add_argument("--dry-run", action="store_true", dest="dry_run")
    leave.set_defaults(runner=_run_leave)

    enroll_status = commands.add_parser("enroll-status",
                                        help="is a credential active, revoked, or expired?")
    enroll_status.add_argument("--credential")
    enroll_status.set_defaults(runner=_run_enroll_status)

    check_env = commands.add_parser("check-env", help="can this machine work with the hub? "
                                                      "OK / FIXED / NEEDS A PERSON per check")
    check_env.add_argument("--agent")
    check_env.add_argument("--report", action="store_true",
                           help="file each NEEDS A PERSON line as this machine's problem")
    check_env.set_defaults(runner=_run_check_env, local=True)

    wait = commands.add_parser("wait",
                               help="long-poll the inbox; --follow loops and prints arrivals")
    wait.add_argument("--agent", required=True)
    wait.add_argument("--fp", help="last known addressed-set fingerprint")
    wait.add_argument("--wait", type=int, default=25)
    wait.add_argument("--follow", action="store_true")
    wait.set_defaults(runner=_run_wait)

    search = commands.add_parser("search", help="ranked search over the whole board")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(runner=_run_search)

    questions = commands.add_parser("questions",
                                    help="every question with waits, lanes, and reply times")
    questions.set_defaults(runner=_run_questions)

    whoami = commands.add_parser("whoami",
                                 help="what the hub resolves your credential and headers to")
    whoami.set_defaults(runner=_run_whoami)

    # The worker loop: next -> start -> step -> finish (+ reground after compaction).
    nxt = commands.add_parser("next", help="the top ready tasks (needs-spec and snoozed beside them)")
    nxt.add_argument("--n", type=int, default=1)
    nxt.set_defaults(runner=_run_next)

    reground = commands.add_parser("reground",
                                   help="print the charter core + its sha — re-entry after context compaction")
    reground.set_defaults(runner=_run_reground, local=True)

    start = commands.add_parser("start", help="claim a task; returns entity + plan + the charter sha to carry")
    start.add_argument("task_id")
    start.add_argument("--agent")
    start.add_argument("--ttl-s", type=int, dest="ttl_s")
    start.add_argument("--machine")
    start.add_argument("--focus")
    start.set_defaults(runner=_run_start)

    step = commands.add_parser("step", help="mark one plan step done with a checkpoint note")
    step.add_argument("task_id")
    step.add_argument("--agent")
    step.add_argument("--step", help="step text fragment or 1-based index; default = first undone")
    step.add_argument("--note", help="what actually happened at this checkpoint")
    step.add_argument("--lease-token", dest="lease_token",
                      help="the held lease's fencing token (or HUB_LEASE_TOKEN)")
    step.add_argument("--machine")
    step.add_argument("--focus")
    step.set_defaults(runner=_run_step)

    finish = commands.add_parser("finish",
                                 help="complete the loop's task: charter gate + the declared probe (if any) + the real complete()")
    finish.add_argument("task_id")
    finish.add_argument("--agent")
    finish.add_argument("--accept-note", required=True, dest="accept_note")
    finish.add_argument("--evidence", action="append", required=True)
    finish.add_argument("--lease-token", dest="lease_token")
    finish.add_argument("--charter-sha", dest="charter_sha",
                        help="the sha `start`/`reground` gave you; required when the board ships a charter core")
    finish.add_argument("--machine")
    finish.add_argument("--focus")
    finish.set_defaults(runner=_run_finish)
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        base = None if getattr(arguments, "local", False) else _base_url(arguments.url)
        if getattr(arguments, "runner", None):
            result = arguments.runner(base, arguments)
        else:
            operation, payload = arguments.payload(arguments)
            result = _post(base, operation, payload, extra_headers=_presence_headers(arguments))
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    if isinstance(result, dict) and isinstance(result.get("lines"), list):
        # A queue-shaped verb prints its lines for a person; everything else it returned
        # (counts, the objects with --json) follows as JSON for a machine.
        for line in result["lines"]:
            print(line)
        rest = {k: v for k, v in result.items() if k != "lines" and v not in (None, [], {})}
        if rest:
            print(json.dumps(rest, indent=2, sort_keys=True))
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
