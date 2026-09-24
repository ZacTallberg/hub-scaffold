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
HUB_SESSION_ID, or flags), `app-error` / `agent-error` / `ack-error` feed the operational
error stream, and `errors --include deferred|all` reads the raw stream back with the queue's own
counts first (what is on the queue, what is unclaimed, and what the bar held back). `ci-failure` posts a failed CI job's log tail; the hub classifies it (rollback /
real / not_deployed / unclear) by what the LOG says, never by the pipeline's trigger. `deploy`
records a verified release and survives a cold hub (growing timeouts, retried only because the
record is idempotent by sha). `components` and `capability` read and register the standard
components and app skeletons a new app starts from.

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

Knowledge rides it too (hub_core/client_knowledge.py): `share` a lesson, record a `finding`,
`method`, `review` or `gap`, `recall` one record in full, and wire `prompt-context --hook` into an
agent harness's prompt hook so the board's knowledge, ranked for what the console is doing,
arrives before each prompt.

Services to the apps around the hub::

    python -m hub_core.client components                         # hosted UI components
    python -m hub_core.client component-props --app budget-app --set agent.greeting="Ask about budgets"
    python -m hub_core.client app-feed --app budget-app          # one app's slice of the board
    python -m hub_core.client profile --person alice --set theme=dark
    python -m hub_core.client profile --person alice --app budget-app --set ui=110   # one app only
    python -m hub_core.client profile --person alice --star budget-app
    python -m hub_core.client agent-ask --question "..." --person alice --app budget-app

What you learned goes on the board as the right KIND of record, and standing doctrine is read
as your credential may see it::

    python -m hub_core.client finding "export drops rows with an empty region" --evidence "..."
    python -m hub_core.client method "verify at the deployed artifact" --how "..."
    python -m hub_core.client gap "no restore drill" --severity P2 --evidence "..."
    python -m hub_core.client review "may the export email real recipients?" --context "..."
    python -m hub_core.client doctrine --doc doctrine --text

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
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
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
    # X-Hub-Client itself carries version+sha (_common_headers); this is the digest the
    # hub compares against the client it serves.
    return {"X-Hub-Client-Version": client_version()}


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
    """Per-seat client state (route health, the offline queue, the blind window). Resolved
    through the Python user profile, never a shell variable: on some workstations the shell's
    home is a network share that is slow or unmapped."""
    from pathlib import Path
    return Path(os.environ.get("HUB_CLIENT_HOME") or os.environ.get("HUB_CLIENT_STATE_DIR")
                or os.path.expanduser("~/.hub-client"))


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

    def codes(self) -> list[str]:
        errors = self.body.get("errors") if isinstance(self.body, dict) else None
        return [str(e.get("code")) for e in (errors or []) if isinstance(e, dict)]


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


def _is_hub_answer(body: Any) -> bool:
    """The hub's own JSON envelope (an answer), as opposed to a gateway's error page."""
    return isinstance(body, dict) and ("errors" in body or "data" in body or "ok" in body)


def _request(bases: list[str], method: str, path: str, *, data: bytes | None,
             headers: dict[str, str], timeout: int | None = None,
             _busy: int = 0) -> dict[str, Any]:
    import socket
    import time as _time
    busy = _busy
    per_try = timeout or (_write_timeout_s() if method != "GET" else _read_timeout_s())
    deadline = _time.monotonic() + max(_budget_s(), per_try)
    last: HubUnreachable | None = None
    for attempt in range(ATTEMPTS):
        for base in bases:
            if _time.monotonic() >= deadline and last is not None:
                raise last
            request = urllib.request.Request(f"{base}/{path.lstrip('/')}", data=data,
                                             headers=_safe_headers(headers), method=method)
            try:
                with urllib.request.urlopen(request, timeout=per_try) as response:
                    _note_hub_client(response)
                    raw = response.read()
                    status = getattr(response, "status", 200)
                    payload = _decode_success(status, raw) if method != "GET" else \
                        json.loads(raw.decode("utf-8") or "{}")
                _mark_route(base, failed=False)
                _window_closed(base)                # the hub answered: report a blind window
                return payload
            except UnicodeError as error:
                # A request this machine could not BUILD never reached any route.
                raise _local_fault(base, error) from error
            except urllib.error.HTTPError as error:
                _note_hub_client(error)
                if error.code < 500:
                    _window_closed(base)            # the hub ANSWERED, even if it said no
                detail = error.read().decode("utf-8", errors="replace")[:8000]
                try:
                    parsed: Any = json.loads(detail)
                except json.JSONDecodeError:
                    parsed = detail
                if error.code == 503 and _retryable(parsed) and busy < _BUSY_ATTEMPTS - 1 \
                        and _time.monotonic() < deadline:
                    # BACK-PRESSURE, NOT A FAULT: the refusal states nothing was written (the
                    # ledger lock was busy), so even a write is safe to repeat on this route.
                    busy += 1
                    try:
                        delay = float(error.headers.get("Retry-After") or 2)
                    except (TypeError, ValueError):
                        delay = 2.0
                    _time.sleep(max(0.5, min(delay, 10.0)) * busy)
                    return _request([base] + [b for b in bases if b != base], method, path,
                                    data=data, headers=headers, timeout=timeout,
                                    _busy=busy)
                if error.code == 503 and _retryable(parsed):
                    raise HubRefused(error.code, parsed) from error
                if error.code >= 500 and _is_hub_answer(parsed):
                    # The HUB answered 5xx in its own envelope: an answer, never retried --
                    # retrying an answer only repeats it (or, for a write, re-applies it).
                    _mark_route(base, failed=False)
                    raise HubRefused(error.code, parsed) from error
                if error.code == 503:
                    # A 503 page that is not the hub's envelope is the PROXY with no upstream to
                    # hand the request to: it never reached the hub, so even a write is safe to
                    # send again on the next sweep.
                    last = HubUnreachable("gateway HTTP 503, not the hub's answer", reached=False,
                                          bases=bases)
                    break
                if error.code >= 500:
                    # A gateway 502/504 in front of the hub: the request may have reached it.
                    # Another route ends at the same process, so it would only repeat the answer;
                    # the backed-off second sweep may retry a read.
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
            if not _REPORTING["active"]:
                _window_failed(bases[0] if bases else "", True, str(last)[:200])
            raise last
        if attempt + 1 < ATTEMPTS and _time.monotonic() < deadline:
            _time.sleep(1 + attempt)          # once per sweep, never between two routes
    assert last is not None
    if not _REPORTING["active"]:
        # Everything that fails while the hub is unreachable is invisible to the board, so the
        # span is remembered locally and reported by the first call that succeeds.
        _window_failed(bases[0] if bases else "", method != "GET", str(last)[:200])
    raise last


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


# -- The blind window: a span in which this client could not reach the hub --
# Every failure inside it is invisible to the board by construction (the report channel IS
# the hub), so the span is remembered locally - across restarts - and reported on the first
# call that succeeds. Byte-locked so two processes on one machine cannot tear the state, and
# read with a retry so a reader never mistakes a half-written file for "no window".

