"""Which Django site package a repository ACTUALLY has, resolved structurally.

A template's package name is not a fact about a repo ported from it. Hardcoding one as the
DJANGO_SETTINGS_MODULE fallback makes every seam that skips the env var point at a module that
does not exist there: the audit's settings scan reports against a phantom path and identity
checks scan a directory that is not present — going quietly blind to the real site package
rather than failing. Proven in production on an adopter whose package name differed from the
template it was ported from.

So resolve it STRUCTURALLY: the one top-level package holding both settings.py and wsgi.py.
When that is ambiguous (none, or several), refuse — a caller that cannot be told the truth gets
an error naming what could not be resolved, never a package named on faith.

Framework-free; the caller supplies the repository root explicitly (the telemetry/cost idiom).
"""
import os
from pathlib import Path


def site_package(base) -> str:
    """The site package name under ``base``, or "" when it cannot be resolved unambiguously.

    An explicit DJANGO_SETTINGS_MODULE always wins — the structural scan is the fallback for
    seams the environment never reached, not a second opinion on a configured one.
    """
    env = os.environ.get("DJANGO_SETTINGS_MODULE")
    if env:
        return env.rsplit(".", 1)[0]
    try:
        found = sorted(p.name for p in Path(base).iterdir()
                       if p.is_dir() and (p / "settings.py").is_file() and (p / "wsgi.py").is_file())
    except OSError:
        found = []
    return found[0] if len(found) == 1 else ""


def settings_module(base) -> str:
    """``<package>.settings`` for the resolved site package; raises when ambiguous."""
    pkg = site_package(base)
    if not pkg:
        raise RuntimeError(
            "the Django site package could not be resolved under %s (no DJANGO_SETTINGS_MODULE, "
            "and no single top-level package holds settings.py + wsgi.py); "
            "set DJANGO_SETTINGS_MODULE=<site>.settings" % base)
    return pkg + ".settings"


def settings_file(base):
    """The resolved package's settings.py as a Path, or None when unresolvable — for callers
    (like the AST settings audit) that want the structural answer without an exception."""
    pkg = site_package(base)
    if not pkg:
        return None
    p = Path(base) / pkg / "settings.py"
    return p if p.is_file() else None


def apply(base) -> str:
    """setdefault DJANGO_SETTINGS_MODULE to the repo's real site package. Returns what is in force."""
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module(base)
    return os.environ["DJANGO_SETTINGS_MODULE"]
