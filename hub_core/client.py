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

A claim that stopped being true is retired, never deleted — the reason is appended, dated::

    python -m hub_core.client retire project:gap:0007 --status closed \
      --addressed-by project:task:0042 --note "export now streams; measured 3 s"
    python -m hub_core.client retire "the queue saturates at noon" --type note \
      --note "no longer true after the worker split"

Console chat histories (off unless the hub enables them; see hub_core/histories.py)::

    python -m hub_core.client history-push --agent alice --follow   # workstation uploader
    python -m hub_core.client history --agent alice                 # list consoles
    python -m hub_core.client history --agent alice --session 3f2a  # read one

`presence` is the seat heartbeat between tasks (focus/cwd/machine/session ride HUB_MACHINE,
HUB_SESSION_ID, or flags), and `app-error` / `agent-error` / `ack-error` feed the operational
error stream.

Services to the apps around the hub::

    python -m hub_core.client components                         # hosted UI components
    python -m hub_core.client component-props --app budget-app --set agent.greeting="Ask about budgets"
    python -m hub_core.client app-feed --app budget-app          # one app's slice of the board
    python -m hub_core.client profile --person alice --set theme=dark
    python -m hub_core.client agent-ask --question "..." --person alice --app budget-app

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


def _decode_success(status: int, raw: bytes) -> dict[str, Any]:
    """A successful write's body, or an honest account of why there is none to quote.

    An endpoint that answers 2xx with an empty (or non-JSON) body used to crash the decode or,
    in sibling tools, print a line of Nones -- a success that reads as a non-event, which is
    exactly what invites a re-run and a duplicate write. The hub is the authority; this line
    says only what the transport knows.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {"accepted": True, "status": status,
                "note": "the hub accepted the write and returned no body, so there is no id or "
                        "version to quote -- read the board to see the result; do not re-run it"}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"accepted": True, "status": status,
                "note": "the hub answered 2xx with a body that is not JSON (a proxy page?) -- "
                        "the write may not have reached the hub; read the board before re-running",
                "body_head": text[:200]}


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
            return _decode_success(response.status, response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(detail)
        except json.JSONDecodeError:
            body = detail
        raise HubRefused(error.code, body) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Hub is unreachable at {base}: {error.reason}") from error


class HubRefused(RuntimeError):
    """The hub answered and said no (HTTP 4xx/5xx). Its message is the refusal body, as JSON,
    so the existing callers that print str(error) keep printing the same thing."""

    def __init__(self, status: int, body: Any):
        super().__init__(json.dumps({"status": status, "response": body}))
        self.status = status
        self.body = body

    def codes(self) -> list[str]:
        errors = self.body.get("errors") if isinstance(self.body, dict) else None
        return [str(e.get("code")) for e in (errors or []) if isinstance(e, dict)]


def _evidence_problems(evidence: list[str]) -> list[str]:
    """Evidence is a REFERENCE -- a URL, a commit sha, a path -- so it is one token. Prose
    always contains whitespace and never dereferences; refusing it here puts the error in front
    of the writer, in one edit, instead of after a round trip."""
    return [item for item in evidence if not str(item).strip() or len(str(item).split()) > 1]


def _evidence_help(error: "HubRefused") -> str:
    """Turn an evidence_unresolvable refusal into the exact re-run, item by item."""
    bad, fix = {}, ""
    for entry in ((error.body or {}).get("errors") or []) if isinstance(error.body, dict) else []:
        if isinstance(entry, dict):
            bad.update(entry.get("bad") or {})
            fix = fix or str(entry.get("fix") or "")
    lines = ["  --> ONE unresolvable --evidence item refuses the WHOLE completion: nothing was",
             "      recorded and the task is still in progress."]
    for item, why in list(bad.items())[:6]:
        lines.append("      %s  (%s)" % (item, why))
    if fix:
        lines.append("      " + fix)
    return "\n".join(lines)


def _note_refused_finish(base: str, arguments: argparse.Namespace, error: Exception) -> None:
    """Write a REFUSED completion onto the task, so it is as visible as one that landed.

    A refusal printed to stderr is loud to whoever is at the terminal and invisible to everyone
    else: the board goes on showing an in-progress task with a claim on it and no hint that its
    holder tried to close it and was told no -- precisely what an unattended worker leaves
    behind. Best-effort by construction: the original refusal always propagates unchanged, and a
    failure to annotate is announced rather than swallowed.
    """
    import datetime as _dt
    reason = str(error).replace("\n", " ")
    try:
        entity = _fetch_task(base, arguments.task_id)
        plan = [dict(s) for s in (entity.get("plan") or []) if isinstance(s, dict)]
        plan.append({"step": "finish REFUSED -- task still in progress", "done": False,
                     "note": "the hub refused the completion, so this task is NOT done: " + reason,
                     "note_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        body: dict[str, Any] = {"id": entity["id"], "plan": plan, "agent": _agent(arguments),
                                "expected_version": entity.get("version")}
        token = getattr(arguments, "lease_token", None) or os.environ.get("HUB_LEASE_TOKEN")
        if token:
            body["token"] = token
        _post(base, "task", body, extra_headers=_presence_headers(arguments))
        print("NOTE: the refusal is recorded on %s as an open plan step." % entity["id"],
              file=sys.stderr)
    except Exception as annotate_error:                          # noqa: BLE001
        print("NOTE: could not record the refused finish on the board (%s: %s) -- the task "
              "reads in progress with no reason attached; say why with `step --note`."
              % (type(annotate_error).__name__, str(annotate_error)[:200]), file=sys.stderr)


def _optional_auth_headers() -> dict[str, str]:
    try:
        return _auth_headers()
    except ValueError:
        return {}          # reads are public; whoami simply reports no credential


def _repo_of(path: str) -> str:
    """The repository a directory sits in, named by its top-level folder, or "" when it is
    in none. Read from the filesystem (a .git entry up the tree), never guessed from the
    path's spelling, so a console parked in a workspace root reports no repo at all."""
    try:
        here = os.path.abspath(path)
        while True:
            if os.path.exists(os.path.join(here, ".git")):
                return os.path.basename(here.rstrip("\\/")) or ""
            parent = os.path.dirname(here)
            if parent == here:
                return ""
            here = parent
    except (OSError, ValueError):
        return ""