_REPORTING = {"active": False}


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


# Operations whose server path honours `idem_key` (a repeated key with an identical payload
# replays the first event instead of writing again). Only these are retried automatically: a
# retry of anything else could double-apply, and a retry that reports failure for a write that
# landed is worse than the failure it was avoiding.
RETRY_SAFE_OPERATIONS = frozenset({"task", "directive", "answer", "ask", "ack", "gap", "note"})
RETRY_ATTEMPTS = 3
RETRY_STATUSES = frozenset({502, 503, 504})   # the edge lost the response or the hub was busy


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header values http.client can actually put on the wire.

    It encodes them as LATIN-1, so one non-ASCII character -- an em dash in a focus line, a
    curly quote, an accented machine name -- raises UnicodeEncodeError BEFORE a byte is sent.
    Every address then fails identically, which reads exactly like the Hub being down. These
    values are telemetry about the session, so transliterating what will not fit is right: the
    request survives and the header stays readable."""
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        text = "" if value is None else str(value)
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            text = text.encode("ascii", "replace").decode("ascii")
        out[str(key)] = text
    return out


def _unattended_headers() -> dict[str, str]:
    """An UNATTENDED run says so on every request (HUB_UNATTENDED=1, set by its launcher), with
    the escalation depth its launcher gave it (HUB_RESPONDER_HOP). The Hub stamps what the run
    raises with that depth, stamps its answers as unattended, and offers it only what an
    unattended run may take (hub_core.offer). An attended session sends nothing."""
    if os.environ.get("HUB_UNATTENDED", "").strip() not in ("1", "true", "yes"):
        return {}
    try:
        hop = max(1, int(os.environ.get("HUB_RESPONDER_HOP") or 1))
    except ValueError:
        hop = 1
    return {"X-Hub-Unattended": "1", "X-Hub-Hop": str(hop)}


def _local_fault(base: str, error: UnicodeError) -> RuntimeError:
    """A request this machine could not BUILD never reached the Hub: say so in its own words,
    never 'unreachable' -- that tells the caller its writes are queueing while the Hub is fine."""
    return RuntimeError(f"local request fault (the Hub at {base} was never asked): "
                        f"{type(error).__name__}: {str(error)[:160]}")


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
          extra_headers: dict[str, str] | None = None,
          timeout: float | None = None) -> dict[str, Any]:
    """POST one write over every configured route (``_request``).

    For a retry-safe operation the request carries an idempotency key (minted here when the
    caller supplied none), and a failure that says nothing about whether the write landed -- a
    read timeout, a reset, a 502/503/504 from the edge -- is retried with the SAME key and
    payload. The server replays a request that already landed, so the caller sees one success
    (`data.replayed` says so) instead of a failure for a write that happened, or a second copy
    of it. Only when every retry is spent does the outcome-unknown path apply."""
    headers = {"Content-Type": "application/json", **_common_headers(), **_auth_headers(),
               **_telemetry_headers(), **_unattended_headers(), **_artifact_headers(),
               **(extra_headers or {})}

    def once(body: dict[str, Any]) -> dict[str, Any]:
        return _request(_bases_of(base), "POST", f"api/{operation}",
                        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                        headers=headers, timeout=timeout)

    if operation not in RETRY_SAFE_OPERATIONS:
        return once(payload)
    payload = dict(payload)
    payload.setdefault("idem_key", "cli:" + uuid.uuid4().hex)
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return once(payload)
        except HubUnreachable as error:
            if not error.reached:
                raise                      # no route connected: the offline queue's case
            if attempt == RETRY_ATTEMPTS:
                raise
            delay = 0.5 * attempt + random.uniform(0, 0.5)
            print(json.dumps({"transient": str(error)[:200], "retry": attempt,
                              "retry_in_s": round(delay, 2)}), file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError("unreachable")


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


_BUSY_ATTEMPTS = 3
_RETRYABLE_CODES = {"busy", "update_write_failed"}


def _retryable(body: Any) -> bool:
    """True only for a refusal that states nothing was written."""
    try:
        return any((e or {}).get("code") in _RETRYABLE_CODES for e in body.get("errors") or [])
    except AttributeError:
        return False


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
        return {}          # no credential: a read on a public board, else the Hub's 401 says why


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


# Set only while REPLAYING a queued write: the identity of the console that queued it.
_REPLAY_ORIGIN: dict[str, str] | None = None


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
    the board's live-console view is only as true as what the seats send. The console's name,
    repository and runtime ride along so the roster can bind it to a project; the repository
    is read from .git/config (never a subprocess) unless HUB_REPO says otherwise.

    The cwd and the repo are sent as ONE fact: the hub applies the repo that accompanies a
    cwd, and treats a missing repo beside a cwd as "this directory is in no repository".
    HUB_FILES (comma-separated) is the console's recently touched files; the hub stamps it
    on arrival and ages it out, so a stale list never outlives the work it described."""
    if _REPLAY_ORIGIN:
        # A REPLAYED write speaks as the console that QUEUED it, identity only: a lease is held
        # by a console, and the location headers would move that console's presence row to
        # wherever the flushing console happens to stand.
        return {name: value for name, value in (
            ("X-Hub-Machine", _REPLAY_ORIGIN.get("machine", "")),
            ("X-Hub-Session", _REPLAY_ORIGIN.get("session", ""))) if value}
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
        # What the crossover detector and the attended/unattended split read. A supervisor that
        # launches an unattended run sets HUB_SESSION_KIND (and HUB_RUN_ID / HUB_SUBJECT).
        # What crossover detection compares: the project this console stands in (declared,
        # or the repository's own folder name), its display name, and whether it is an
        # unattended process nobody is reading.
        "X-Hub-Project": os.environ.get("HUB_PROJECT") or _repo_name(),
        "X-Hub-Console-Name": os.environ.get("HUB_CONSOLE_NAME", ""),
        "X-Hub-Unattended": os.environ.get("HUB_UNATTENDED", ""),
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
        if getattr(arguments, "project", None):
            values["X-Hub-Project"] = arguments.project
        if getattr(arguments, "file", None):
            values["X-Hub-Files"] = ",".join(arguments.file)
        if getattr(arguments, "name", None):
            values["X-Hub-Console-Name"] = arguments.name
        if getattr(arguments, "unattended", None):
            values["X-Hub-Unattended"] = "1"
    headers = {name: value for name, value in values.items() if value}
    if arguments is not None and getattr(arguments, "files", None) is not None:
        # A CURRENT claim: `--files a b` replaces this console's list, a bare `--files` sends an
        # empty header ("nothing edited recently") and clears it, omitting it keeps the last one.
        headers["X-Hub-Files"] = ",".join(arguments.files)[:1800]
    return headers


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
                    headers={**_common_headers(), **_optional_auth_headers(),
                             **_telemetry_headers(), **_unattended_headers()},
                    timeout=timeout)


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
    if getattr(arguments, "only_on", None):
        # MACHINE AFFINITY: this task's input exists on one machine, so only a worker there is
        # offered it (hub_core.offer). Not --machine: that flag is the WRITER's presence.
        payload["machine"] = arguments.only_on.strip().lower()
    if getattr(arguments, "project", None):
        payload["project"] = arguments.project.strip().lower()
    required = list(dict.fromkeys(arguments.requires + (["unattended"] if arguments.unattended
                                                         else [])))
    if required:
        # A required capability is a hard placement filter at pull time; `unattended` is the
        # one the event-driven responder (hub_core.responder) takes, so an attended worker
        # that never declared it is never handed the task by `next`.
        payload["routing"] = {"required_capabilities": required}
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


