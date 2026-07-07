"""agent-guard.py — the always-fires code layer under permissive agent modes.

Prefix/allowlist deny-rules are sidestep-able (rm -fr vs rm -rf, find -delete,
Remove-Item -Recurse); this hook pattern-matches the ACTUAL command for the small set of
catastrophes that must never run, regardless of mode. Exit 2 + stderr = block (the agent sees
the reason); anything else exits 0 — fail-open, because a broken guard must never brick the
session. Blocks append to ~/.claude/guard-blocks.log so enforcement is observable, and a
liveness beacon is written on every call so the guard's silent death is detectable out-of-band.

Target harness: Claude Code's PreToolUse hook — wire in ~/.claude/settings.json with matcher
Bash|PowerShell, command "python /path/to/agent-guard.py". Stdlib only; no venv, so a moved
virtualenv can't kill it. The PATTERN ports to any agent harness that offers a pre-execution
hook receiving the proposed command: adapt read_event()/the exit convention and keep RULES.
"""
import json
import os
import re
import sys
import time

LOG_DIR = os.environ.get("AGENT_GUARD_DIR") or os.path.join(os.path.expanduser("~"), ".claude")

RULES = [
    # (name, regex matched against the lowercased command, why)
    # --- Generic catastrophic commands: keep these everywhere -------------------------------
    ("recursive-force-delete-root",
     r"\brm\s+(-[a-z]*[rf][a-z]*\s+){1,3}(--\S+\s+)*[\"']?(/|/c|/home(/\w+)?|/users(/\w+)?|/c/users(/\w+)?|~|\$home|c:\\?)[\"']?\s*$",
     "recursive force-delete of a root path"),
    ("rm-rf-anything-broad",
     r"\brm\s+-(rf|fr|r\s+-f|f\s+-r)\b.*\*",
     "recursive force-delete with a wildcard — use targeted paths"),
    ("find-delete-root",
     r"\bfind\s+[\"']?(/|/c|c:\\|\$home|~)[\"']?\s[^|;]*-delete",
     "find -delete anchored at a root path"),
    ("mkfs-or-raw-dd",
     r"((^|[;&|]\s*|\$\(|`)\s*(sudo\s+)?mkfs(\.\w+)?\s|\bdd\s+[^|;]*of=/dev/)",
     "filesystem format / raw device write"),
    ("fork-bomb", r":\(\)\s*\{\s*:\|\:&\s*\}\s*;\s*:", "fork bomb"),
    ("ps-remove-root",
     r"remove-item\s+[^|;]*-recurse[^|;]*(c:\\(code|users(\\\w+)?)?[\"']?\s*$|c:\\[\"']?\s*$)",
     "Remove-Item -Recurse on a root path"),
    ("ps-disk-destroy",
     r"\b(format-volume|clear-disk|initialize-disk|remove-partition)\b",
     "disk-level destructive cmdlet"),

    # =========================================================================================
    # ORG-SPECIFIC RULES GO HERE — policy, not physics. Examples from a real deployment,
    # commented out; adapt or delete. Keep each rule certain-bad FOR YOUR ORG.
    # =========================================================================================
    # ("kill-user-browser",
    #  r"\b(stop-process|taskkill)\b[^|;]*(chrome|msedge|firefox)",
    #  "killing the user's browser is banned (use a headless browser and close that instead)"),
    # ("headless-paid-credit",
    #  r"\bclaude\s+(-p|--print)\b",
    #  "headless agent invocations draw a separately-billed credit; run interactive or get "
    #  "explicit operator sign-off (set GUARD_ALLOW_HEADLESS=1 in the command to override)"),
    # ("inline-api-key",
    #  r"\b[A-Z_]*API_KEY\s*=",
    #  "inline paid API key use is banned by org policy — use the sanctioned auth path"),
]


def main() -> int:
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except Exception:
        return 0
    # Liveness beacon: prove the guard is alive on every shell call, so its silent death
    # (moved interpreter, broken hook wiring) is detectable by an out-of-band checker.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "guard-health.json"), "w", encoding="utf-8") as _hf:
            _hf.write(json.dumps({"ts": int(time.time()), "pid": os.getpid()}))
    except Exception:
        pass
    if ev.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    low = cmd.lower()
    for name, rx, why in RULES:
        # Per-rule escape hatches (deliberate, in-command, so they leave a transcript trail)
        # belong here — e.g. the commented headless-paid-credit rule above would skip when
        # "guard_allow_headless=1" in low.
        if re.search(rx, low, re.IGNORECASE):
            try:
                with open(os.path.join(LOG_DIR, "guard-blocks.log"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": int(time.time()), "rule": name,
                                        "cmd": cmd[:400]}) + "\n")
            except Exception:
                pass
            sys.stderr.write(f"BLOCKED by guard rule '{name}': {why}. "
                             "If this is genuinely intended, the operator can run it manually.")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
