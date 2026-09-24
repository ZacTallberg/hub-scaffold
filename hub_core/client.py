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
HUB_SESSION_ID, or flags), and `app-error` / `agent-error` / `ack-error` feed the operational
error stream.

The worker LOOP rides the same seam — the converged core of two adopter fleets::

    python -m hub_core.client next                       # top ready + needs-spec + snoozed
    python -m hub_core.client start proj:task:0042 --agent worker-1
    python -m hub_core.client step  proj:task:0042 --agent worker-1 --note "schema landed"
    HUB_LEASE_TOKEN=... python -m hub_core.client finish proj:task:0042 --agent worker-1 \
      --accept-note "export live" --evidence https://app.example/export --charter-sha <sha>
    python -m hub_core.client reground                   # after context compaction

Task lifecycle beyond the loop::

    python -m hub_core.client create --title "..." --acceptance "..." --priority P2 --unattended
    python -m hub_core.client create --title "Pick the retention window" --acceptance "..." --decision
    python -m hub_core.client step proj:task:0042 --note "pushed" --sha 3f9c2e1 --pipeline 812
    python -m hub_core.client hand proj:task:0042 --agent worker-1     # back to the unattended queue
    python -m hub_core.client unclaim proj:task:0042 --agent worker-1  # just let go
    python -m hub_core.client recall proj:task:0042                    # state, holder, checkpoints
    python -m hub_core.client decide proj:task:0050 --then file --decision "keep 30 days"
    python -m hub_core.client attention                                # what needs a person, and the fix
    python -m hub_core.client consoles --session 1a2b3c4d              # live consoles + crossovers
    python -m hub_core.client feed billing                             # one project's task feed

Every call sends X-Hub-Client-Version (a digest of this client). The hub shows seats running a
different client than it serves; HUB_CLIENT_SELF_UPDATE=1 lets a stale client fast-forward its
own checkout (rate-limited, detached, fail-soft).

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
_CLIENT_FILES = ("client.py", "checkpoints.py", "verifier.py")
_SEEN = {"hub_client": ""}
CONVERGE_EVERY_S = 15 * 60


