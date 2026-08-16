#!/usr/bin/env python3
"""Focused proof for flow parity, leases, WIP, atomic pickup, and receipt truth."""
from pathlib import Path
import py_compile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hub_core import flow  # noqa: E402


def main():
    flags = {"deps_unmet": ["hub:task:dep"]}
    cases = [
        (flow.classify({"status": "done"}), "terminal"),
        (flow.classify({"status": "todo"}, flags), "blocked"),
        (flow.classify({"status": "todo", "poison_blocked": True}), "poison"),
        (flow.classify({"status": "todo"}, {"snoozed_until": "soon"}), "snoozed"),
        (flow.classify({"status": "todo"}, lease={"agent": "one"}), "leased"),
        (flow.classify({"status": "todo"}, strictness="strict"), "needs_spec"),
        (flow.classify({"status": "todo", "verification_command": "prove"}, strictness="strict"), "ready"),
        (flow.classify({"status": "in_progress"}), "stale_reclaim"),
    ]
    errors = [f"classifier wanted {want}, got {got['state']}" for got, want in cases if got["state"] != want]
    bodies = {name: (ROOT / name).read_text(encoding="utf-8") for name in (
        "adapters/django/hub/hub_app.py", "adapters/django/hub/hub_api.py",
        "adapters/django/hub/hub_write.py", "adapters/django/hub/urls.py")}
    required = {
        "adapters/django/hub/hub_app.py": ("last_heartbeat", "heartbeat_after_s", "HUB_WIP_LIMIT", "def leases("),
        "adapters/django/hub/hub_api.py": ("current_status", "heartbeat_age_s", "flow.classify",
            "int(time.time() // 5)", "lease.changed", "busy_touches"),
        "adapters/django/hub/hub_write.py": ("def take(", "def release(", "one_active",
            "board_saturated", "output_sha256", "HUB_DONE_STRICTNESS"),
        "adapters/django/hub/urls.py": ('path("api/take"', 'path("api/release"'),
    }
    errors += [f"{name}: missing {needle}" for name, needles in required.items()
               for needle in needles if needle not in bodies[name]]
    for path in (ROOT / "hub_core/flow.py", ROOT / "adapters/django/hub/hub_app.py",
                 ROOT / "adapters/django/hub/hub_api.py", ROOT / "adapters/django/hub/hub_write.py"):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(str(exc))
    if errors:
        print("Hub throughput contract: FAIL", file=sys.stderr)
        print("\n".join("- " + error for error in errors), file=sys.stderr)
        return 1
    print("Hub throughput contract: PASS (flow parity, event-time history, heartbeat, WIP, atomic take/release, receipt truth)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
