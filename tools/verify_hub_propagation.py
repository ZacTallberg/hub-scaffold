#!/usr/bin/env python3
"""Prove fresh and differently branded adopters inherit the complete Hub contract."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    result = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(" ".join(args) + "\n" + result.stdout + result.stderr)
    return result.stdout


def bash_path(path):
    """Translate a Windows path for Git Bash; POSIX paths pass through unchanged."""
    path = Path(path).resolve()
    if path.drive:
        return "/" + path.drive[0].lower() + path.as_posix()[2:]
    return path.as_posix()


def main():
    try:
        for verifier in ("tools/verify_hub_excellence.py --contract", "tools/verify_hub_visual.py",
                         "tools/verify_hub_throughput.py"):
            run(sys.executable, *verifier.split())
        run(sys.executable, "tools/build_bootstrap.py", "--check")
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
        if not bash:
            raise RuntimeError("bash is required to exercise init.sh")
        with tempfile.TemporaryDirectory(prefix="hub-propagation-") as tmp:
            root = Path(tmp)
            cases = (("fresh-proof", "Fresh Proof", "https://fresh-proof.invalid"),
                     ("themed-proof", "Themed Proof", "https://themed-proof.invalid"))
            for key, brand, host in cases:
                target = root / key
                run(bash, "init.sh", bash_path(target), key, brand, host)
                identity = json.loads((target / "PROJECT/project.json").read_text(encoding="utf-8"))
                if (identity["key"], identity["brand"], identity["app_host"]) != (key, brand, host):
                    raise RuntimeError(f"identity drift in {key}: {identity}")
                for rel in ("PROJECT/HUB-QUALITY.md", "hub_core/frontend/theme.js",
                            "hub_core/frontend/README.md", "campaigns/elevate-hub.md"):
                    if not (target / rel).is_file():
                        raise RuntimeError(f"{key} missing {rel}")
                # init.sh itself performs the fail-closed recursive placeholder gate before the
                # genesis commit; reaching this point proves no template token survived.
    except Exception as exc:
        print(f"Hub propagation: FAIL\n- {exc}", file=sys.stderr)
        return 1
    print("Hub propagation: PASS (contract + visual + throughput + bootstrap + fresh/themed init)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
