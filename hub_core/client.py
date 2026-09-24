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
CLIENT_VERSION = "1.1"

# ── Transport: several routes to ONE hub, swept breadth-first, failed over only on a route fault ──
#
# A seat's network posture changes underneath it (a VPN captures one route, an overlay network
# another), so HUB_API_BASE may name several comma-separated addresses. They are ROUTES to one
# hub process, never replicas, and three rules follow from that — each paid for on the origin
# system:
#
# * BREADTH FIRST. Giving each address its full retry budget before trying the next let one slow
#   address spend a whole request's patience while a healthy one waited behind it; every write
#   then read "hub unreachable" with a working route second in the list. Every address now gets
#   one attempt before any gets two, and the whole request is bounded by a wall clock, so
#   "unreachable" means every route was tried.
# * FAIL OVER ONLY ON A ROUTE FAILURE. urllib wraps a failure to connect or send (DNS, refused,
#   connect timeout, TLS) in URLError; a failure after the request went out (read timeout, reset)
#   arrives unwrapped, and an HTTP status is the hub answering. Only the first kind moves to the
#   next address: sending a reached-but-slow request to another route only queues a second copy
#   behind the first on a process that is already short of threads — and for a write, the first
#   copy may already have landed.
# * DEMOTE, DON'T FORGET. A route that failed at the transport layer is tried LAST for
#   ROUTE_DEMOTE_S; the address that answered last time is tried first.
#
# Writes get more patience than reads: a read that times out is retried by whoever wanted it,
# a write that times out is a report its sender cannot see was lost.

ROUTE_DEMOTE_S = 1800
ATTEMPTS = 2


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _read_timeout_s() -> int:
    return _env_int("HUB_CLIENT_TIMEOUT_S", 10)


def _write_timeout_s() -> int:
    return _env_int("HUB_CLIENT_WRITE_TIMEOUT_S", 30)


def _budget_s() -> int:
    return _env_int("HUB_CLIENT_TOTAL_BUDGET_S", 90)


def _state_dir():
    """Per-seat client state (route health, the offline queue). Resolved through the Python
    user profile, never a shell variable: on some workstations the shell's home is a network
    share that is slow or unmapped."""
    from pathlib import Path
    return Path(os.environ.get("HUB_CLIENT_HOME") or os.path.expanduser("~/.hub-client"))


def _normalize_base(raw: str) -> str:
    raw = (raw or "").strip().rstrip("/")
    if raw.endswith("/api"):
        raw = raw[:-4]
    return raw


def _base_urls(value: str | None) -> list[str]:
    """Every configured route, best first: the last one that answered, then the rest in the
    order given, then any route demoted by a recent transport failure."""
    raw = value or os.environ.get("HUB_API_BASE") or ""
    bases: list[str] = []
    for part in raw.replace(";", ",").split(","):
        base = _normalize_base(part)
        if base and base not in bases:
            bases.append(base)
    if not bases:
        raise ValueError("set HUB_API_BASE to the served Hub URL, for example https://app.example/hub "
                         "(several comma-separated routes to the same hub are allowed)")
    import time as _time
    health = _route_health()
    now = _time.time()
    try:
        last = (_state_dir() / "last-good-url").read_text(encoding="utf-8").strip()
    except OSError:
        last = ""

    def rank(base: str) -> tuple[int, int]:
        demoted = now - float(health.get(base) or 0) < ROUTE_DEMOTE_S
        return (2 if demoted else 0 if base == last else 1, bases.index(base))

    return sorted(bases, key=rank)


def _base_url(value: str | None) -> str:
    """The best route now (kept for callers that need one base, e.g. to print it)."""
    return _base_urls(value)[0]


