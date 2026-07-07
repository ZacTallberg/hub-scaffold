#!/usr/bin/env python
"""Django's command-line utility for the minimal hub example site.

The example runs the scaffold IN PLACE: the scaffold root (for `hub_core`) and
`adapters/django` (for `hub`) are added to sys.path, so nothing needs copying."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCAFFOLD = BASE_DIR.parent
for _p in (str(SCAFFOLD), str(SCAFFOLD / "adapters" / "django")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example_site.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Is it installed and on PYTHONPATH?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
