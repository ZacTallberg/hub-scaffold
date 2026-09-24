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
      --text "the retry queue; requeue stalled items"         # needs ask:answer
    python -m hub_core.client ack project:directive:0001 --agent worker-1 --revision 1
    python -m hub_core.client msg bob --note "schema landed; your import can start"
    python -m hub_core.client inbox --agent bob --ack m-alice-0a1b2c3d   # retire a message
    python -m hub_core.client receipts --undelivered          # offered, never acknowledged

`ask --to <agent>` addresses one agent; an unaddressed ask is the operator's; any ask unanswered
past HUB_ASK_UNSTICK_S reaches every console. Set HUB_SESSION_ID so mail and answers come back
to the console that asked.

`update --note "..." --evidence <sha|url>` posts one first-person line to the agents' feed; under
HUB_AUTOWORKER=1 the answer/ack/finish verbs post their own line automatically.

`presence` is the seat heartbeat between tasks (focus/cwd/machine/session ride HUB_MACHINE,
HUB_SESSION_ID, or flags), and `app-error` / `agent-error` / `ack-error` feed the operational
error stream.

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
import sys
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
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # BACK-PRESSURE IS RETRIED, NOTHING ELSE. A 503 whose code says the write never happened
    # (the ledger lock was busy, a sidecar feed lost its append) is safe to repeat by
    # construction; any other failure is returned to the caller unchanged, because a retry that
    # reports failure for a write that landed is worse than the failure it was avoiding.
    for attempt in range(_BUSY_ATTEMPTS):
        request = urllib.request.Request(
            f"{base}/api/{operation}", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                body: Any = json.loads(detail)
            except json.JSONDecodeError:
                body = detail
            if error.code == 503 and attempt < _BUSY_ATTEMPTS - 1 and _retryable(body):
                import time as _time
                try:
                    delay = float(error.headers.get("Retry-After") or 2)
                except (TypeError, ValueError):
                    delay = 2.0
                _time.sleep(max(0.5, min(delay, 10.0)) * (attempt + 1))
                continue
            raise RuntimeError(json.dumps({"status": error.code, "response": body})) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Hub is unreachable at {base}: {error.reason}") from error
    raise RuntimeError(f"Hub stayed busy at {base} after {_BUSY_ATTEMPTS} attempts")


_BUSY_ATTEMPTS = 3
_RETRYABLE_CODES = {"busy", "update_write_failed"}


def _retryable(body: Any) -> bool:
    """True only for a refusal that states nothing was written."""
    try:
        return any((e or {}).get("code") in _RETRYABLE_CODES for e in body.get("errors") or [])
    except AttributeError:
        return False


def _optional_auth_headers() -> dict[str, str]:
    try:
        return _auth_headers()
    except ValueError:
        return {}          # reads are public; whoami simply reports no credential


def _repo_remote(start: str) -> str:
    """The origin remote of the repository containing `start`, read from .git/config — no
    subprocess on the hot path. '' when there is none."""
    import configparser
    from pathlib import Path
    try:
        here = Path(start).resolve()
    except OSError:
        return ""
    for folder in (here, *here.parents):
        git = folder / ".git"
        config = git / "config"
        if git.is_file():                     # a worktree: "gitdir: <path>"
            try:
                target = git.read_text(encoding="utf-8").split(":", 1)[1].strip()
                common = (Path(target) / "commondir")
                base = Path(target) / (common.read_text(encoding="utf-8").strip()
                                       if common.is_file() else ".")
                config = base.resolve() / "config"
            except (OSError, IndexError):
                return ""
        if config.is_file():
            parser = configparser.ConfigParser(strict=False)
            try:
                parser.read(config, encoding="utf-8")
                return parser.get('remote "origin"', "url", fallback="")
            except (configparser.Error, OSError):
                return ""
    return ""


def _presence_headers(arguments: argparse.Namespace | None = None) -> dict[str, str]:
    """The observed-presence headers every write may carry. Environment first, flags win —
    the board's live-console view is only as true as what the seats send. The console's name,
    repository and runtime ride along so the roster can bind it to a project; the repository
    is read from .git/config (never a subprocess) unless HUB_REPO says otherwise."""
    cwd = os.environ.get("HUB_CWD") or os.getcwd()
    values = {
        "X-Hub-Machine": os.environ.get("HUB_MACHINE", ""),
        "X-Hub-Session": os.environ.get("HUB_SESSION_ID", ""),
        "X-Hub-Cwd": cwd,
        "X-Hub-Focus": os.environ.get("HUB_FOCUS", ""),
        "X-Hub-Console": os.environ.get("HUB_CONSOLE_NAME", ""),
        "X-Hub-Repo": os.environ.get("HUB_REPO") or _repo_remote(cwd),
        "X-Hub-App": os.environ.get("HUB_APP", ""),
        "X-Hub-Runtime": os.environ.get("HUB_RUNTIME", ""),
    }
    if arguments is not None:
        if getattr(arguments, "machine", None):
            values["X-Hub-Machine"] = arguments.machine
        if getattr(arguments, "focus", None):
            values["X-Hub-Focus"] = arguments.focus
    return {name: value for name, value in values.items() if value}


def _get(base: str, path: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base}/{path.lstrip('/')}",
        headers={"Accept": "application/json",
                 "User-Agent": os.environ.get("HUB_CLIENT_USER_AGENT", DEFAULT_USER_AGENT),
                 **_optional_auth_headers()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(detail)
        except json.JSONDecodeError:
            body = detail
        raise RuntimeError(json.dumps({"status": error.code, "response": body})) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Hub is unreachable at {base}: {error.reason}") from error


def _agent(arguments: argparse.Namespace) -> str:
    return getattr(arguments, "agent", None) or os.environ.get("HUB_AGENT_ID") or "agent"


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


def _payload_claim(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"id": arguments.task_id, "agent": _agent(arguments)}
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
    if getattr(arguments, "to", None):
        payload["to"] = arguments.to
    if getattr(arguments, "human_only", False):
        payload["human_only"] = True
    return "ask", payload


def _payload_answer(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"question": arguments.question_id, "text": arguments.text}
    if arguments.crystallize:
        payload["crystallize"] = True
    if getattr(arguments, "disclose", False):
        payload["disclose"] = True
    return "answer", payload


def _run_tier(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Read every agent's visibility tier, or set one (credential:manage scope)."""
    if arguments.set is None:
        return _get(base, "tiers.json")
    return _post(base, "tier", {"target": arguments.target, "tier": arguments.set})


def _run_veil_audit(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Render every veiled route as a contributor and list any hidden term that got through."""
    return _get(base, "veil-audit.json", timeout=120)


def _run_perf(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Per-route latency (worst process window), the slow-route verdict and the snapshot's
    per-phase timings; --profile runs one profiled snapshot build (perf:profile scope)."""
    return _get(base, "perf.json" + ("?profile=snapshot" if arguments.profile else ""),
                timeout=120)


def _payload_directive(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": arguments.title, "body_md": arguments.body}
    if arguments.target:
        payload["targets"] = arguments.target
    if arguments.remediation_cmd:
        payload["remediation_cmd"] = arguments.remediation_cmd
    return "directive", payload


def _payload_ack(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "directive": arguments.directive_id}
    if arguments.note:
        payload["note"] = arguments.note
    if arguments.revision is not None:
        payload["delivery_revision"] = arguments.revision
    return "ack", payload


def _payload_msg(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Agent-to-agent mail. `--session` addresses one of the recipient's consoles (the id the
    board and `consoles` show); your own console rides X-Hub-Session so a reply comes back
    to THIS window."""
    payload: dict[str, Any] = {"agent": _agent(arguments), "to": arguments.to,
                               "note": arguments.note}
    for name in ("title", "session", "machine"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    return "message", payload


def _payload_update(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """One first-person line on the agents' feed. `--evidence` MUST reach the row: a feed post
    without the sha/url behind it is the one thing feed evidence exists to prevent."""
    payload: dict[str, Any] = {"agent": _agent(arguments), "summary": arguments.note,
                               "kind": arguments.kind}
    if arguments.evidence:
        payload["evidence"] = arguments.evidence
    if arguments.item:
        payload["item"] = arguments.item
    payload["by"] = "autoworker" if _autoworker() else "human"
    return "agent-update", payload


def _autoworker() -> bool:
    return os.environ.get("HUB_AUTOWORKER", "").strip().lower() in ("1", "true", "yes", "on")


def _auto_update(base: str, arguments: argparse.Namespace, kind: str, summary: str,
                 evidence: str = "", item: str = "") -> dict[str, Any] | None:
    """The emit is MECHANICAL for unattended agents, so one cannot forget to narrate: under
    HUB_AUTOWORKER=1 the answer/ack/finish verbs post their own feed line. An interactive person
    running the same verb does not flood the feed. Fail-soft: narration never fails the verb."""
    if not _autoworker():
        return None
    try:
        return _post(base, "agent-update",
                     {"agent": _agent(arguments), "kind": kind, "summary": summary[:4000],
                      "evidence": evidence, "item": item, "by": "autoworker"},
                     extra_headers=_presence_headers(arguments))
    except (RuntimeError, ValueError) as error:
        return {"feed_error": str(error)[:300]}


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


def _reader_query(arguments: argparse.Namespace) -> str:
    """?agent=&session=&machine= for inbox reads: a console that names itself receives its own
    mail, plus mail for a console of this agent that has since ended."""
    from urllib.parse import urlencode
    query = {"agent": arguments.agent}
    session = getattr(arguments, "session", None) or os.environ.get("HUB_SESSION_ID", "")
    machine = getattr(arguments, "machine", None) or os.environ.get("HUB_MACHINE", "")
    if session:
        query["session"] = session
    if machine:
        query["machine"] = machine
    return urlencode(query)


def _run_inbox(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """What is addressed to you now. `--ack <id>` retires one delivered MESSAGE instead (an
    answer or directive is acked with `ack`, naming its delivery revision)."""
    if getattr(arguments, "ack", None):
        payload = {"agent": arguments.agent, "id": arguments.ack}
        if getattr(arguments, "via", None):
            payload["via"] = arguments.via
        return _post(base, "message/ack", payload, extra_headers=_presence_headers(arguments))
    return _get(base, f"inbox.json?{_reader_query(arguments)}")


def _run_receipts(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """The notification lifecycle: one item's thread (--ref), the offers nobody acknowledged
    (--undelivered), or the newest receipts."""
    from urllib.parse import urlencode
    query: dict[str, Any] = {}
    if arguments.ref:
        query["ref"] = arguments.ref
    elif arguments.undelivered:
        query["undelivered"] = 1
        if arguments.kind:
            query["kind"] = arguments.kind
    else:
        query["limit"] = arguments.limit
        if arguments.stage:
            query["stage"] = arguments.stage
        if arguments.agent:
            query["agent"] = arguments.agent
    return _get(base, "receipts.json?" + urlencode(query))


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
                f"inbox/wait?{_reader_query(arguments)}&fp={quote(fingerprint)}"
                f"&wait={arguments.wait}",
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


def _run_release(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Hand a held task back (the lease only; the task stays on the board) and retract the
    focus `start` declared for it."""
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")
    headers = _presence_headers(arguments)
    try:
        title = str(_fetch_task(base, arguments.task_id).get("title") or "")
    except RuntimeError:
        title = ""
    if title:
        headers["X-Hub-Focus-Retract"] = title[:180]
    return _post(base, "release", {"id": arguments.task_id, "token": token,
                                   "agent": _agent(arguments)}, extra_headers=headers)


def _run_consoles(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Every live console on the board — project, state, focus, files, and the task THAT
    console holds — with this console marked and the projects it is in without a task."""
    from urllib.parse import urlencode
    query: dict[str, str] = {}
    if arguments.agent:
        query["agent"] = arguments.agent
    session = os.environ.get("HUB_SESSION_ID", "")
    if session:
        query["session"] = session
    payload = _get(base, "activity.json" + ("?" + urlencode(query) if query else ""))
    machine = os.environ.get("HUB_MACHINE", "").strip().lower()
    for row in payload.get("data") or []:
        if session and str(row.get("session") or "") == session[:8]:
            row["this_console"] = True
        elif machine and str(row.get("machine") or "").lower() == machine:
            row["this_machine"] = True
    nudge = (payload.get("metadata") or {}).get("no_task_for") or []
    if nudge:
        payload["nudge"] = ("you hold no task for %s — `start <task>` or `create` one so this "
                            "work is visible on the board" % ", ".join(nudge))
    return payload


def _context_state_path(session: str):
    from pathlib import Path
    root = Path(os.environ.get("HUB_CONTEXT_STATE_DIR")
                or Path(os.path.expanduser("~")) / ".hub-context")
    safe = "".join(c for c in (session or "default") if c.isalnum() or c in "._-")[:64]
    return root / ((safe or "default") + ".json")


def _run_prompt_context(base: str | None, arguments: argparse.Namespace) -> dict[str, Any]:
    """What a per-prompt hook should inject — THREE channels, fingerprinted SEPARATELY, because
    they change at three different rates.

    Folding standing doctrine, slow-moving reference and live board lines into one string under
    one hash makes the cheapest half of the payload hostage to the most volatile: the live lines
    move every prompt, so the hash moves every prompt, and the unchanged doctrine is re-sent
    (and re-billed) every time. Here:

      doctrine  the charter core (+ HUB_DOCTRINE_FILE): emitted when its text changes, on
                SessionStart (which also covers resume and post-compaction), or with --force;
                otherwise SILENT — the session already holds it
      live      what is addressed to this agent/console now (inbox): re-sent whenever it moves
      nudge     the projects this console is in without a task: re-sent whenever it moves

    The per-session state file records the hash and time each channel was last emitted — the
    receipt that says what this session was actually told. The text to inject is `context`."""
    import hashlib
    import time as _time
    session = arguments.session or os.environ.get("HUB_SESSION_ID", "") or "default"
    state_path = _context_state_path(session)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    force = arguments.force or arguments.event.lower() in ("sessionstart", "session-start")
    channels: dict[str, str] = {}

    doctrine_parts = []
    for path in [_charter_file()] + [p for p in (os.environ.get("HUB_DOCTRINE_FILE") or "").split(
            os.pathsep) if p]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                doctrine_parts.append(handle.read().strip())
        except OSError:
            continue
    channels["doctrine"] = "\n\n".join(p for p in doctrine_parts if p)

    live_lines, nudge = [], ""
    agent = _agent(arguments)
    if base:
        try:
            from urllib.parse import urlencode
            query = {"agent": agent}
            if session != "default":
                query["session"] = session
            inbox = _get(base, "inbox.json?" + urlencode(query), timeout=8)
            from .inbox import render_line
            for item in (inbox.get("data") or {}).get("items") or []:
                live_lines.append("- " + render_line(item) + "  [" + str(item.get("id")) + "]")
            if session != "default":
                activity = _get(base, "activity.json?" + urlencode({"session": session}), timeout=8)
                gap = (activity.get("metadata") or {}).get("no_task_for") or []
                if gap:
                    nudge = ("You hold no task for %s — `start <task>` or `create` one so this "
                             "work is visible on the board." % ", ".join(gap))
        except (RuntimeError, ValueError):
            live_lines.append("- (the hub did not answer; addressed items are unknown this prompt)")
    channels["live"] = ("Addressed to you now:\n" + "\n".join(live_lines)) if live_lines else ""
    channels["nudge"] = nudge

    emitted, parts = {}, []
    now = _time.time()
    for name in ("doctrine", "live", "nudge"):
        text = channels[name]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        prior = (state.get(name) or {}).get("sha")
        send = bool(text) and (digest != prior or (name == "doctrine" and force))
        if send:
            parts.append(text)
            state[name] = {"sha": digest, "emitted_at": now, "chars": len(text)}
        elif not text and prior:
            state[name] = {"sha": digest, "emitted_at": now, "chars": 0}   # cleared
        emitted[name] = {"sent": send, "chars": len(text) if send else 0, "sha": digest}
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        # If the receipt cannot be written the next prompt cannot know what this one said, so
        # it must say everything again: drop the doctrine hash rather than go silent on a guess.
        pass
    context = "\n\n".join(parts)
    if arguments.text:
        print(context)
        raise SystemExit(0)
    return {"context": context, "channels": emitted, "session": session,
            "chars": len(context)}


def _run_search(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"search.json?q={quote(arguments.query)}&limit={arguments.limit}")


def _run_questions(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "questions.json")


def _run_whoami(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "whoami.json")


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
    headers = _presence_headers(arguments)
    # START PUBLISHES WHAT, NEVER WHICH: the console's focus becomes the task's title (a bare id
    # tells a reader of the roster nothing), unless the caller declared a focus of its own.
    if not getattr(arguments, "focus", None):
        try:
            title = str(_fetch_task(base, arguments.task_id).get("title") or "")
        except RuntimeError:
            title = ""
        if title:
            headers["X-Hub-Focus"] = title[:180]
    claim = _post(base, "claim", payload, extra_headers=headers)
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

    headers = _presence_headers(arguments)
    if entity.get("title"):
        # Retract the focus `start` declared — only if it is still that one; a newer focus set
        # by another verb is never cleared by this.
        headers["X-Hub-Focus-Retract"] = str(entity["title"])[:180]
    result = _post(base, "complete", payload, extra_headers=headers)
    feed = _auto_update(base, arguments, "fixed",
                        f"Finished {entity.get('title') or arguments.task_id}: {arguments.accept_note}",
                        evidence=(arguments.evidence or [""])[0], item=arguments.task_id)
    return {"completed": result, **({"feed": feed} if feed else {}),
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

    claim = commands.add_parser("claim", help="claim a task and receive its fencing token")
    claim.add_argument("task_id")
    claim.add_argument("--agent")
    claim.add_argument("--ttl-s", type=int)
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
    ask.add_argument("--to", help="address one agent (default: the operator); an ask unanswered "
                                  "past HUB_ASK_UNSTICK_S reaches every console regardless")
    ask.add_argument("--human-only", action="store_true", dest="human_only",
                     help="only a person can satisfy this (an approval, a physical step): "
                          "delivered as a gate, never widened to every console")
    ask.set_defaults(payload=_payload_ask)

    answer = commands.add_parser("answer",
                                 help="reply to a question AND retire it (ask:answer scope)")
    answer.add_argument("question_id")
    answer.add_argument("--text", required=True)
    answer.add_argument("--crystallize", action="store_true",
                        help="also mint a standing knowledge note (only when the NEXT person "
                             "would otherwise re-derive this; most answers are one-offs)")
    answer.add_argument("--disclose", action="store_true",
                        help="send a reply that names a facet the asker's tier cannot see, and "
                             "record the disclosure (veil:disclose scope)")
    answer.set_defaults(payload=_payload_answer)

    tier = commands.add_parser("tier", help="read tiers, or set one agent's visibility tier")
    tier.add_argument("target", nargs="?", help="the agent")
    tier.add_argument("--set", choices=("operator", "member", "contributor", ""),
                      help="the tier to set ('' clears it)")
    tier.set_defaults(runner=_run_tier)

    veil_audit = commands.add_parser(
        "veil-audit", help="render every veiled route as a contributor; list any leaked term")
    veil_audit.set_defaults(runner=_run_veil_audit)

    perf = commands.add_parser("perf", help="route latency, slow routes, snapshot phase timings")
    perf.add_argument("--profile", action="store_true",
                      help="profile one snapshot build (perf:profile scope)")
    perf.set_defaults(runner=_run_perf)

    directive = commands.add_parser("directive",
                                    help="issue an operator instruction (directive:write scope)")
    directive.add_argument("--title", required=True)
    directive.add_argument("--body", required=True)
    directive.add_argument("--target", action="append", default=[])
    directive.add_argument("--remediation-cmd", dest="remediation_cmd")
    directive.set_defaults(payload=_payload_directive)

    ack = commands.add_parser("ack", help="record that a directive/answer was delivered to you")
    ack.add_argument("directive_id")
    ack.add_argument("--agent")
    ack.add_argument("--note")
    ack.add_argument("--revision", type=int,
                     help="the delivery_revision you read (required when the answer carries one)")
    ack.set_defaults(payload=_payload_ack)

    msg = commands.add_parser("msg", help="send mail to another agent (delivered into its inbox)")
    msg.add_argument("to")
    msg.add_argument("--note", required=True)
    msg.add_argument("--title")
    msg.add_argument("--session", help="the recipient console id (from `consoles`)")
    msg.add_argument("--machine", help="pin delivery to one of the recipient's machines")
    msg.add_argument("--agent")
    msg.set_defaults(payload=_payload_msg)

    update = commands.add_parser("update",
                                 help="post one first-person line to the agents' updates feed")
    update.add_argument("--note", required=True, help="what you did, in your own words")
    update.add_argument("--evidence", help="the sha / URL / path that proves it")
    update.add_argument("--kind", choices=("fixed", "answered", "acked", "shipped", "escalated",
                                           "noop"), default="fixed")
    update.add_argument("--item", help="the board id this narrates (task, question, error)")
    update.add_argument("--agent")
    update.add_argument("--machine")
    update.set_defaults(payload=_payload_update)

    presence = commands.add_parser("presence",
                                   help="seat heartbeat; sends X-Hub-* headers from env/flags")
    presence.add_argument("--agent")
    presence.add_argument("--machine")
    presence.add_argument("--focus")
    presence.set_defaults(payload=_payload_presence)

    focus = commands.add_parser("focus",
                                help="say what THIS console is on, in your own words")
    focus.add_argument("focus", help="a sentence, not an id")
    focus.add_argument("--agent")
    focus.add_argument("--machine")
    focus.set_defaults(payload=_payload_presence)

    consoles = commands.add_parser("consoles",
                                   help="every live console: project, focus, and the task it holds")
    consoles.add_argument("--agent", help="only this agent's consoles")
    consoles.set_defaults(runner=_run_consoles)

    release = commands.add_parser("release", help="hand a held task's lease back")
    release.add_argument("task_id")
    release.add_argument("--agent")
    release.add_argument("--lease-token", dest="lease_token")
    release.add_argument("--machine")
    release.set_defaults(runner=_run_release)

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
    inbox.add_argument("--session", help="this console's id (default HUB_SESSION_ID)")
    inbox.add_argument("--machine", help="this machine (default HUB_MACHINE)")
    inbox.add_argument("--ack", help="retire one delivered message by id")
    inbox.add_argument("--via", help="how it was delivered (recorded on the receipt)")
    inbox.set_defaults(runner=_run_inbox)

    receipts = commands.add_parser("receipts",
                                   help="notification lifecycle: offered/delivered/failed/resolved")
    receipts.add_argument("--ref", help="one item's thread")
    receipts.add_argument("--undelivered", action="store_true",
                          help="offers nobody acknowledged (last 24 h)")
    receipts.add_argument("--kind", help="narrow --undelivered (message, answer, directive)")
    receipts.add_argument("--stage", choices=("offered", "delivered", "failed", "resolved"))
    receipts.add_argument("--agent")
    receipts.add_argument("--limit", type=int, default=50)
    receipts.set_defaults(runner=_run_receipts)

    wait = commands.add_parser("wait",
                               help="long-poll the inbox; --follow loops and prints arrivals")
    wait.add_argument("--agent", required=True)
    wait.add_argument("--session", help="this console's id (default HUB_SESSION_ID)")
    wait.add_argument("--machine", help="this machine (default HUB_MACHINE)")
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

    prompt_context = commands.add_parser(
        "prompt-context",
        help="per-prompt hook payload: doctrine only when it changed, live items when they move")
    prompt_context.add_argument("--event", default="UserPromptSubmit",
                                help="the hook event; SessionStart always re-sends the doctrine")
    prompt_context.add_argument("--session", help="this console's id (default HUB_SESSION_ID)")
    prompt_context.add_argument("--agent")
    prompt_context.add_argument("--force", action="store_true", help="re-send the doctrine")
    prompt_context.add_argument("--text", action="store_true",
                                help="print only the text to inject (for a hook's stdout)")
    prompt_context.set_defaults(runner=_run_prompt_context, optional_base=True)

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
        if getattr(arguments, "local", False):
            base = None
        elif getattr(arguments, "optional_base", False):
            # A hook must still emit the local doctrine when no hub is configured.
            try:
                base = _base_url(arguments.url)
            except ValueError:
                base = None
        else:
            base = _base_url(arguments.url)
        if getattr(arguments, "runner", None):
            result = arguments.runner(base, arguments)
        else:
            operation, payload = arguments.payload(arguments)
            result = _post(base, operation, payload, extra_headers=_presence_headers(arguments))
            if operation == "answer":
                feed = _auto_update(base, arguments, "answered",
                                    "Answered %s: %s" % (arguments.question_id, arguments.text),
                                    item=arguments.question_id)
            elif operation == "ack":
                feed = _auto_update(base, arguments, "acked",
                                    "Acknowledged %s%s" % (arguments.directive_id,
                                                           (": " + arguments.note) if arguments.note else ""),
                                    item=arguments.directive_id)
            else:
                feed = None
            if feed:
                result = {**result, "feed": feed}
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
