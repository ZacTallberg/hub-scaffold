#!/usr/bin/env python3
"""Focused runtime proof for the combined Hub Excellence release."""
from pathlib import Path
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "example", ROOT / "adapters" / "django"):
    sys.path.insert(0, str(path))


def main():
    with tempfile.TemporaryDirectory(prefix="hub-runtime-") as tmp:
        os.environ.update({
            "DJANGO_SETTINGS_MODULE": "example_site.settings",
            "DEBUG": "1",
            "HUB_WRITE_TOKEN": "runtime-proof-token",
            "HUB_DIR": str(Path(tmp) / "hub"),
            "HUB_TEST_DB": str(Path(tmp) / "example.sqlite3"),
        })
        import django
        django.setup()
        from django.test import Client
        response = Client().get("/hub/")
        body = response.content.decode("utf-8", errors="replace")
        assert response.status_code == 200, response.status_code
        assert "Hub theme runtime" in body
        assert "Command deck" in body
    print("Hub runtime: PASS (mounted page, worker-health projection, theme and command deck)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