def _route_health() -> dict:
    try:
        data = json.loads((_state_dir() / "url-health.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mark_route(base: str, failed: bool) -> None:
    """Record a route's transport outcome. Never raises: bookkeeping must not fail a request."""
    import time as _time
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        health = _route_health()
        if failed:
            health[base] = _time.time()
        else:
            if base not in health:
                try:
                    if (directory / "last-good-url").read_text(encoding="utf-8").strip() == base:
                        return                    # the common case costs one read, no write
                except OSError:
                    pass
            health.pop(base, None)
            (directory / "last-good-url").write_text(base, encoding="utf-8")
        tmp = directory / ("url-health.%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(health), encoding="utf-8")
        os.replace(tmp, directory / "url-health.json")
    except OSError:
        pass


class HubRefused(RuntimeError):
    """The hub ANSWERED with a refusal. The message keeps the historical JSON shape."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(json.dumps({"status": status, "response": body}))


class HubUnreachable(RuntimeError):
    """No answer. `reached` says whether the request may have arrived (a read timeout or a 5xx
    from the hub/its proxy) — for a write that means the outcome is UNKNOWN, not failed."""

    def __init__(self, message: str, *, reached: bool, bases: list[str]):
        self.reached = reached
        self.bases = bases
        hint = ("" if reached else
                " -- every configured route failed to connect. If you hold another route to the "
                "same hub (e.g. a tunnel you opened yourself), set HUB_API_BASE to it for this "
                "shell only; the token is sent to whatever answers there.")
        super().__init__(f"Hub is unreachable at {', '.join(bases)}: {message}{hint}")


def _client_id() -> str:
    """version+sha of the running client file: the telemetry a board uses to tell a seat that
    runs the kit from a bare caller that merely set a machine header."""
    import hashlib
    try:
        with open(__file__, "rb") as fh:
            return "%s+%s" % (CLIENT_VERSION,
                              hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()[:16])
    except OSError:
        return CLIENT_VERSION


def _request(bases: list[str], method: str, path: str, *, data: bytes | None,
             headers: dict[str, str], timeout: int | None = None) -> dict[str, Any]:
    import socket
    import time as _time
    per_try = timeout or (_write_timeout_s() if method != "GET" else _read_timeout_s())
    deadline = _time.monotonic() + max(_budget_s(), per_try)
    last: HubUnreachable | None = None
    for attempt in range(ATTEMPTS):
        for base in bases:
            if _time.monotonic() >= deadline and last is not None:
                raise last
            request = urllib.request.Request(f"{base}/{path.lstrip('/')}", data=data,
                                             headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=per_try) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                _mark_route(base, failed=False)
                return payload
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:8000]
                if error.code >= 500:
                    # The hub (or the proxy speaking for it) answered 5xx. Another route ends at
                    # the same process, so it would only repeat the answer; the backed-off second
                    # sweep may retry a read.
                    last = HubUnreachable(f"HTTP {error.code}", reached=True, bases=bases)
                    break
                _mark_route(base, failed=False)   # it answered: this route is right
                try:
                    body: Any = json.loads(detail)
                except json.JSONDecodeError:
                    body = detail
                raise HubRefused(error.code, body) from error
            except urllib.error.URLError as error:
                _mark_route(base, failed=True)    # a ROUTE failure: demote it, try the next now
                last = HubUnreachable(str(error.reason)[:200], reached=False, bases=bases)
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as error:
                # REACHED: the request went out and the answer did not come back. No second
                # route in this sweep, and never a resend of a write — it may have landed.
                last = HubUnreachable(f"{type(error).__name__}: {str(error)[:160]}",
                                      reached=True, bases=bases)
                break
        if last is not None and last.reached and method != "GET":
            raise last
        if attempt + 1 < ATTEMPTS and _time.monotonic() < deadline:
            _time.sleep(1 + attempt)          # once per sweep, never between two routes
    assert last is not None
    raise last


def _auth_headers() -> dict[str, str]:
    agent_token = os.environ.get("HUB_AGENT_TOKEN", "").strip()
    if agent_token:
        return {"X-Agent-Token": agent_token}
    write_token = os.environ.get("HUB_WRITE_TOKEN", "").strip()
    if write_token:
        return {"X-Write-Token": write_token}
    raise ValueError("set HUB_AGENT_TOKEN (preferred) or HUB_WRITE_TOKEN in the process environment")


def _common_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        # Some production edges reject Python urllib's default signature before the request can
        # reach Hub authentication. Keep a stable browser-compatible identity while allowing an
        # adopter to name its own operational client at the edge.
        "User-Agent": os.environ.get("HUB_CLIENT_USER_AGENT", DEFAULT_USER_AGENT),
        "X-Hub-Client": _client_id(),
    }


def _bases_of(base: Any) -> list[str]:
    return list(base) if isinstance(base, (list, tuple)) else [str(base)]


def _post(base: Any, operation: str, payload: dict[str, Any],
          extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **_common_headers(), **_auth_headers(),
               **_artifact_headers(), **(extra_headers or {})}
    return _request(_bases_of(base), "POST", f"api/{operation}",
                    data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers=headers)


def _post_versioned(base: Any, operation: str, payload: dict[str, Any],
                    extra_headers: dict[str, str] | None = None,
                    rebuild=None) -> dict[str, Any]:
    """POST an entity update under optimistic concurrency, surviving ONE lost race.

    A version miss answers 428 (no expected_version on an existing entity) or 409 conflict,
    and both bodies NAME the current version. That is the single most retryable failure there
    is — two writers one version apart — so it is retried once: with `rebuild(current)` when
    the payload was computed from the entity (recompute it from a fresh read, never replay a
    stale delta), else with the reported version. A second miss is a live race and is raised;
    looping on it turns a refusal into a flood. A 409 that names no current version (a lease or
    content refusal) is not a version race and is raised at once."""
    try:
        return _post(base, operation, payload, extra_headers)
    except HubRefused as refusal:
        if refusal.status not in (409, 428):
            raise
        current = None
        body = refusal.body if isinstance(refusal.body, dict) else {}
        for err in body.get("errors") or []:
            if isinstance(err, dict) and err.get("current") is not None:
                current = err["current"]
                break
        if current is None:
            raise
        retry = rebuild(current) if rebuild else dict(payload, expected_version=current)
        return _post(base, operation, retry, extra_headers)


def _optional_auth_headers() -> dict[str, str]:
    try:
        return _auth_headers()
    except ValueError:
        return {}          # reads are public; whoami simply reports no credential


# Set only while REPLAYING a queued write: the identity of the console that queued it.
_REPLAY_ORIGIN: dict[str, str] | None = None


def _presence_headers(arguments: argparse.Namespace | None = None) -> dict[str, str]:
    """The observed-presence headers every write may carry. Environment first, flags win —
    the board's live-console view is only as true as what the seats send."""
    if _REPLAY_ORIGIN:
        # A REPLAYED write speaks as the console that QUEUED it, identity only: a lease is held
        # by a console, and the location headers would move that console's presence row to
        # wherever the flushing console happens to stand.
        return {name: value for name, value in (
            ("X-Hub-Machine", _REPLAY_ORIGIN.get("machine", "")),
            ("X-Hub-Session", _REPLAY_ORIGIN.get("session", ""))) if value}
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
    headers = {name: value for name, value in values.items() if value}
    if arguments is not None and getattr(arguments, "files", None) is not None:
        # A CURRENT claim: `--files a b` replaces this console's list, a bare `--files` sends an
        # empty header ("nothing edited recently") and clears it, omitting it keeps the last one.
        headers["X-Hub-Files"] = ",".join(arguments.files)[:1800]
    return headers


def _artifact_headers() -> dict[str, str]:
    """X-Hub-Artifacts: `name=sha16,...` for every artifact this seat is running, so the board's
    distribution view can grade it against what the hub publishes: always the client itself and
    the charter core (when present), plus adopter-declared files named in HUB_ARTIFACTS as
    `name=path,...`. Hashes are over LF-normalized bytes, the same form the hub publishes, so a
    checkout with CRLF line endings is not reported as drift. Never raises."""
    import hashlib
    pairs = {"client": _client_id().rsplit("+", 1)[-1]}
    sha = _charter_sha()
    if sha:
        pairs["charter"] = sha[:16]
    for spec in (os.environ.get("HUB_ARTIFACTS") or "").split(","):
        name, _, path = spec.partition("=")
        name, path = name.strip(), path.strip()
        if not name or not path:
            continue
        try:
            with open(os.path.expanduser(path), "rb") as fh:
                pairs[name] = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()[:16]
        except OSError:
            continue
    return {"X-Hub-Artifacts": ",".join(f"{k}={v}" for k, v in sorted(pairs.items()))}


def _get(base: Any, path: str, timeout: int | None = None) -> dict[str, Any]:
    return _request(_bases_of(base), "GET", path, data=None,
                    headers={**_common_headers(), **_optional_auth_headers()}, timeout=timeout)


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


def _run_inbox(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"inbox.json?agent={quote(arguments.agent)}")


def _run_wait(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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


def _run_search(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"search.json?q={quote(arguments.query)}&limit={arguments.limit}")


def _run_questions(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "questions.json")


def _run_whoami(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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


def _run_reground(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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


def _run_next(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    """DISCOVER: the top ready tasks, with needs-spec and snoozed reported honestly beside
    them. Answers 429 body verbatim at WIP saturation — a refusal, never silence."""
    return _get(base, f"next.json?n={max(1, int(arguments.n))}")


def _run_start(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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


def _run_step(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    """Mark one plan step done, with the checkpoint note the fleet card surfaces. The write is
    a minimal delta under OCC; a lost version race re-reads the task and recomputes the plan
    once (never replays a plan computed from a stale read)."""
    import datetime as _dt

    def build(entity: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list]:
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
        return body, target, plan

    body, target, plan = build(_fetch_task(base, arguments.task_id))
    rebuilt: dict[str, Any] = {}

    def rebuild(_current: Any) -> dict[str, Any]:
        fresh_body, fresh_target, fresh_plan = build(_fetch_task(base, arguments.task_id))
        rebuilt.update(target=fresh_target, plan=fresh_plan)
        return fresh_body

    result = _post_versioned(base, "task", body, extra_headers=_presence_headers(arguments),
                             rebuild=rebuild)
    target = rebuilt.get("target", target)
    plan = rebuilt.get("plan", plan)
    done = sum(1 for s in plan if s.get("done"))
    return {"updated": result, "step": target.get("step"),
            "progress": f"{done}/{len(plan)}",
            **({"retried": "a concurrent write moved the task; the step was recomputed from a "
                           "fresh read and written once more"} if rebuilt else {})}


RECORD_TYPES = ("gap", "feat", "note", "adr", "decision", "capability")


def _run_record(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    """Create or AMEND a gap/feat/note/adr/decision/capability through its versioned upsert.

    An amend of an existing id must carry expected_version; the version is read first, and a
    lost race (409/428 naming the current version) is retried once through `_post_versioned`."""
    try:
        fields: dict[str, Any] = json.loads(arguments.json) if arguments.json else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"--json is not valid JSON: {error}") from error
    if not isinstance(fields, dict):
        raise ValueError("--json must be a JSON object")
    for pair in arguments.set or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set expects key=value, got {pair!r}")
        fields[key.strip()] = value
    fields.setdefault("agent", _agent(arguments))
    if arguments.id:
        fields["id"] = arguments.id

    def with_version(current: Any) -> dict[str, Any]:
        return dict(fields, expected_version=current)

    if arguments.id:
        try:
            entity = _get(base, f"{arguments.type}/{_task_local(arguments.id)}.json").get("data")
        except HubRefused as refusal:
            if refusal.status != 404:
                raise
            entity = None
        if isinstance(entity, dict) and entity.get("version") is not None:
            fields["expected_version"] = entity["version"]
    return _post_versioned(base, arguments.type, fields,
                           extra_headers=_presence_headers(arguments), rebuild=with_version)


def _run_distribution(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "distribution.json")


def _run_built(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, "built.json" + (f"?person={quote(arguments.person)}" if arguments.person else ""))


def _run_ci_events(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import urlencode
    query = {k: v for k, v in (("pipeline", arguments.pipeline), ("job", arguments.job),
                               ("project", arguments.project), ("limit", arguments.limit)) if v}
    return _get(base, "ci-events.json?" + urlencode(query))


def _run_ci_report(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    """Report one CI/deploy result from a pipeline step. Authenticated by the CI webhook secret
    (HUB_CI_WEBHOOK_SECRET), not an agent credential: the sender is the pipeline. A deploy step
    that restored the previous commit reports --status rolled_back --restored-sha <sha>, the one
    outcome a success-only release record can never carry."""
    secret = os.environ.get("HUB_CI_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise ValueError("set HUB_CI_WEBHOOK_SECRET in the environment (the hub's CI webhook secret)")
    payload = {k: v for k, v in (
        ("kind", arguments.kind), ("project", arguments.project), ("status", arguments.status),
        ("ref", arguments.ref), ("sha", arguments.sha), ("job", arguments.job),
        ("pipeline", arguments.pipeline), ("trigger", arguments.trigger), ("url", arguments.link),
        ("restored_sha", arguments.restored_sha), ("failure_reason", arguments.reason)) if v}
    if arguments.jobs:
        payload["jobs"] = [{"name": n, "status": st} for n, _, st in
                           (j.partition("=") for j in arguments.jobs)]
    headers = {"Content-Type": "application/json", **_common_headers(),
               "X-Hub-Webhook-Token": secret}
    return _request(_bases_of(base), "POST", "api/ci-event",
                    data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers=headers)


def _run_finish(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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


# ── The offline queue: a write that could not reach the hub is kept, replayed in order, and
#    replayed AS the console that queued it ──
#
# Three outcomes for a write, not two: landed, refused, and QUEUED — and a queued write is not a
# success (it may still be refused when it replays), so it exits QUEUED_EXIT, distinguishable at
# the call site, which is the only place the caller is still paying attention. Only a ROUTE
# failure queues: a write whose request reached the hub and timed out may already have landed,
# so it is reported as an unknown outcome (OUTCOME_UNKNOWN_EXIT) and never resent blindly.
#
# The rules, each paid for on the origin system:
# * STAMP WHO QUEUED IT. The queue file is per seat, but several consoles share a seat, and a
#   task lease is held by a console. A replay under the flushing console's identity is refused
#   by the very console that wrote it. Each entry carries origin {agent, machine, session}; a
#   replay sends that identity and nothing about where the flusher stands.
# * REMOVE WHAT THIS RUN CONSUMED, BY IDENTITY. Two consoles drain concurrently (every write verb
#   drains first). A drain that asserts "the head is unchanged" fails under a concurrent drain,
#   keeps what it already sent, and re-sends it forever. Entries carry an id; a drain removes
#   exactly the ids it consumed, so an entry another drain took is simply absent and anything
#   appended meanwhile stays.
# * THE QUEUE CAN NEVER WEDGE. A permanent refusal, or any unexpected failure while replaying, is
#   dead-lettered and the drain continues; only an unreachable hub stops it (order is the
#   contract). `flush --dead` replays dead letters as their queuer and ARCHIVES what is refused
#   again; `flush --dead-archive` archives one handled by hand. Nothing is ever deleted: a
#   warning that rides every invocation and names nothing to run teaches people to read past it.

QUEUED_EXIT = 4
OUTCOME_UNKNOWN_EXIT = 5
QUEUEABLE = {"create", "complete", "ask", "answer", "directive", "ack", "app-error",
             "agent-error", "ack-error", "step", "record"}


def _queue_path():
    return _state_dir() / "queue.jsonl"


def _dead_path():
    return _state_dir() / "queue.dead.jsonl"


def _dead_archive_path():
    return _state_dir() / "queue.dead.archive.jsonl"


def _queue_lock():
    from .process_lock import ProcessFileLock
    _state_dir().mkdir(parents=True, exist_ok=True)
    return ProcessFileLock(_state_dir(), name=".queue.lock", timeout=10)


def _jsonl_load(path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                row = {"raw": line, "dead_error": "corrupt line"}
            if isinstance(row, dict):
                out.append(row)
    return out


def _jsonl_write(path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        try:
            path.unlink()
        except OSError:
            pass
        return
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
                   encoding="utf-8")
    os.replace(tmp, path)


def _jsonl_append(path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _serializable_args(arguments: argparse.Namespace) -> dict[str, Any]:
    out = {}
    for key, value in vars(arguments).items():
        if key in ("payload", "runner") or callable(value):
            continue
        if isinstance(value, (str, int, float, bool, list, type(None))):
            out[key] = value
    return out


def _queue_append(arguments: argparse.Namespace, reason: str) -> dict[str, Any]:
    import datetime as _dt
    import uuid
    entry = {"id": uuid.uuid4().hex, "verb": arguments.command,
             "args": _serializable_args(arguments),
             "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
             "reason": reason[:300],
             "origin": {"agent": _agent(arguments) if hasattr(arguments, "agent") else "",
                        "machine": getattr(arguments, "machine", None) or os.environ.get("HUB_MACHINE", ""),
                        "session": os.environ.get("HUB_SESSION_ID", "")}}
    with _queue_lock():
        _jsonl_append(_queue_path(), entry)
    return entry


def _dispatch(bases: list[str], arguments: argparse.Namespace) -> dict[str, Any]:
    if getattr(arguments, "runner", None):
        return arguments.runner(bases, arguments)
    operation, payload = arguments.payload(arguments)
    return _post(bases, operation, payload, extra_headers=_presence_headers(arguments))


def _replay(bases: list[str], entry: dict[str, Any]) -> dict[str, Any]:
    """Re-run a queued verb exactly as it was invoked, speaking as the console that queued it."""
    global _REPLAY_ORIGIN
    parser = _parser()
    choices = parser._subparsers._group_actions[0].choices          # noqa: SLF001
    sub = choices.get(entry.get("verb") or "")
    if sub is None:
        raise ValueError(f"verb {entry.get('verb')!r} is not known to this client")
    arguments = argparse.Namespace(**{key: sub.get_default(key)
                                      for key in ("payload", "runner", "local")})
    for key, value in (entry.get("args") or {}).items():
        setattr(arguments, key, value)
    arguments.command = entry.get("verb")
    origin = entry.get("origin") or {}
    _REPLAY_ORIGIN = {k: str(origin.get(k) or "") for k in ("machine", "session")} \
        if origin.get("session") or origin.get("machine") else None
    try:
        return _dispatch(bases, arguments)
    finally:
        _REPLAY_ORIGIN = None


def flush_queue(bases: list[str], verbose: bool = True) -> tuple[int, int]:
    """Replay queued writes in order. Returns (sent, still_queued). Stops at the first
    unreachable hub; dead-letters refusals and continues."""
    with _queue_lock():
        entries = _jsonl_load(_queue_path())
    if not entries:
        return 0, 0
    consumed: set[str] = set()
    sent = 0
    for entry in entries:
        label = f"{entry.get('verb')} {(entry.get('args') or {}).get('task_id') or ''}".strip()
        try:
            _replay(bases, entry)
            sent += 1
            consumed.add(entry.get("id") or "")
            if verbose:
                print(f"flushed {label} (queued {entry.get('ts', '?')})", file=sys.stderr)
        except HubUnreachable as error:
            if error.reached and entry.get("verb") in QUEUEABLE:
                # It may have landed; do not resend it blindly. Dead-letter with the reason so
                # a person reads the board and then replays or archives it deliberately.
                _jsonl_append(_dead_path(), {**entry, "dead_error": "outcome unknown: " + str(error)[:300]})
                consumed.add(entry.get("id") or "")
                continue
            break
        except HubRefused as error:
            _jsonl_append(_dead_path(), {**entry, "dead_error": str(error)[:400]})
            consumed.add(entry.get("id") or "")
            if verbose:
                print(f"DEAD-LETTER {label}: {str(error)[:200]}", file=sys.stderr)
        except Exception as error:                          # noqa: BLE001 - THE FLOOR
            _jsonl_append(_dead_path(), {**entry, "dead_error":
                                         f"unhandled while replaying: {type(error).__name__}: {error}"[:400]})
            consumed.add(entry.get("id") or "")
    with _queue_lock():
        current = [e for e in _jsonl_load(_queue_path()) if (e.get("id") or "") not in consumed]
        _jsonl_write(_queue_path(), current)
    return sent, len(current)


def flush_dead(bases: list[str] | None, only: str | None, archive_only: bool) -> dict[str, Any]:
    """Replay dead letters as the consoles that queued them, or archive them (never delete)."""
    import datetime as _dt
    with _queue_lock():
        entries = _jsonl_load(_dead_path())
    keep, replayed, archived, stopped = [], [], [], None

    def archive(entry, why):
        _jsonl_append(_dead_archive_path(), {**entry, "archived": why[:400],
                                             "archived_ts": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        archived.append(entry.get("id"))

    for index, entry in enumerate(entries):
        task = str((entry.get("args") or {}).get("task_id") or "")
        if only and only != "*" and only not in (task, task.rsplit(":", 1)[-1], entry.get("id")):
            keep.append(entry)
            continue
        if archive_only or "verb" not in entry:
            archive(entry, "archived by operator" if "verb" in entry else "nothing to replay")
            continue
        try:
            _replay(bases or [], entry)
            replayed.append(entry.get("id"))
        except HubUnreachable as error:
            if error.reached:
                archive(entry, "outcome unknown on replay: " + str(error)[:300])
                continue
            stopped = index
            break
        except HubRefused as error:
            archive(entry, "refused again on replay: " + str(error)[:300])
        except Exception as error:                          # noqa: BLE001
            archive(entry, f"failed on replay: {type(error).__name__}: {error}")
    if stopped is not None:
        keep += entries[stopped:]
    with _queue_lock():
        # Anything dead-lettered while this ran is preserved: merge by id, never overwrite.
        processed = {e.get("id") for e in entries} - {e.get("id") for e in keep}
        current = [e for e in _jsonl_load(_dead_path()) if e.get("id") not in processed]
        _jsonl_write(_dead_path(), current)
    return {"replayed": len(replayed), "archived": len(archived), "remaining": len(current),
            "archive": str(_dead_archive_path()) if archived else None,
            **({"stopped": "the hub is unreachable; run flush --dead again"} if stopped is not None else {})}


def _run_flush(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
    bases = _bases_of(base) if base else None
    if arguments.dead is not None or arguments.dead_archive is not None:
        chosen = arguments.dead_archive if arguments.dead_archive is not None else arguments.dead
        if arguments.dead_archive is None and not bases:
            raise ValueError("set HUB_API_BASE to replay dead letters")
        return flush_dead(bases, chosen, archive_only=arguments.dead_archive is not None)
    if not bases:
        raise ValueError("set HUB_API_BASE to flush the queue")
    sent, remaining = flush_queue(bases)
    dead = len(_jsonl_load(_dead_path()))
    return {"sent": sent, "still_queued": remaining, "dead_letters": dead,
            "queue": str(_queue_path())}


def _warn_dead() -> None:
    dead = _jsonl_load(_dead_path())
    if dead:
        print(f"WARNING: {len(dead)} dead-lettered write(s) in {_dead_path()} are NOT on the board. "
              f"`flush --dead [ID]` replays each as the console that queued it and archives what "
              f"is refused again; `flush --dead-archive [ID]` archives one you handled by hand.",
              file=sys.stderr)


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
    presence.add_argument("--files", nargs="*", default=None,
                          help="files this console edited recently, as <project>/<path>")
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

    record = commands.add_parser("record",
                                 help="create or amend a gap/feat/note/adr/decision/capability "
                                      "(versioned upsert; a lost race is retried once)")
    record.add_argument("type", choices=RECORD_TYPES)
    record.add_argument("--id", help="amend this entity (its version is read first)")
    record.add_argument("--json", help="the entity fields as a JSON object")
    record.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    record.add_argument("--agent")
    record.set_defaults(runner=_run_record)

    flush = commands.add_parser("flush",
                                help="send queued writes now; --dead replays dead letters as the "
                                     "console that queued them, --dead-archive archives them")
    flush.add_argument("--dead", nargs="?", const="*", metavar="ID",
                       help="replay dead letters (all, or one task/entry id)")
    flush.add_argument("--dead-archive", dest="dead_archive", nargs="?", const="*", metavar="ID",
                       help="archive dead letters without replaying (never deletes)")

    distribution = commands.add_parser("distribution",
                                       help="is every seat running what this hub publishes? "
                                            "(offline seats are named, never graded as drift)")
    distribution.set_defaults(runner=_run_distribution)

    built = commands.add_parser("built", help="what each person has built, derived from the ledger")
    built.add_argument("--person")
    built.set_defaults(runner=_run_built)

    ci_report = commands.add_parser("ci-report",
                                    help="report a CI/deploy result (webhook secret, not an agent "
                                         "credential); a failure becomes a board row, a green "
                                         "retires it")
    ci_report.add_argument("--kind", choices=("pipeline", "job", "deploy"), required=True)
    ci_report.add_argument("--project", required=True)
    ci_report.add_argument("--status", required=True,
                           help="success | failed | rolled_back (deploy) | running ...")
    ci_report.add_argument("--ref")
    ci_report.add_argument("--sha")
    ci_report.add_argument("--job")
    ci_report.add_argument("--pipeline")
    ci_report.add_argument("--trigger", help="push, schedule, api, ... (a non-push run is named)")
    ci_report.add_argument("--link", help="URL of the run")
    ci_report.add_argument("--restored-sha", dest="restored_sha")
    ci_report.add_argument("--reason")
    ci_report.add_argument("--jobs", nargs="*", default=[], metavar="NAME=STATUS",
                           help="a pipeline's jobs and how each ended")
    ci_report.set_defaults(runner=_run_ci_report)

    ci_events = commands.add_parser("ci-events",
                                    help="the raw CI deliveries behind a CI row (needs ci:read)")
    ci_events.add_argument("--pipeline")
    ci_events.add_argument("--job")
    ci_events.add_argument("--project")
    ci_events.add_argument("--limit", type=int)
    ci_events.set_defaults(runner=_run_ci_events)
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        local = getattr(arguments, "local", False)
        bases = None if local else _base_urls(arguments.url)
        if arguments.command == "flush":
            try:
                bases = _base_urls(arguments.url)
            except ValueError:
                bases = None                       # archiving needs no hub
            result = _run_flush(bases, arguments)
        elif arguments.command in QUEUEABLE:
            # Older writes first, so the ledger keeps event order. The queue existing must never
            # stop new work being recorded, so a failing drain only warns.
            try:
                _, still_queued = flush_queue(bases)
            except Exception as error:                   # noqa: BLE001
                print(f"WARNING: could not replay the offline queue ({type(error).__name__}: "
                      f"{error}); continuing with this write.", file=sys.stderr)
                still_queued = 0
            if still_queued:
                _queue_append(arguments, "queued behind older unsent writes")
                print(f"NOT ON THE BOARD YET -- queued locally behind {still_queued} older write(s): "
                      f"{arguments.command}. It is sent on the next invocation (or `flush`) and MAY "
                      f"still be refused. Do not report this as done.", file=sys.stderr)
                _warn_dead()
                return QUEUED_EXIT
            try:
                result = _dispatch(bases, arguments)
            except HubUnreachable as error:
                if error.reached:
                    print(f"OUTCOME UNKNOWN -- the hub received this {arguments.command} and did not "
                          f"answer in time; it may have landed. Read the board before resending. "
                          f"({error})", file=sys.stderr)
                    return OUTCOME_UNKNOWN_EXIT
                _queue_append(arguments, str(error))
                print(f"NOT ON THE BOARD YET -- queued locally: {arguments.command}. It is sent on "
                      f"the next invocation (or `flush`) and MAY still be refused. Do not report "
                      f"this as done. ({error})", file=sys.stderr)
                return QUEUED_EXIT
            _warn_dead()
        else:
            result = _dispatch(bases, arguments)
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