def _payload_hand(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """GIVE a task to a named agent. `--to` is the recipient; the writer stays you."""
    to = (arguments.to or "").strip().lower()
    me = _agent(arguments).strip().lower()
    if to and to == me:
        raise ValueError(f"hand: {to} is you — handing gives a task to someone else; to work it "
                         f"yourself run `start {arguments.task_id}`")
    payload = {"id": arguments.task_id, "to": to, "agent": _agent(arguments)}
    if getattr(arguments, "only_on", None) is not None:
        payload["machine"] = arguments.only_on.strip().lower()
    return "hand", payload


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
    # THE LOOP BREAK IS MECHANICAL. An unattended responder (hub_core.responder) never works
    # an ask stamped `via=responder`; the stamp is added HERE whenever the launching process
    # marked the session unattended, so a chain of automated escalations stops at one hop
    # without depending on a model remembering to type the marker.
    if os.environ.get("HUB_UNATTENDED") == "1":
        stamp = "via=responder"
        if stamp not in (payload.get("context") or ""):
            payload["context"] = ((payload.get("context") or "") + " " + stamp).strip()
    # An unattended run's launcher sets HUB_RESPONDER_HOP; every question the run raises is
    # stamped with it by the process, so the chain bound never depends on the model's memory.
    hop = getattr(arguments, "hop", None) if getattr(arguments, "hop", None) is not None else os.environ.get("HUB_RESPONDER_HOP")
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
    # PINNED delivery: one computer, and/or one console (session id or its display NAME —
    # the hub refuses a console that is not live, rather than delivering nowhere).
    if arguments.pin_machine:
        payload["machine"] = arguments.pin_machine
    if arguments.session:
        payload["session"] = arguments.session
    return "directive", payload


def _payload_ack(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "directive": arguments.directive_id}
    if arguments.note:
        payload["note"] = arguments.note
    if arguments.revision is not None:
        payload["delivery_revision"] = arguments.revision
    return "ack", payload


def _ack_any(base: str, item_id: str, agent: str, note: str = "",
             evidence: str = "", revision: int | None = None, via: str = "",
             headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Acknowledge ANY addressed id — the caller should not have to know which store it
    lives in. Routed by the id's own type, with the directive store asked first for anything
    else, and a fingerprint considered only when the directive store does not know the id:

      ov-...                  a crossover signal -> recorded as seen for this side
      a message (m-... note)  a message addressed to you -> api/message/ack
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
    local = item_id.rsplit(":", 1)[-1]
    if local.startswith("m-") and (":note:" in item_id or ":" not in item_id):
        payload = {"agent": agent, "id": item_id}
        if via:
            payload["via"] = via
        return {"routed": "message",
                "response": _post(base, "message/ack", payload, extra_headers=headers)}
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
    if revision is not None:
        payload["delivery_revision"] = revision
    try:
        return {"routed": "directive",
                "response": _post(base, "ack", payload, extra_headers=headers)}
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
                    arguments.evidence or "", revision=getattr(arguments, "revision", None),
                    headers=_presence_headers(arguments))


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


def _payload_ci_failure(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Post a failed job's log TAIL; the hub classifies it by what the log says."""
    from hub_core import ci_trace
    trace = ""
    if arguments.trace_file:
        stream = sys.stdin if arguments.trace_file == "-" else open(
            arguments.trace_file, encoding="utf-8", errors="replace")
        with stream:
            trace = stream.read()[-ci_trace.TAIL_CHARS:]
    payload: dict[str, Any] = {"project": arguments.project, "job": arguments.job, "trace": trace}
    for name in ("pipeline", "job_id", "ref", "sha", "source", "url"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if arguments.deployless:
        payload["deployless"] = True
    return "ci-failure", payload


#: A deploy record is posted seconds after the release restarted the service, so its first read
#: pays for the whole warm-up. One short timeout there turns a slow hub into a LOST record.
DEPLOY_RECORD_TIMEOUTS = (20.0, 45.0, 90.0)
#: Statuses that are the edge or a waking service talking, not the Hub's verdict on the record.
_TRANSIENT_STATUSES = {502, 503, 504}


def _transient(error: RuntimeError) -> bool:
    """True for a transport failure or a gateway status; False for any Hub verdict (4xx, 500)."""
    text = str(error)
    if text.startswith("Hub is unreachable"):
        return True
    try:
        return int(json.loads(text).get("status")) in _TRANSIENT_STATUSES
    except (ValueError, TypeError, AttributeError):
        return False


def _run_deploy(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Record one immutable release closure, surviving a cold hub.

    Retried ONLY because the write is idempotent by sha: the Hub answers an exact repeat of the
    same proof as ``idempotent: true``, so an attempt that landed but whose reply was lost cannot
    become a second record or a false failure on the next try. Each transport failure prints one
    ``DEPLOY_RECORD_RETRY`` line on stderr, so a hub that is getting slower stays audible; a
    refusal (bad sha, task not done, changed proof) is a verdict and is never retried."""
    import time
    sha = arguments.sha.strip().lower()
    payload: dict[str, Any] = {"sha": sha, "served_sha": (arguments.served_sha or "").strip().lower(),
                               "tasks_closed": list(arguments.task or []), "agent": _agent(arguments)}
    for name in ("at", "method", "build"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if "at" not in payload:
        # Stamped ONCE, before the first attempt: a retry must repeat the same proof exactly,
        # or the idempotent repeat becomes a refused rewrite of this sha's record.
        import datetime
        payload["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if arguments.audit_ok is not None:
        payload["audit_ok"] = arguments.audit_ok == "true"
    try:
        timeouts = [float(v) for v in str(arguments.timeouts).split(",") if v.strip()]
    except ValueError as error:
        raise ValueError("--timeouts takes comma-separated seconds, e.g. 20,45,90") from error
    if not timeouts or any(t <= 0 for t in timeouts):
        raise ValueError("--timeouts needs at least one positive number of seconds")
    last: RuntimeError | None = None
    for attempt, timeout in enumerate(timeouts, start=1):
        try:
            result = _post(base, "deploy", payload, extra_headers=_presence_headers(arguments),
                           timeout=timeout)
            if attempt > 1:
                result = {**result, "attempts": attempt}
            return result
        except RuntimeError as error:
            if not _transient(error):
                raise
            last = error
            print(f"DEPLOY_RECORD_RETRY sha={sha[:12]} attempt={attempt}/{len(timeouts)} "
                  f"timeout_s={timeout:g} reason={error}", file=sys.stderr)
            if attempt < len(timeouts):
                time.sleep(min(5.0 * attempt, 30.0))
    raise RuntimeError(f"DEPLOY_RECORD_FAILED sha={sha[:12]} after {len(timeouts)} attempts: {last}")


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


# ── Record verbs: which KIND of record a thing is decides where it lands ──
# finding = a fact you DISCOVERED about how something actually behaves (needs evidence);
# method  = a procedure this project both follows and exhibits (reusable practice);
# gap     = a named deficiency somebody could own and close, with a severity;
# review  = a human gate -- a question only a person may answer, raised BEFORE the thing ships.
# A lesson (a rule earned from a mistake) is a `note` in the `gotcha` category. Filing
# everything as one kind is how a board's other tabs go stale while one list grows.

def _payload_finding(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": arguments.title, "category": "discovery",
                               "status": "standing", "body_md": arguments.evidence,
                               "tags": ["finding", *arguments.tag], "agent": _agent(arguments)}
    if arguments.relates_to:
        payload["relates_to"] = arguments.relates_to
    return "note", payload


def _payload_method(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": arguments.title, "category": "method",
                               "status": "standing", "body_md": arguments.how,
                               "tags": ["method", *arguments.tag], "agent": _agent(arguments)}
    if arguments.relates_to:
        payload["relates_to"] = arguments.relates_to
    return "note", payload


def _payload_gap(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": arguments.title, "status": "open",
                               "severity": arguments.severity, "evidence": arguments.evidence,
                               "agent": _agent(arguments)}
    if arguments.source:
        payload["source"] = arguments.source
    return "gap", payload


def _payload_review(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """A review gate rides the ask loop on purpose: it must be DELIVERED to the person who
    decides, not merely recorded where they might one day look."""
    payload: dict[str, Any] = {"agent": _agent(arguments),
                               "question": "Review gate: " + arguments.question,
                               "context": (arguments.context or "") +
                                          ("\n\nThis is a human gate: nothing it covers ships "
                                           "until a person answers."),
                               "anyway": True, "review": True}
    if arguments.relates_to:
        payload["relates_to"] = arguments.relates_to
    return "ask", payload


def _run_doctrine(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """A standing document as THIS credential may read it (facet fences applied server-side)."""
    from urllib.parse import quote
    payload = _get(base, f"doctrine.json?doc={quote(arguments.doc)}")
    if arguments.text:
        return {"text": (payload.get("data") or {}).get("text", "")}
    return payload


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


_INBOX_HEADINGS = (("message", "MESSAGES"), ("gate", "GATES ONLY A PERSON CAN CLEAR"),
                   ("decision", "DECISIONS WAITING ON YOU"), ("question", "QUESTIONS"),
                   ("assignment", "GIVEN TO YOU"), ("answer", "ANSWERS"),
                   ("directive", "DIRECTIVES"), ("task-stall", "TASK ROT"),
                   ("attention", "NEEDS ATTENTION"), ("overlap", "CROSSOVERS"))


def render_inbox(data: dict[str, Any]) -> str:
    """The addressed set as text a person reads: grouped by kind, each with its reply command."""
    items = data.get("items") or []
    if not items:
        return "Nothing is addressed to you."
    lines = []
    known = {kind for kind, _ in _INBOX_HEADINGS}
    # A kind this table does not name yet is still shown: an addressed item must never render
    # as nothing just because its heading is missing.
    headings = _INBOX_HEADINGS + tuple(
        (kind, str(kind).upper()) for kind in dict.fromkeys(i.get("kind") for i in items)
        if kind not in known)
    for kind, heading in headings:
        rows = [i for i in items if i.get("kind") == kind]
        if not rows:
            continue
        lines.append("%s (%d)" % (heading, len(rows)))
        for item in rows:
            lines.append("  - %s" % (item.get("title") or item.get("id")))
            if item.get("reply_cmd"):
                lines.append("      %s" % item["reply_cmd"])
    return "\n".join(lines)


def _payload_hold(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Record a finished commit deliberately NOT live yet, so it ages in public.

    The client that holds the repository attests the commit is fetchable -- ``git branch -r
    --contains <sha>`` names a remote branch -- and the Hub also asks its own resolver. A commit
    on no remote needs --unpushed-reason: it is allowed, never silently."""
    import subprocess
    sha = arguments.sha.strip().lower()
    attested, where = False, ""
    try:
        run = subprocess.run(["git", "branch", "-r", "--contains", sha], capture_output=True,
                             text=True, timeout=15)
        remotes = [line.strip() for line in run.stdout.splitlines() if line.strip()]
        attested = run.returncode == 0 and bool(remotes)
        where = remotes[0] if remotes else ""
    except (OSError, subprocess.SubprocessError):
        attested = False
    payload: dict[str, Any] = {"agent": _agent(arguments), "repo": arguments.repo,
                               "sha": sha, "reason": arguments.reason,
                               "rebuild": arguments.rebuild, "attested": attested}
    if arguments.branch or where:
        payload["branch"] = arguments.branch or where.split("/", 1)[-1]
    if arguments.gap:
        payload["from_gap"] = arguments.gap
    if arguments.unpushed_reason:
        payload["unpushed_reason"] = arguments.unpushed_reason
        payload["local_path"] = os.getcwd()
    if arguments.title:
        payload["title"] = arguments.title
    return "held", payload


def _payload_promote(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "repo": arguments.repo,
                               "sha": arguments.sha.strip().lower(),
                               "evidence": arguments.evidence}
    if arguments.note:
        payload["note"] = arguments.note
    return "held/promote", payload


def _payload_abandon(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    return "held/abandon", {"agent": _agent(arguments), "repo": arguments.repo,
                            "sha": arguments.sha.strip().lower(), "reason": arguments.reason}


def _run_held(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, "held.json" + (f"?repo={quote(arguments.repo)}" if arguments.repo else ""))


def _run_lineage(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """A task traced hop by hop to what is serving it: the commits it recorded (`step --sha`),
    the verified deploy that carries one, the first release to carry it, and whether what is
    serving now still contains it. Every hop that cannot be established says unknown and why."""
    payload = _get(base, f"task/{_task_local(arguments.task_id)}.json?lineage=1")
    return (payload.get("data") or {}).get("lineage") or payload


def _payload_item_claim(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Claim a non-task item (a question, an error fingerprint) for ONE machine, so two
    machines never spend a session on the same thing. The same machine re-claims idempotently."""
    machine = arguments.machine or os.environ.get("HUB_MACHINE") or ""
    if not machine:
        raise ValueError("item-claim needs --machine (or HUB_MACHINE): a claim is per machine")
    payload: dict[str, Any] = {"agent": _agent(arguments), "item": arguments.item,
                               "machine": machine}
    session = getattr(arguments, "session", None) or os.environ.get("HUB_SESSION_ID") or ""
    if session:
        payload["session"] = session     # the console that holds it, so a GONE one can release
    if arguments.release:
        payload["release"] = True
    return "item-claim", payload


def _run_item_claims(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "item-claims.json")


def _run_inbox(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """What is addressed to you now (`--text`: grouped for a person, each with its reply
    command). `--ack <id>` retires one delivered MESSAGE instead (an answer or directive is
    acked with `ack`, naming its delivery revision)."""
    if getattr(arguments, "ack", None):
        # Any addressed id, routed by its own type (a message, a crossover, a problem, a
        # directive or answer, an error signature).
        return _ack_any(base, arguments.ack, arguments.agent, getattr(arguments, "note", "") or "",
                        getattr(arguments, "evidence", "") or "",
                        via=getattr(arguments, "via", "") or "",
                        headers=_presence_headers(arguments))
    payload = _get(base, f"inbox.json?{_reader_query(arguments)}")
    if getattr(arguments, "text", False):
        return {"text": render_inbox(payload.get("data") or {})}
    return payload


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


def _run_hand(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """`hand --to <agent>` gives the task to that agent (the recipient's inbox carries it until
    they claim it); `hand` without --to hands it back to the queue for an unattended worker."""
    if getattr(arguments, "to", None) is not None:
        operation, payload = _payload_hand(arguments)
        return _post(base, operation, payload, extra_headers=_presence_headers(arguments))
    return _run_let_go("hand-to-queue")(base, arguments)


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


def _run_feed(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"project/{quote(arguments.project)}/tasks.json")


def _run_overlap_seen(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _post(base, "overlap-seen", {"ids": arguments.ids, "agent": _agent(arguments)},
                 extra_headers=_presence_headers(arguments))


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
    """Hand back a PROBLEM (p-<12 hex>: its claim) or a held TASK (the lease only; the task
    stays on the board) and retract the focus `start` declared for it."""
    pid = _problem_id(arguments.task_id)
    if pid:
        return _post(base, "problem/release", {"problem": pid, "agent": _agent(arguments),
                                               **_console_fields()},
                     extra_headers=_presence_headers(arguments))
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


def _run_consoles_mine(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
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


def _run_consoles(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Every live console on the board — project, state, focus, files, and the task THAT
    console holds — with this console marked, the projects it is in without a task, the
    attended/unattended split, and the crossovers between consoles. `--mine` answers only the
    signals that concern THIS console (`--seen` records them as delivered)."""
    if getattr(arguments, "mine", False):
        return _run_consoles_mine(base, arguments)
    from urllib.parse import urlencode
    query: dict[str, str] = {}
    if arguments.agent:
        query["agent"] = arguments.agent
    session = getattr(arguments, "session", None) or os.environ.get("HUB_SESSION_ID", "")
    if session:
        query["session"] = session
    payload = _get(base, "activity.json" + ("?" + urlencode(query) if query else ""))
    # The attended / unattended / finished split and the crossover pairs (consoles.json); with
    # a session, only the crossover signals addressed to that console, phrased from its side.
    try:
        split = _get(base, "consoles.json" + ("?" + urlencode({"session": session})
                                              if session else "")).get("data") or {}
    except RuntimeError as error:
        split = {"error": str(error)[:300]}
    payload["split"] = {k: split.get(k) for k in ("counts", "unattended", "finished", "error")
                        if k in split}
    payload["crossovers"] = split.get("crossovers") or []
    if "addressed" in split:
        payload["addressed"] = split["addressed"]
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


def _stream_age(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return "<1m"
    hours, rest = divmod(seconds, 3600)
    return "%dh%02dm" % (hours, rest // 60) if hours else "%dm" % (rest // 60)


def _run_errors_stream(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Read the operational error stream. COUNT THE QUEUE, NOT THE LISTING: the summary is
    taken from each row's own `bar` verdict and the server's queue counts, whatever this call
    asked to list — a listing that includes deferred rows must never print them as unclaimed
    work. The remainder the bar held back is named on its own line."""
    from urllib.parse import urlencode
    query = {k: v for k, v in (("app", arguments.app), ("include", arguments.include)) if v}
    payload = _get(base, "errors.json" + ("?" + urlencode(query) if query else ""))
    meta = payload.get("metadata") or {}
    rows = payload.get("data") or []
    listed_off = sum(1 for r in rows if r.get("bar") == "deferred")
    summary = ["%d on the queue%s: %d unclaimed, %d claimed -- oldest unclaimed %s"
               % (int(meta.get("on_board") or 0),
                  (" for " + meta["app"]) if meta.get("app") else "",
                  int(meta.get("unclaimed") or 0), int(meta.get("claimed") or 0),
                  _stream_age(meta.get("oldest_unclaimed_s")))]
    deferred = int(meta.get("deferred") or 0)
    if deferred:
        summary.append("  + %d below the bar (counted, kept, not queued -- nobody is expected "
                       "to work these)%s" % (deferred, "" if listed_off else
                                             "; --include deferred lists them"))
    if meta.get("available") is False:
        summary.append("  ! the error store is IMPAIRED (%s) -- quiet here is not healthy"
                       % (meta.get("reason") or "write failure"))
    for line in summary:
        print(line, file=sys.stderr)
    return {"summary": summary, **payload}


def _run_search(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    return _get(base, f"search.json?q={quote(arguments.query)}&limit={arguments.limit}")


def _run_components(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    suffix = f"?kind={quote(arguments.kind)}" if arguments.kind else ""
    return _get(base, "components.json" + suffix)


def _payload_capability(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Register (or, with --expected-version, update) a capability — including a standard
    component or an app skeleton."""
    payload: dict[str, Any] = {"agent": _agent(arguments), "name": arguments.name,
                               "maturity": arguments.maturity}
    for name in ("kind", "what", "when", "get", "entry", "delivery", "hosted_at", "exemplar",
                 "iface"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if arguments.cap_local:
        payload["local"] = arguments.cap_local
    for name in ("depends_on", "applies", "adopters"):
        value = getattr(arguments, name, None)
        if value:
            payload[name] = value
    if arguments.default:
        payload["default"] = True
    if arguments.applies_all:
        payload["applies_all"] = True
    if arguments.expected_version is not None:
        payload["expected_version"] = arguments.expected_version
    return "capability", payload


def _run_list(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """One WHOLE collection. hub.json may carry a large collection only as a head (its
    `partial` block says which); this is the read that returns every row."""
    from urllib.parse import quote
    return _get(base, f"{quote(arguments.type)}.json")


def _run_questions(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    return _get(base, "questions.json")


def _run_whoami(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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
    one problem with its full stored trace (head AND tail). --include deferred|all reads the
    raw error stream instead, with the queue's own counts first."""
    if getattr(arguments, "include", None):
        return _run_errors_stream(base, arguments)
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


def _run_hosted_components(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """The UI components this hub hosts for its apps (GET /hub/components/)."""
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
    """Read or change one person's preferences, as the app that signed them in.

    `--set key=value` merges everywhere; with `--app <slug>` it sets that app's override
    instead, and `--clear-app` puts that app back on the everywhere-values. `--star` /
    `--unstar` edit the starred apps (a list that replaces, so the current one is read first).
    With no change it reads, including the apps the person can reach and, with --app, what that
    app resolves to. Needs profile:read / profile:write."""
    from urllib.parse import urlencode
    query = {"person": arguments.person}
    if arguments.app:
        query["app"] = arguments.app
    path = "profile?" + urlencode(query)
    change: dict[str, Any] = _pairs(arguments.set)
    if arguments.app and (change or arguments.clear_app):
        change = {"apps": {arguments.app: {} if arguments.clear_app else change}}
    elif arguments.clear_app:
        raise ValueError("--clear-app needs --app <slug>")
    if arguments.star or arguments.unstar:
        current = _get(base, "api/profile?" + urlencode({"person": arguments.person}))
        starred = list(((current.get("data") or {}).get("prefs") or {}).get("starred") or [])
        starred = [s for s in starred if s not in (arguments.unstar or [])]
        starred += [s for s in (arguments.star or []) if s not in starred]
        change["starred"] = starred
    if change:
        return _post(base, path, {"prefs": change})
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
    them. Answers 429 body verbatim at WIP saturation — a refusal, never silence. Work given to
    somebody else by name is left out for the calling agent."""
    from urllib.parse import quote
    who = arguments.agent or os.environ.get("HUB_AGENT_ID") or ""
    where = arguments.machine or os.environ.get("HUB_MACHINE") or ""
    return _get(base, f"next.json?n={max(1, int(arguments.n))}"
                      + (f"&agent={quote(who)}" if who else "")
                      + (f"&machine={quote(where)}" if where else ""))


def _run_start(base: Any, arguments: argparse.Namespace) -> dict[str, Any]:
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
    _journal_lease(arguments.task_id, claim)
    token = claim.get("token") or (claim.get("data") or {}).get("token") or ""
    entity = _fetch_task(base, arguments.task_id)
    sha = _charter_sha()
    return {"claim": claim, "lease_token": token, "task": entity,
            "charter": ({"sha256": sha, "carry": "pass to `finish --charter-sha`"} if sha else
                        {"present": False,
                         "note": "no charter core here — the finish regrounding gate is not in force"})}


#: Mirrors hub_core.plan.LIFECYCLE_KINDS: rows a scheduler wrote about its own run.
_LIFECYCLE_KINDS = frozenset({"handed_back", "lease_released", "reaped", "launcher_timeout",
                              "claim_expired", "lifecycle"})


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

    headers = _presence_headers(arguments)
    if entity.get("title"):
        # Retract the focus `start` declared — only if it is still that one; a newer focus set
        # by another verb is never cleared by this.
        headers["X-Hub-Focus-Retract"] = str(entity["title"])[:180]
    try:
        result = _post(base, "complete", payload, extra_headers=headers)
    except HubRefused as refusal:
        # A refused finish is visible on the board and carries its fix.
        _note_refused_finish(base, arguments, refusal)
        if "evidence_unresolvable" in refusal.codes():
            raise RuntimeError(str(refusal) + "\n" + _evidence_help(refusal)) from refusal
        raise
    feed = _auto_update(base, arguments, "fixed",
                        f"Finished {entity.get('title') or arguments.task_id}: {arguments.accept_note}",
                        evidence=(arguments.evidence or [""])[0], item=arguments.task_id)
    return {"completed": result, **({"feed": feed} if feed else {}),
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
    if operation == "complete":
        malformed = _evidence_problems(payload.get("evidence_uri") or [])
        if malformed:
            raise ValueError("evidence is a reference (a URL, a commit sha, a path), so it is one "
                             "token with no spaces; put prose in --accept-note. Not a reference: "
                             + "; ".join(repr(item) for item in malformed))
    try:
        result = _post(bases, operation, payload, extra_headers=_presence_headers(arguments))
        if operation == "claim":
            _journal_lease(payload["id"], result)
        return result
    except HubRefused as refusal:
        if operation == "complete":
            _note_refused_finish(bases, arguments, refusal)
            if "evidence_unresolvable" in refusal.codes():
                raise RuntimeError(str(refusal) + "\n" + _evidence_help(refusal)) from refusal
        raise


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
    create.add_argument("--requires", action="append", default=[],
                        help="a capability the claiming worker must declare (repeatable)")
    create.add_argument("--agent")
    create.add_argument("--project", help="the project this task is about, when it is not the "
                                          "Hub's own repository (its commits count as evidence)")
    create.add_argument("--only-on", dest="only_on",
                        help="MACHINE AFFINITY: only this machine can do the task (its input is "
                             "there); only a worker declaring it is offered the task")
    create.add_argument("--unattended", action="store_true",
                        help="offer it to unattended workers (P0-P2 only; never a decision) and "
                             "hand it to the unattended responder lane "
                             "(routing.required_capabilities += unattended)")
    create.add_argument("--decision", action="store_true",
                        help="a person's call: delivered to the deciders, never an unattended worker")
    create.add_argument("--work-kind", dest="work_kind")
    create.add_argument("--idem-key", dest="idem_key",
                        help="reuse the key a timed-out create printed to replay, not duplicate")
    create.add_argument("--machine")
    create.add_argument("--focus")
    create.set_defaults(runner=_payload_create_retry)

    claim = commands.add_parser("claim", help="claim a task (fencing token) or a problem "
                                              "(p-<12 hex>: your console's name on it)")
    claim.add_argument("task_id")
    claim.add_argument("--agent")
    claim.add_argument("--ttl-s", type=int)
    claim.add_argument("--note", help="problem claims: what you are checking")
    claim.add_argument("--take", action="store_true",
                       help="problem claims: displace a live holder (recorded on the claim)")
    claim.set_defaults(payload=_payload_claim)

    hold = commands.add_parser("hold", help="record a finished commit held back from live, so "
                                            "it ages in public until promoted with evidence")
    hold.add_argument("repo", help="the project path a reader can fetch, e.g. team/budget-app")
    hold.add_argument("sha")
    hold.add_argument("--reason", required=True, help="why it may not go live yet")
    hold.add_argument("--rebuild", required=True,
                      help="what must be rebuilt/re-indexed and PROVEN before it can go live")
    hold.add_argument("--branch", help="the remote branch it was parked on")
    hold.add_argument("--gap", help="the gap id this commit answers")
    hold.add_argument("--unpushed-reason", dest="unpushed_reason",
                      help="why this commit is on NO remote (recorded, marked ON ONE DISK ONLY)")
    hold.add_argument("--title")
    hold.add_argument("--agent")
    hold.set_defaults(payload=_payload_hold)

    promote = commands.add_parser("promote", help="free a held commit: the rebuild ran, here is "
                                                  "the proof")
    promote.add_argument("repo")
    promote.add_argument("sha")
    promote.add_argument("--evidence", required=True,
                         help="the pipeline, sha or URL of the rebuild that actually ran")
    promote.add_argument("--note", help="what the rebuild did, with its counts")
    promote.add_argument("--agent")
    promote.set_defaults(payload=_payload_promote)

    abandon = commands.add_parser("abandon", help="close a hold without promoting it (a decision "
                                                  "that states its reason)")
    abandon.add_argument("repo")
    abandon.add_argument("sha")
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--agent")
    abandon.set_defaults(payload=_payload_abandon)

    held = commands.add_parser("held", help="the promotion queue: what is held, oldest first")
    held.add_argument("--repo")
    held.set_defaults(runner=_run_held)

    lineage = commands.add_parser("lineage", help="trace a task hop by hop to what is serving it")
    lineage.add_argument("task_id")
    lineage.set_defaults(runner=_run_lineage)

    item_claim = commands.add_parser("item-claim", help="claim a question/error item for ONE "
                                                        "machine (one responder per item)")
    item_claim.add_argument("item", help="the item id (a question note id, an error fingerprint)")
    item_claim.add_argument("--machine")
    item_claim.add_argument("--release", action="store_true",
                            help="give the claim back (only the holding machine may)")
    item_claim.add_argument("--agent")
    item_claim.add_argument("--session", help="the console holding it (default HUB_SESSION_ID); "
                                              "a claim whose console is provably gone frees "
                                              "itself after the grace")
    item_claim.set_defaults(payload=_payload_item_claim)

    item_claims = commands.add_parser("item-claims", help="every live item claim")
    item_claims.set_defaults(runner=_run_item_claims)

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
    ask.add_argument("--hop", type=int,
                     help="escalation depth (default HUB_RESPONDER_HOP; unattended runs only)")
    ask.set_defaults(payload=_payload_ask)

    hand_back = commands.add_parser(
        "hand-back", help="a run ended with its task unfinished: back to todo with ONE self-counting row")
    hand_back.add_argument("task_id")
    hand_back.add_argument("--agent")
    hand_back.add_argument("--lease-token", dest="lease_token")
    hand_back.add_argument("--note", required=True, help="why the run ended with the task unfinished")
    hand_back.set_defaults(payload=_payload_hand_back)

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

    perf = commands.add_parser("perf", help="route latency, slow routes, snapshot phase timings, "
                                            "and which process answered (role, backgrounder "
                                            "clock, startup prewarm)")
    perf.add_argument("--profile", action="store_true",
                      help="profile one snapshot build (perf:profile scope)")
    perf.set_defaults(runner=_run_perf)

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
    ack.add_argument("--revision", type=int,
                     help="the delivery_revision you read (required when the answer carries one)")
    ack.add_argument("--evidence", help="problem ids: the sha/url that proves the fix")
    ack.set_defaults(runner=_run_ack)

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
    digest.add_argument("--files", nargs="*", default=None,
                        help="files this console edited recently, as <project>/<path> (crossover "
                             "detection); a bare --files clears the list")
    presence.add_argument("--file", action="append", default=[],
                          help="a file this console just edited (repeatable)")
    presence.add_argument("--name", help="this console's display name")
    presence.add_argument("--unattended", action="store_true",
                          help="an unattended process nobody is reading")
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
    consoles.add_argument("--mine", action="store_true",
                          help="only the crossovers that concern this console")
    consoles.add_argument("--seen", action="store_true",
                          help="with --mine: record them as delivered (announced once per side)")
    consoles.add_argument("--session",
                          help="this console (default HUB_SESSION_ID): marks it and narrows "
                               "crossovers to the signals addressed to it")
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

    ci_failure = commands.add_parser(
        "ci-failure", help="report a failed CI job; the hub classifies its log tail")
    ci_failure.add_argument("--project", required=True)
    ci_failure.add_argument("--job", required=True)
    ci_failure.add_argument("--trace-file", dest="trace_file",
                            help="the job log (or - for stdin); only its tail is sent")
    for name in ("pipeline", "job-id", "ref", "sha", "source", "url"):
        ci_failure.add_argument("--" + name, dest=name.replace("-", "_"))
    ci_failure.add_argument("--deployless", action="store_true",
                            help="this pipeline carries no deploy stage")
    ci_failure.set_defaults(payload=_payload_ci_failure)

    deploy = commands.add_parser(
        "deploy", help="record a verified release (idempotent by sha; retries a cold hub)")
    deploy.add_argument("--sha", required=True, help="the commit that was built and shipped")
    deploy.add_argument("--served-sha", dest="served_sha", required=True,
                        help="the identity the front-door canary actually observed")
    deploy.add_argument("--task", action="append",
                        help="a done task this release carries (repeatable; none is allowed)")
    deploy.add_argument("--at")
    deploy.add_argument("--method")
    deploy.add_argument("--build")
    deploy.add_argument("--audit-ok", dest="audit_ok", choices=("true", "false"))
    deploy.add_argument("--agent")
    deploy.add_argument("--timeouts", default=",".join("%g" % t for t in DEPLOY_RECORD_TIMEOUTS),
                        help="per-attempt timeouts in seconds, growing (default 20,45,90)")
    deploy.set_defaults(runner=_run_deploy)

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

    review = commands.add_parser("review-gate",
                                 help="raise a human gate: a question only a person may answer, "
                                      "DELIVERED to the operator before the thing ships (`review` "
                                      "records a review note instead)")
    review.add_argument("question")
    review.add_argument("--context")
    review.add_argument("--relates-to", action="append", default=[], dest="relates_to")
    review.add_argument("--agent")
    review.set_defaults(payload=_payload_review)

    doctrine = commands.add_parser("doctrine",
                                   help="read a standing document as your credential may see it")
    doctrine.add_argument("--doc", default="doctrine")
    doctrine.add_argument("--text", action="store_true", help="print only the rendered text")
    doctrine.set_defaults(runner=_run_doctrine)

    inbox = commands.add_parser("inbox", help="what is addressed to an agent right now")
    inbox.add_argument("--agent", required=True)
    inbox.add_argument("--session", help="this console's id (default HUB_SESSION_ID)")
    inbox.add_argument("--machine", help="this machine (default HUB_MACHINE)")
    inbox.add_argument("--ack", help="acknowledge one item by its id (routed by its type)")
    inbox.add_argument("--note")
    inbox.add_argument("--evidence")
    inbox.add_argument("--via", help="how it was delivered (recorded on the receipt)")
    inbox.add_argument("--text", action="store_true",
                       help="grouped for a person: decisions, questions, TASK ROT, attention, crossovers")
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

    for verb, operation, helptext in (
            ("hand", "hand-to-queue", "--to <agent>: give a task to a named agent (--to \"\" clears "
                                  "it); without --to: hand it back to the queue for an "
                                  "unattended worker"),
            ("unclaim", "unclaim", "let go of a task (also an orphaned console's lease of yours)")):
        let_go = commands.add_parser(verb, help=helptext)
        let_go.add_argument("task_id")
        let_go.add_argument("--agent")
        let_go.add_argument("--note")
        let_go.add_argument("--lease-token", dest="lease_token")
        let_go.add_argument("--machine")
        let_go.add_argument("--focus")
        if verb == "hand":
            let_go.add_argument("--to", default=None,
                                help="the RECIPIENT; --agent stays who is writing")
            let_go.add_argument("--only-on", dest="only_on",
                                help="with --to: also set MACHINE AFFINITY (\"\" clears it)")
            let_go.set_defaults(runner=_run_hand)
        else:
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

    feed = commands.add_parser("feed", help="one project's annotated task feed")
    feed.add_argument("project")
    feed.set_defaults(runner=_run_feed)

    seen = commands.add_parser("overlap-seen", help="record that crossover signals were delivered")
    seen.add_argument("ids", nargs="+")
    seen.add_argument("--agent")
    seen.set_defaults(runner=_run_overlap_seen)

    errors = commands.add_parser("errors", help="the operational queue, folded into PROBLEMS")
    errors.add_argument("--agent")
    errors.add_argument("--mine", action="store_true", help="problems I own or hold")
    errors.add_argument("--app", help="one service's problems")
    errors.add_argument("--all", action="store_true",
                        help="include what the read-time bar holds back (with the reason)")
    errors.add_argument("--resolved", action="store_true", help="include resolved problems")
    errors.add_argument("--trace", metavar="P_ID", help="one problem with its full stored trace")
    errors.add_argument("--json", action="store_true", help="also return the problem objects")
    errors.add_argument("--include", choices=("deferred", "all"),
                        help="read the RAW stream instead (queue counts first), also listing "
                             "the rows the bar held back")
    errors.set_defaults(runner=_run_errors)

    resolve = commands.add_parser("resolve", help="resolve a problem: ack every row behind it "
                                                  "and record the root cause")
    resolve.add_argument("problem_id")
    resolve.add_argument("--agent")
    resolve.add_argument("--note", required=True, help="the ROOT CAUSE")
    resolve.add_argument("--evidence", help="the sha or url that proves the fix")
    resolve.set_defaults(runner=_run_resolve)

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

    components = commands.add_parser(
        "components", help="standard components and app skeletons, resolved on this read")
    components.add_argument("--kind", help="component | skeleton")
    components.set_defaults(runner=_run_components)

    capability = commands.add_parser(
        "capability", help="register a capability, standard component, or app skeleton")
    capability.add_argument("--name", required=True)
    capability.add_argument("--kind", help="component | skeleton | service | python_module | ...")
    capability.add_argument("--maturity", default="proven",
                            help="concept | prototype | proven | reusable | extracted")
    capability.add_argument("--agent")
    for name in ("what", "when", "get", "entry", "delivery", "hosted-at", "exemplar", "iface"):
        capability.add_argument("--" + name, dest=name.replace("-", "_"))
    # NOT dest="local": that attribute is main()'s "no Hub URL needed" switch.
    capability.add_argument("--local", dest="cap_local",
                            help="the id's local part (default: slug of --name)")
    capability.add_argument("--depends-on", dest="depends_on", action="append",
                            help="a capability id this must come after (repeatable)")
    capability.add_argument("--applies", action="append",
                            help="skeleton: a component id it applies, in order (repeatable)")
    capability.add_argument("--adopters", action="append", help="an app that carries it")
    capability.add_argument("--applies-all", dest="applies_all", action="store_true",
                            help="skeleton: take every component, ordered by depends_on")
    capability.add_argument("--default", action="store_true")
    capability.add_argument("--expected-version", dest="expected_version", type=int,
                            help="required to update an existing capability")
    capability.set_defaults(payload=_payload_capability)

    listing = commands.add_parser("list", help="one whole collection (task, note, directive, ...)")
    listing.add_argument("type", help="singular type (task) or snapshot key (tasks)")
    listing.set_defaults(runner=_run_list)

    questions = commands.add_parser("questions",
                                    help="every question with waits, lanes, and reply times")
    questions.set_defaults(runner=_run_questions)

    whoami = commands.add_parser("whoami",
                                 help="what the hub resolves your credential and headers to")
    whoami.set_defaults(runner=_run_whoami)

    hosted = commands.add_parser("hosted-components",
                                 help="the UI components this hub hosts for its apps: versions, "
                                      "files, adopters (`components` lists the standard components "
                                      "and app skeletons a new app starts from)")
    hosted.set_defaults(runner=_run_hosted_components)

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
    prof.add_argument("--app", help="scope --set to this app's override, and resolve for it")
    prof.add_argument("--clear-app", action="store_true",
                      help="with --app: put that app back on the everywhere-values")
    prof.add_argument("--star", action="append", metavar="SLUG")
    prof.add_argument("--unstar", action="append", metavar="SLUG")
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
    nxt.add_argument("--agent", help="the calling agent (default HUB_AGENT_ID); hides work "
                                     "given to somebody else")
    nxt.add_argument("--machine", help="the calling machine (default HUB_MACHINE); work only "
                                       "another machine can do is left out")
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

    # Knowledge: share/finding/method/review/gap, recall, related, capabilities, the per-prompt
    # knowledge block, the local mirror, and overlap adjudication (hub_core/client_knowledge.py).
    # Registered LAST: it extends `recall` and `prompt-context` rather than shadowing them.
    from . import client_knowledge
    client_knowledge.register(commands)
    for sub in commands.choices.values():
        sub.allow_abbrev = False
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        local = getattr(arguments, "local", False)
        if local:
            bases = None
        elif getattr(arguments, "optional_base", False) or arguments.command == "flush":
            # A hook must still emit the local doctrine when no hub is configured, and archiving
            # dead letters needs no hub.
            try:
                bases = _base_urls(arguments.url)
            except ValueError:
                bases = None
        else:
            bases = _base_urls(arguments.url)
        if arguments.command == "flush":
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
        # Unattended agents narrate answer/ack on the feed by themselves (HUB_AUTOWORKER=1).
        if not getattr(arguments, "runner", None) and isinstance(result, dict):
            if arguments.command == "answer":
                feed = _auto_update(bases, arguments, "answered",
                                    "Answered %s: %s" % (arguments.question_id, arguments.text),
                                    item=arguments.question_id)
            elif arguments.command == "ack":
                feed = _auto_update(bases, arguments, "acked",
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
    if result is None:              # the runner printed its own human-readable output
        return 0
    converged = _maybe_converge()
    if converged and isinstance(result, dict):
        result["client_converge"] = converged
    if isinstance(result, dict) and isinstance(result.get("lines"), list):
        # A queue-shaped verb prints its lines for a person; everything else it returned
        # (counts, the objects with --json) follows as JSON for a machine.
        for line in result["lines"]:
            print(line)
        rest = {k: v for k, v in result.items() if k != "lines" and v not in (None, [], {})}
        if rest:
            print(json.dumps(rest, indent=2, sort_keys=True))
        return 0
    if isinstance(result, dict) and set(result) == {"text"}:
        print(result["text"])
        return 0
    code = int(result.pop("_exit", 0) or 0) if isinstance(result, dict) else 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
