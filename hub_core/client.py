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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


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


def _post(base: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json", **_auth_headers()}
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
            {"step": step, "status": "pending"} for step in arguments.plan_item
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
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        base = _base_url(arguments.url)
        operation, payload = arguments.payload(arguments)
        result = _post(base, operation, payload)
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
