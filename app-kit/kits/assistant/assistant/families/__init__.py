"""Every generic family this copy of the kit ships, wired in one call.

    from assistant import families
    SPECS, FUNCTIONS = families.build_all(ADAPTER)
    families.register_all(SPECS, FUNCTIONS, register)

``REGISTRAR`` is the one table of what exists here: ``(module, what it needs beyond the adapter,
which canon families it serves)``. ``assistant.audit`` reads it to put the remedy beside a gap
("this kit already implements it -- wire the source"), so it is never a second list to keep in
step.

**A family an app cannot support is SKIPPED, not failed**, and the skip is named in
``build_all.skipped`` -- a family that silently registers nothing looks exactly like one that
works and finds no results.

**A per-user entity that declares no scope is LEFT OUT**, fail-closed: an entity with an owner
column (``adapter.OWNER_FIELDS``) and no ``Entity.scope`` gets no generic tool at all and is named
in ``skipped`` as ``entity:<key>``. An app that re-vendors the kit without declaring scopes loses
those tools rather than showing every user's rows to every user.

This copy ships the ``core`` family (reads, counts, breakdowns over declared entities). The rest
of the canon's families are app-owned: write them against the same adapter, add a row here when
one becomes generic, and the audit starts crediting it the same day.
"""
from __future__ import annotations

import importlib
import inspect
import logging

logger = logging.getLogger(__name__)

REGISTRAR: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("core", (), ("core_reads", "primitives")),
)


def build_all(adapter, **sources):
    """``(SPECS, FUNCTIONS)`` for every family this app can support.

    A NAME COLLISION IS FATAL, not a merge: two families answering to one name means one is
    unreachable, and an unreachable tool is worse than a missing one -- the catalog advertises
    it and calling it gets the other thing.
    """
    import dataclasses

    from django.apps import apps

    from ..adapter import check, unscoped_owner

    skipped: list[tuple[str, str]] = []
    keep = []
    for entity in adapter.entities:
        try:
            owner = unscoped_owner(entity, apps.get_model(entity.model))
        except Exception:                                     # noqa: BLE001
            owner = ""                                        # check() names a bad model below
        if owner:
            skipped.append((f"entity:{entity.key}",
                            f"UNSCOPED: has an owner column ({owner!r}) and no scope, so no "
                            "generic tool is built over it -- declare Entity(scope=...) or "
                            "scope=SHARED"))
            logger.error("assistant entity %s left out: owner column %r, no scope",
                         entity.key, owner)
        else:
            keep.append(entity)
    if len(keep) != len(adapter.entities):
        adapter = dataclasses.replace(adapter, entities=tuple(keep))

    problems = check(adapter)
    if problems:
        raise ValueError("the assistant adapter is not usable:\n  - " + "\n  - ".join(problems))
    specs: dict = {}
    functions: dict = {}
    for name, needs, _serves in REGISTRAR:
        missing = [n for n in needs if not sources.get(n)]
        if missing:
            skipped.append((name, f"this app supplies no {', '.join(missing)}"))
            continue
        try:
            module = importlib.import_module(f".{name}", __package__)
        except ModuleNotFoundError as exc:
            # Two failures wear this exception with opposite remedies: the family module is
            # absent (an app took a subset -- skip it), or it IS here and something it imports
            # is not. Reporting the second as "not in this copy" sends somebody looking for a
            # file that is sitting right there.
            absent = str(getattr(exc, "name", "") or "")
            if absent in (f"{__package__}.{name}", name):
                skipped.append((name, "not in this copy of the kit"))
            else:
                logger.exception("assistant family %s: missing dependency", name)
                skipped.append((name, f"is here but cannot import: no module named {absent!r}"))
            continue
        params = inspect.signature(module.build).parameters
        kwargs = {k: v for k, v in sources.items() if k in params}
        try:
            family_specs, family_functions = module.build(adapter, **kwargs)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("assistant family %s failed to build", name)
            skipped.append((name, f"failed to build: {type(exc).__name__}: {exc}"))
            continue
        clash = set(family_specs) & set(specs)
        if clash:
            raise RuntimeError(f"the {name} family reuses tool name(s) {sorted(clash)}; one "
                               "would be unreachable. Rename one.")
        drift = set(family_specs) ^ set(family_functions)
        if drift:
            raise RuntimeError(f"the {name} family's specs and functions disagree about "
                               f"{sorted(drift)}: a spec with no function is a name the model "
                               "calls and gets nothing from.")
        specs.update(family_specs)
        functions.update(family_functions)
    build_all.skipped = skipped
    return specs, functions


def register_all(specs, functions, register) -> int:
    """Hand every built tool to the app's registry through ``register(name, description,
    parameters, fn)``. The generic families are all READS; an app's own WRITE verbs are
    registered by the app, because what they cost is a decision the kit must not make for it.
    Returns how many were registered."""
    for name, (description, parameters) in specs.items():
        register(name, description, parameters, functions[name])
    return len(specs)