def client_version() -> str:
    """A digest of the client this process runs: this module and the modules its verbs import,
    EOL-normalized so a CRLF checkout and an LF server agree on the same commit. The hub computes
    the same digest over the client it serves and names seats that differ."""
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _CLIENT_FILES:
        try:
            digest.update((root / name).read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            pass
    return digest.hexdigest()[:12]


def _telemetry_headers() -> dict[str, str]:
    return {"X-Hub-Client-Version": client_version(), "X-Hub-Client": "hub_core.client"}


def _note_hub_client(response) -> None:
    try:
        current = response.headers.get("X-Hub-Client-Current") or ""
    except Exception:                                        # noqa: BLE001
        current = ""
    if current:
        _SEEN["hub_client"] = current


def _maybe_converge() -> dict[str, Any] | None:
    """Pull a STALE client forward, opt-in (HUB_CLIENT_SELF_UPDATE=1), and never in the way.

    * Only when the hub said which client it serves and ours differs.
    * Rate-limited to one attempt per CONVERGE_EVERY_S, and the stamp is written BEFORE the
      attempt, so a failing update can never become a retry storm on every verb.
    * Only a git checkout, and only `pull --ff-only`: git itself refuses a pull that would
      clobber local work or merge divergent history. A client never rewrites files it did not
      get from its own repository.
    * Detached and fail-soft: the verb the worker ran returns at once; the pull writes its
      outcome to a log next to the stamp."""
    import subprocess
    import time as _time
    from pathlib import Path
    want = _SEEN.get("hub_client") or ""
    if os.environ.get("HUB_CLIENT_SELF_UPDATE") != "1" or not want or want == client_version():
        return None
    state_dir = Path(os.environ.get("HUB_CLIENT_STATE_DIR") or (Path.home() / ".hub-client"))
    stamp = state_dir / "converge.stamp"
    try:
        if _time.time() - float(stamp.read_text(encoding="utf-8")) < CONVERGE_EVERY_S:
            return None
    except (OSError, ValueError):
        pass
    root = Path(__file__).resolve().parent.parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(_time.time()), encoding="utf-8")
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=10)
        if probe.stdout.strip() != "true":
            return {"converge": "skipped", "why": "this client is not a git checkout"}
        log = open(state_dir / "converge.log", "ab")
        flags = 0x00000008 if os.name == "nt" else 0          # DETACHED_PROCESS on Windows
        subprocess.Popen(["git", "-C", str(root), "pull", "--ff-only", "--quiet"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=(os.name != "nt"))
        return {"converge": "started", "from": client_version(), "to": want}
    except Exception as error:                               # noqa: BLE001
        return {"converge": "failed", "why": str(error)[:120]}


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
        **_telemetry_headers(),
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
            _note_hub_client(response)
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        _note_hub_client(error)
        detail = error.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(detail)
        except json.JSONDecodeError:
            body = detail
        raise RuntimeError(json.dumps({"status": error.code, "response": body})) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Hub is unreachable at {base}: {error.reason}") from error


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
        # What the crossover detector and the attended/unattended split read. A supervisor that
        # launches an unattended run sets HUB_SESSION_KIND (and HUB_RUN_ID / HUB_SUBJECT).
        "X-Hub-Project": os.environ.get("HUB_PROJECT", ""),
        "X-Hub-Files": os.environ.get("HUB_FILES", ""),
        "X-Hub-Session-Kind": os.environ.get("HUB_SESSION_KIND", ""),
        "X-Hub-Run": os.environ.get("HUB_RUN_ID", ""),
        "X-Hub-Subject": os.environ.get("HUB_SUBJECT", ""),
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
                 **_optional_auth_headers(), **_telemetry_headers()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _note_hub_client(response)
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
    if arguments.project:
        payload["project"] = arguments.project
    if arguments.decision:
        # A decision is a person's call: it goes to the deciders' inbox, never the unattended lane.
        payload["work_kind"] = "decision"
    elif arguments.work_kind:
        payload["work_kind"] = arguments.work_kind
    if arguments.unattended:
        payload["unattended"] = True
    # ONE KEY PER INTENT: stamped once here and sent on every retry of THIS create, so a retried
    # request (a response that timed out) replays the first record instead of minting a twin.
    import uuid
    payload["idem_key"] = arguments.idem_key or ("create-" + uuid.uuid4().hex)
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
    return "directive", payload


def _payload_ack(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "directive": arguments.directive_id}
    if arguments.note:
        payload["note"] = arguments.note
    return "ack", payload


#: The session digest a supervisor may report with a heartbeat: what it distilled from the
#: session's own activity, and for an unattended run its lifecycle. The hub bounds every field.
_DIGEST_FLAGS = ("phase", "doing", "narration", "last_result", "targets", "kind", "run",
                 "subject", "subject_title", "outcome", "state", "started", "ended", "bounded_s",
                 "project", "files")


def _payload_presence(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments)}
    digest = {name: getattr(arguments, name) for name in _DIGEST_FLAGS
              if getattr(arguments, name, None) not in (None, "")}
    if digest:
        payload["session_state"] = digest
    return "presence", payload


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


_INBOX_HEADINGS = (("decision", "DECISIONS WAITING ON YOU"), ("question", "QUESTIONS"),
                   ("answer", "ANSWERS"), ("directive", "DIRECTIVES"),
                   ("task-stall", "TASK ROT"), ("attention", "NEEDS ATTENTION"),
                   ("overlap", "CROSSOVERS"))


def render_inbox(data: dict[str, Any]) -> str:
    """The addressed set as text a person reads: grouped by kind, each with its reply command."""
    items = data.get("items") or []
    if not items:
        return "Nothing is addressed to you."
    lines = []
    for kind, heading in _INBOX_HEADINGS:
        rows = [i for i in items if i.get("kind") == kind]
        if not rows:
            continue
        lines.append("%s (%d)" % (heading, len(rows)))
        for item in rows:
            lines.append("  - %s" % (item.get("title") or item.get("id")))
            if item.get("reply_cmd"):
                lines.append("      %s" % item["reply_cmd"])
    return "\n".join(lines)


