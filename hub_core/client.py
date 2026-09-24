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
    request = urllib.request.Request(
        f"{base}/api/{operation}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
    # An unattended run's launcher sets HUB_RESPONDER_HOP; every question the run raises is
    # stamped with it by the process, so the chain bound never depends on the model's memory.
    hop = arguments.hop if arguments.hop is not None else os.environ.get("HUB_RESPONDER_HOP")
    if hop not in (None, "", "0", 0):
        payload["hop"] = int(hop)
    return "ask", payload


def _payload_release(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")
    return "release", {"id": arguments.task_id, "token": token, "agent": _agent(arguments)}


def _payload_hand_back(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    token = arguments.lease_token or os.environ.get("HUB_LEASE_TOKEN")
    if not token:
        raise ValueError("provide --lease-token or set HUB_LEASE_TOKEN")
    return "hand-back", {"id": arguments.task_id, "token": token, "agent": _agent(arguments),
                         "note": arguments.note}


def _journal_lease(task_id: str, result: Any) -> None:
    """Record a granted lease where the launcher that started this process can find it.

    An unattended launcher sets HUB_RUN_LEASES to a file of its own. A run that ends with a task
    still held leaves nobody to release it — the fencing token lived only in the session — so
    the lease would read as work in flight until its TTL. With the token journalled, the
    launcher hands the task back at teardown WITH PROOF (the hub checks the token), never on a
    guess about whose lease it is. Fail-soft: journalling never turns a claim into an error."""
    path = os.environ.get("HUB_RUN_LEASES", "").strip()
    if not path or not isinstance(result, dict):
        return
    token = result.get("token") or (result.get("data") or {}).get("token")
    if not token:
        return
    try:
        import time as _time
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task": task_id, "token": token, "at": _time.time()}) + "\n")
    except OSError:
        pass


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


def _run_inbox(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"inbox.json?agent={quote(arguments.agent)}")


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
    _journal_lease(arguments.task_id, claim)
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
        target = next((s for s in plan if not s.get("done") and not s.get("lifecycle")), None)
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
    work = [s for s in plan if not s.get("lifecycle")]
    done = sum(1 for s in work if s.get("done"))
    return {"updated": result, "step": target.get("step"),
            "progress": f"{done}/{len(work)}"}


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
    ask.add_argument("--hop", type=int,
                     help="escalation depth (default HUB_RESPONDER_HOP; unattended runs only)")
    ask.set_defaults(payload=_payload_ask)

    release = commands.add_parser("release", help="return a held lease (the task stays in progress)")
    release.add_argument("task_id")
    release.add_argument("--agent")
    release.add_argument("--lease-token", dest="lease_token")
    release.set_defaults(payload=_payload_release)

    hand_back = commands.add_parser(
        "hand-back", help="a run ended with its task unfinished: back to todo with ONE self-counting row")
    hand_back.add_argument("task_id")
    hand_back.add_argument("--agent")
    hand_back.add_argument("--lease-token", dest="lease_token")
    hand_back.add_argument("--note", required=True, help="why the run ended with the task unfinished")
    hand_back.set_defaults(payload=_payload_hand_back)

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
    inbox.set_defaults(runner=_run_inbox)

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
            if operation == "claim":
                _journal_lease(payload["id"], result)
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
