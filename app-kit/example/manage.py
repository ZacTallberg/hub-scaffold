#!/usr/bin/env python
"""The app-kit example: a small budget app assembled from the kits (see ../README.md)."""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