def _presence_headers(arguments: argparse.Namespace | None = None) -> dict[str, str]:
    """The observed-presence headers every write may carry. Environment first, flags win —
    the board's live-console view is only as true as what the seats send.

    The cwd and the repo are sent as ONE fact: the hub applies the repo that accompanies a
    cwd, and treats a missing repo beside a cwd as "this directory is in no repository".
    HUB_FILES (comma-separated) is the console's recently touched files; the hub stamps it
    on arrival and ages it out, so a stale list never outlives the work it described."""
    cwd = os.environ.get("HUB_CWD") or os.getcwd()
    values = {
        "X-Hub-Machine": os.environ.get("HUB_MACHINE", ""),
        "X-Hub-Session": os.environ.get("HUB_SESSION_ID", ""),
        "X-Hub-Cwd": cwd,
        "X-Hub-Repo": os.environ.get("HUB_REPO") or _repo_of(cwd),
        "X-Hub-Files": os.environ.get("HUB_FILES", ""),
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


def _payload_retire(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments)}
    target = arguments.target
    if arguments.type:
        payload.update({"type": arguments.type, "title": target})
    else:
        payload["id"] = target
    for name in ("status", "note", "superseded_by"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if arguments.addressed_by:
        payload["addressed_by"] = arguments.addressed_by
    return "retire", payload


def _run_history_push(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Upload this workstation's new console turns (see hub_core.transcripts for what is sent)."""
    import time as _time

    from . import transcripts

    agent = _agent(arguments)
    headers = _presence_headers(arguments)

    def post(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _post(base, operation, payload, extra_headers=headers)

    result = transcripts.push(post, agent, force=not arguments.follow)
    if not arguments.follow:
        return result
    while True:                                   # a notifier-style loop; Ctrl-C ends it
        print(json.dumps(result, sort_keys=True), flush=True)
        _time.sleep(max(15, arguments.interval))
        result = transcripts.push(post, agent)


def _run_history(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import urlencode
    query = {key: value for key, value in (("agent", arguments.agent),
                                             ("machine", arguments.machine),
                                             ("session", arguments.session),
                                             ("limit", arguments.limit)) if value}
    return _get(base, "history.json" + ("?" + urlencode(query) if query else ""))


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


# ── Services to the apps around the hub: hosted components, per-app component properties,
# one app's slice of the board, a person's cross-app preferences, the brokered agent ──

def _pairs(items: list[str] | None) -> dict[str, Any]:
    """`key=value` flags into a dict; a value that parses as JSON (a number, a list) is used as
    that JSON, otherwise as the literal string."""
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("expected key=value, got %r" % item)
        key, value = item.split("=", 1)
        try:
            out[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            out[key.strip()] = value
    return out


def _run_components(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "components/")


def _run_component_props(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Read one app's component properties, or REPLACE them with --set component.key=value
    (a full set: properties not named return to their defaults, so current values are read
    first and the named ones laid over them)."""
    from urllib.parse import quote
    current = _get(base, f"components/props/{quote(arguments.app)}.json")
    if not arguments.set:
        return current
    props = {c: dict(v) for c, v in (current.get("props") or {}).items()}
    for dotted, value in _pairs(arguments.set).items():
        if "." not in dotted:
            raise ValueError("name the property as component.key, got %r" % dotted)
        component, key = dotted.split(".", 1)
        props.setdefault(component, {})[key] = value
    return _post(base, "component-props", {"app": arguments.app, "props": props,
                                           "agent": _agent(arguments)})


def _run_app_feed(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import urlencode
    query = {"app": arguments.app}
    if arguments.name:
        query["name"] = arguments.name
    return _get(base, "app-feed.json?" + urlencode(query))


def _run_profile(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    path = "profile?person=" + quote(arguments.person)
    if arguments.set:
        return _post(base, path, {"prefs": _pairs(arguments.set)})
    return _get(base, "api/" + path)


def _run_agent_ask(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"question": arguments.question}
    for name in ("person", "app", "conversation_id"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    return _post(base, "agent/ask", payload)


def _run_agent_history(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import urlencode
    query = {"person": arguments.person, "scope": arguments.scope}
    if arguments.app:
        query["app"] = arguments.app
    return _get(base, "api/agent/history?" + urlencode(query))


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
        # Whole: a checkpoint note is what the next reader acts on, and a silent cut here
        # reported success while dropping the end of it.
        target["note"] = arguments.note
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
    malformed = _evidence_problems(arguments.evidence)
    if malformed:
        raise ValueError("evidence is a reference (a URL, a commit sha, a path), so it is one "
                         "token with no spaces; put prose in --accept-note. Not a reference: "
                         + "; ".join(repr(item) for item in malformed))
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

    try:
        result = _post(base, "complete", payload, extra_headers=_presence_headers(arguments))
    except HubRefused as refusal:
        _note_refused_finish(base, arguments, refusal)
        if "evidence_unresolvable" in refusal.codes():
            raise RuntimeError(str(refusal) + "\n" + _evidence_help(refusal)) from refusal
        raise
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

    retire = commands.add_parser(
        "retire", help="retire or re-open a gap/note/directive/ADR/finding (reason required)")
    retire.add_argument("target", help="the record id, or its exact title together with --type")
    retire.add_argument("--type", help="the record type when TARGET is a title (gap, note, ...)")
    retire.add_argument("--status", help="the new status; each type has a sensible default "
                                          "except gap, which must be named")
    retire.add_argument("--note", help="what retired it — appended with a dated stamp")
    retire.add_argument("--addressed-by", action="append", default=[], dest="addressed_by",
                        help="task id that closed a gap (required for closed/mitigated)")
    retire.add_argument("--superseded-by", dest="superseded_by",
                        help="the id of the record that replaced this one")
    retire.add_argument("--agent")
    retire.set_defaults(payload=_payload_retire)

    history_push = commands.add_parser(
        "history-push", help="upload this workstation's new console turns (history:write scope)")
    history_push.add_argument("--agent")
    history_push.add_argument("--machine")
    history_push.add_argument("--follow", action="store_true",
                              help="keep uploading; at most one upload per interval")
    history_push.add_argument("--interval", type=int, default=60)
    history_push.set_defaults(runner=_run_history_push)

    history = commands.add_parser(
        "history", help="list stored consoles, or read one console's turns (history:read scope)")
    history.add_argument("--agent")
    history.add_argument("--machine")
    history.add_argument("--session")
    history.add_argument("--limit", type=int)
    history.set_defaults(runner=_run_history)

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

    components = commands.add_parser("components",
                                     help="the hosted UI components: versions, files, adopters")
    components.set_defaults(runner=_run_components)

    cprops = commands.add_parser("component-props",
                                 help="one app's component properties; --set comp.key=value replaces them")
    cprops.add_argument("--app", required=True, help="the app slug")
    cprops.add_argument("--set", action="append", metavar="COMPONENT.KEY=VALUE")
    cprops.add_argument("--agent")
    cprops.set_defaults(runner=_run_component_props)

    feed = commands.add_parser("app-feed", help="one app's slice of the board (checklist, announcements)")
    feed.add_argument("--app", required=True)
    feed.add_argument("--name", help="the app's display name, matched as well as the slug")
    feed.set_defaults(runner=_run_app_feed)

    prof = commands.add_parser("profile",
                               help="a person's cross-app preferences; --set key=value merges (profile:write)")
    prof.add_argument("--person", required=True)
    prof.add_argument("--set", action="append", metavar="KEY=VALUE")
    prof.set_defaults(runner=_run_profile)

    ask_agent = commands.add_parser("agent-ask", help="ask the brokered agent (agent:ask)")
    ask_agent.add_argument("--question", required=True)
    ask_agent.add_argument("--person")
    ask_agent.add_argument("--app")
    ask_agent.add_argument("--conversation-id", dest="conversation_id")
    ask_agent.set_defaults(runner=_run_agent_ask)

    agent_hist = commands.add_parser("agent-history", help="a person's past agent conversations (agent:history)")
    agent_hist.add_argument("--person", required=True)
    agent_hist.add_argument("--app")
    agent_hist.add_argument("--scope", choices=["app", "all"], default="app")
    agent_hist.set_defaults(runner=_run_agent_history)

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
            if operation == "complete":
                malformed = _evidence_problems(payload.get("evidence_uri") or [])
                if malformed:
                    raise ValueError("evidence is a reference (a URL, a commit sha, a path), so "
                                     "it is one token with no spaces; put prose in "
                                     "--accept-note. Not a reference: "
                                     + "; ".join(repr(item) for item in malformed))
            try:
                result = _post(base, operation, payload,
                               extra_headers=_presence_headers(arguments))
            except HubRefused as refusal:
                if operation == "complete":
                    _note_refused_finish(base, arguments, refusal)
                    if "evidence_unresolvable" in refusal.codes():
                        raise RuntimeError(str(refusal) + "\n" + _evidence_help(refusal)) \
                            from refusal
                raise
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