def _run_inbox(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    payload = _get(base, f"inbox.json?agent={quote(arguments.agent)}")
    if getattr(arguments, "text", False):
        return {"text": render_inbox(payload.get("data") or {})}
    return payload


def _payload_create_retry(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """create, retried ONCE on a transport failure with the SAME idempotency key: the hub
    answers the retry with the record the first attempt made (``replayed: true``)."""
    operation, payload = _payload_create(arguments)
    try:
        result = _post(base, operation, payload, extra_headers=_presence_headers(arguments))
    except RuntimeError as error:
        if "unreachable" not in str(error):
            raise
        result = _post(base, operation, payload, extra_headers=_presence_headers(arguments))
    result["idem_key"] = payload["idem_key"]
    return result


def _run_let_go(operation: str):
    def run(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": arguments.task_id, "agent": _agent(arguments)}
        token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
        if token:
            payload["token"] = token
        if arguments.note:
            payload["note"] = arguments.note
        return _post(base, operation, payload, extra_headers=_presence_headers(arguments))
    return run


def _run_recall(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Everything a peer needs to pick up or check a task, from the hub's own joined row:
    state, holder, run, commits, the checkpoints IN ORDER (lifecycle rows labelled), evidence."""
    from . import checkpoints as _cp
    task = _fetch_task(base, arguments.task_id)
    counts = _cp.progress(task)
    trail = []
    for n, s in enumerate(task.get("plan") or [], 1):
        if not isinstance(s, dict):
            continue
        tag = ("scheduler" if _cp.is_lifecycle(s) else
               "placeholder" if _cp.is_placeholder(s) else s.get("kind") or "checkpoint")
        trail.append("%d. [%s] %s %s%s%s" % (
            n, tag, "done" if s.get("done") else "open", s.get("step") or "",
            (" — " + str(s.get("note"))) if s.get("note") else "",
            (" (commit %s)" % s["sha"]) if s.get("sha") else ""))
    return {"id": task.get("id"), "title": task.get("title"), "status": task.get("status"),
            "priority": task.get("priority"), "work_kind": task.get("work_kind"),
            "unattended": bool(task.get("unattended")), "project": task.get("project"),
            "owner": ((task.get("provenance") or {}).get("created_by")
                      or (task.get("provenance") or {}).get("agent")),
            "holder": task.get("holder"), "responder": task.get("responder"),
            "pushed": task.get("pushed"), "deployed": task.get("deployed"),
            "handed_back": task.get("handed_back"), "auto_close": task.get("auto_close"),
            "decision": task.get("decision"),
            "progress": "%d/%d work checkpoints" % (counts["done"], counts["total"]),
            "checkpoints": trail, "evidence": task.get("evidence_uri") or [],
            "acceptance": task.get("acceptance")}


def _run_decide(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _post(base, "task/decide", {"id": arguments.task_id, "decision": arguments.decision,
                                        "then": arguments.then},
                 extra_headers=_presence_headers(arguments))


def _run_attention(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    payload = _get(base, "attention.json")
    data = payload.get("data") or {}
    if getattr(arguments, "mine", None):
        who = arguments.mine.lower()
        data["items"] = [i for i in data.get("items") or [] if i.get("agent") == who]
    return {"verdict": data.get("verdict"), "counts": data.get("counts"),
            "items": [{k: i.get(k) for k in ("severity", "title", "who", "fix", "age_s",
                                              "evidence", "id")}
                      for i in data.get("items") or []],
            "recently_cleared": (data.get("recently_cleared") or [])[:5],
            "sources": data.get("sources")}


def _run_consoles(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    path = "consoles.json"
    if arguments.session:
        path += "?session=" + quote(arguments.session)
    return _get(base, path)


def _run_feed(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"project/{quote(arguments.project)}/tasks.json")


def _run_overlap_seen(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _post(base, "overlap-seen", {"ids": arguments.ids, "agent": _agent(arguments)},
                 extra_headers=_presence_headers(arguments))


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
    claim = _post(base, "claim", payload, extra_headers=_presence_headers(arguments))
    token = claim.get("token") or (claim.get("data") or {}).get("token") or ""
    entity = _fetch_task(base, arguments.task_id)
    sha = _charter_sha()
    return {"claim": claim, "lease_token": token, "task": entity,
            "charter": ({"sha256": sha, "carry": "pass to `finish --charter-sha`"} if sha else
                        {"present": False,
                         "note": "no charter core here — the finish regrounding gate is not in force"})}


def _conflict(error: RuntimeError) -> bool:
    """Is this refusal an optimistic-concurrency race (worth re-reading and re-applying)?"""
    try:
        detail = json.loads(str(error))
    except (TypeError, ValueError):
        return False
    body = detail.get("response") if isinstance(detail, dict) else None
    codes = [e.get("code") for e in ((body or {}).get("errors") or []) if isinstance(e, dict)]
    return detail.get("status") in (409, 428) and bool({"conflict", "precondition_required"} & set(codes))


def _task_write(base: str, task_id: str, mutate, arguments: argparse.Namespace,
                attempts: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the task, apply `mutate(entity) -> delta`, write the delta under OCC -- and on a
    version race, re-read and re-apply (bounded). A plan write that lost a race used to
    dead-letter the checkpoint it carried; re-applying the SAME intent to the fresh record is
    correct because the delta is recomputed from what the record now says, never replayed
    blind. Returns (write_result, delta)."""
    last: RuntimeError | None = None
    for _attempt in range(max(1, attempts)):
        entity = _fetch_task(base, task_id)
        delta = mutate(entity)
        body: dict[str, Any] = {"id": entity["id"], "agent": _agent(arguments),
                                "expected_version": entity.get("version"), **delta}
        token = getattr(arguments, "lease_token", None) or os.environ.get("HUB_LEASE_TOKEN")
        if token:
            body["token"] = token
        try:
            return _post(base, "task", body, extra_headers=_presence_headers(arguments)), delta
        except RuntimeError as error:
            if not _conflict(error):
                raise
            last = error
    raise RuntimeError(f"gave up after {attempts} version races on {task_id}: {last}")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _run_step(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Record one checkpoint: mark a plan step done with the note the board surfaces.

    Targeting: `--step N` (1-based) or a text fragment; default = the first undone WORK step.
    A number past the end GROWS the plan with `auto` placeholders so the report lands where the
    worker numbered it -- placeholders never count toward "N of N done". A planless task, or one
    whose plan is complete, takes a report carrying --note as a NEW checkpoint instead of
    refusing it: progress narrated into a planless task used to be dropped, which made the
    task invisible.

    Structured fields ride the checkpoint so no reader parses prose: `--sha` (+ `--pipeline`,
    `--pipeline-url`) records a `pushed` checkpoint naming the commit; `--kind` sets any other
    schema kind. The write is a minimal delta under OCC, re-applied on a version race."""
    from . import checkpoints as _cp
    kind = arguments.kind or ("pushed" if arguments.sha else "checkpoint")
    picked: dict[str, Any] = {}

    def mutate(entity: dict[str, Any]) -> dict[str, Any]:
        try:
            plan, target = _cp.apply_step(
                entity.get("plan") or [], step=arguments.step, note=arguments.note or "",
                kind=kind, sha=arguments.sha or "", pipeline_id=arguments.pipeline or "",
                pipeline_url=arguments.pipeline_url or "", at=_now_iso())
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        picked.clear()
        picked.update(target)
        return {"plan": plan}

    result, delta = _task_write(base, arguments.task_id, mutate, arguments)
    counts = _cp.progress(delta["plan"])
    out: dict[str, Any] = {"updated": result, "step": picked.get("step"),
                           "kind": picked.get("kind") or "checkpoint",
                           "progress": f"{counts['done']}/{counts['total']}"}
    if counts["lifecycle"] or counts["placeholders"]:
        out["not_counted"] = {"lifecycle": counts["lifecycle"],
                              "placeholders": counts["placeholders"]}
    return out


def _run_plan(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Declare (or extend) a task's checklist: `--steps "a|b|c"`. Steps already on the plan keep
    their done state and notes; new ones are appended in order. A leased task's plan belongs to
    its holder, so writing it needs the lease token -- `--take` claims the task first (and only
    then does `plan` claim), handing back the token to carry."""
    steps = [s.strip() for s in (arguments.steps or "").split("|") if s.strip()]
    if not steps:
        raise ValueError('--steps is required, e.g. --steps "schema|endpoint|board card"')
    claimed: dict[str, Any] = {}
    if arguments.take:
        payload: dict[str, Any] = {"id": arguments.task_id, "agent": _agent(arguments)}
        claimed = _post(base, "claim", payload, extra_headers=_presence_headers(arguments))
        arguments.lease_token = claimed.get("token") or arguments.lease_token

    def mutate(entity: dict[str, Any]) -> dict[str, Any]:
        plan = [dict(s) for s in (entity.get("plan") or []) if isinstance(s, dict)]
        have = {str(s.get("step") or "").strip().lower() for s in plan}
        for step in steps:
            if step.lower() not in have:
                plan.append({"step": step[:200], "done": False})
                have.add(step.lower())
        return {"plan": plan}

    result, delta = _task_write(base, arguments.task_id, mutate, arguments)
    out: dict[str, Any] = {"updated": result, "plan": [s.get("step") for s in delta["plan"]]}
    if claimed:
        out["claim"] = claimed
        out["lease_token"] = claimed.get("token")
    return out


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
    # allow_abbrev=False everywhere: an abbreviated flag that silently resolves to a DIFFERENT
    # option (`--pipe` for `--pipeline-url`) records the wrong field without a word.
    parser = argparse.ArgumentParser(
        description="Mutate a running Hub through the same HTTP seam that publishes realtime state.",
        allow_abbrev=False,
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
    create.add_argument("--project", help="the app/component slug this task is about")
    create.add_argument("--unattended", action="store_true",
                        help="offer it to unattended workers (P0-P2 only; never a decision)")
    create.add_argument("--decision", action="store_true",
                        help="a person's call: delivered to the deciders, never an unattended worker")
    create.add_argument("--work-kind", dest="work_kind")
    create.add_argument("--idem-key", dest="idem_key",
                        help="reuse the key a timed-out create printed to replay, not duplicate")
    create.add_argument("--machine")
    create.add_argument("--focus")
    create.set_defaults(runner=_payload_create_retry)

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
    directive.set_defaults(payload=_payload_directive)

    ack = commands.add_parser("ack", help="record that a directive/answer was delivered to you")
    ack.add_argument("directive_id")
    ack.add_argument("--agent")
    ack.add_argument("--note")
    ack.set_defaults(payload=_payload_ack)

    presence = commands.add_parser("presence",
                                   help="seat heartbeat; sends X-Hub-* headers from env/flags")
    presence.add_argument("--agent")
    presence.add_argument("--machine")
    presence.add_argument("--focus")
    digest = presence.add_argument_group(
        "session digest", "what this session is doing, as a supervisor distilled it; an "
        "unattended run adds its lifecycle (--kind responder|scheduled|autoworker|unattended)")
    digest.add_argument("--phase", help="e.g. reading, editing, testing, waiting")
    digest.add_argument("--doing", help="one line: what it is doing right now")
    digest.add_argument("--narration", help="its own latest words")
    digest.add_argument("--last-result", dest="last_result", help="e.g. 'exit 0' or '12 passed'")
    digest.add_argument("--targets", help="comma list of files/areas it is on")
    digest.add_argument("--kind", help="omit for a person's console")
    digest.add_argument("--run", help="the supervisor's run id")
    digest.add_argument("--subject", help="the task/ask the run was started for")
    digest.add_argument("--subject-title", dest="subject_title")
    digest.add_argument("--state", choices=("working", "waiting", "done", "gone"))
    digest.add_argument("--outcome", help="how a finished run ended (finished, handed back, suspended…)")
    digest.add_argument("--started", type=float, help="epoch seconds the run started")
    digest.add_argument("--ended", type=float, help="epoch seconds the run ended")
    digest.add_argument("--bounded-s", dest="bounded_s", type=float, help="the run's time bound")
    digest.add_argument("--project", help="the project this session is in (crossover detection)")
    digest.add_argument("--files", help="comma list of files edited recently (crossover detection)")
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
    inbox.add_argument("--text", action="store_true",
                       help="grouped for a person: decisions, questions, TASK ROT, attention, crossovers")
    inbox.set_defaults(runner=_run_inbox)

    for verb, operation, helptext in (
            ("hand", "hand", "hand a task back to the queue for an unattended worker"),
            ("unclaim", "unclaim", "let go of a task (also an orphaned console's lease of yours)")):
        let_go = commands.add_parser(verb, help=helptext)
        let_go.add_argument("task_id")
        let_go.add_argument("--agent")
        let_go.add_argument("--note")
        let_go.add_argument("--lease-token", dest="lease_token")
        let_go.add_argument("--machine")
        let_go.add_argument("--focus")
        let_go.set_defaults(runner=_run_let_go(operation))

    recall = commands.add_parser("recall", help="a task's state, holder, commits and checkpoints")
    recall.add_argument("task_id")
    recall.set_defaults(runner=_run_recall)

    decide = commands.add_parser("decide", help="decide a decision task (a person's credential)")
    decide.add_argument("task_id")
    decide.add_argument("--decision", required=True)
    decide.add_argument("--then", required=True, choices=("file", "close", "reply"))
    decide.set_defaults(runner=_run_decide)

    attention = commands.add_parser("attention",
                                    help="what needs a person: owner, exact fix, values, age")
    attention.add_argument("--mine", help="only items owned by this agent")
    attention.set_defaults(runner=_run_attention)

    consoles = commands.add_parser("consoles", help="live consoles and crossovers between them")
    consoles.add_argument("--session", help="only the signals addressed to this console")
    consoles.set_defaults(runner=_run_consoles)

    feed = commands.add_parser("feed", help="one project's annotated task feed")
    feed.add_argument("project")
    feed.set_defaults(runner=_run_feed)

    seen = commands.add_parser("overlap-seen", help="record that crossover signals were delivered")
    seen.add_argument("ids", nargs="+")
    seen.add_argument("--agent")
    seen.set_defaults(runner=_run_overlap_seen)

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

    step = commands.add_parser("step", help="record a checkpoint: mark a plan step done with a note")
    step.add_argument("task_id")
    step.add_argument("--agent")
    step.add_argument("--step", help="step text fragment or 1-based index (grows the plan); "
                                     "default = first undone work step")
    step.add_argument("--note", help="what actually happened at this checkpoint")
    step.add_argument("--sha", help="the commit this checkpoint pushed (records kind=pushed)")
    step.add_argument("--pipeline", help="the numeric id of the pipeline that built --sha")
    step.add_argument("--pipeline-url", dest="pipeline_url")
    step.add_argument("--kind", help="checkpoint kind (default checkpoint, or pushed with --sha)")
    step.add_argument("--lease-token", dest="lease_token",
                      help="the held lease's fencing token (or HUB_LEASE_TOKEN)")
    step.add_argument("--machine")
    step.add_argument("--focus")
    step.set_defaults(runner=_run_step)

    plan = commands.add_parser("plan", help='declare or extend a checklist: --steps "a|b|c"')
    plan.add_argument("task_id")
    plan.add_argument("--steps", required=True)
    plan.add_argument("--agent")
    plan.add_argument("--take", action="store_true",
                      help="claim the task first (the only way plan claims)")
    plan.add_argument("--lease-token", dest="lease_token")
    plan.add_argument("--machine")
    plan.add_argument("--focus")
    plan.set_defaults(runner=_run_plan)

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
    for sub in commands.choices.values():
        sub.allow_abbrev = False
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
    converged = _maybe_converge()
    if converged and isinstance(result, dict):
        result["client_converge"] = converged
    if isinstance(result, dict) and set(result) == {"text"}:
        print(result["text"])
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
