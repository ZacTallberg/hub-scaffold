"""Count an app's assistant against the canon, and name what it has nothing for.

    python -m assistant.audit                 # from inside a configured Django project
    python -m assistant.audit --json

This is the enforcement mechanism, and it is a COUNT rather than a test on purpose. No test
can fail for a capability that was never written -- a suite is green precisely because the
tool does not exist. Counting is the only check whose failure mode is "you are missing
something" rather than "what you built is broken".

It reads the app's LIVE registry, so a document cannot satisfy it, and it names where it read
the registry from -- an audit that cannot name its own source is an audit nobody can check.

An app declares its deliberate gaps in settings:

    ASSISTANT_GAPS = {
        "comms": "this app has no message corpus",
        "schedule": "records here have no dependency graph",
    }

A declared gap prints as a decision; an undeclared one prints as a gap. The difference is
somebody having thought about it, and "we didn't get to it" is not a reason.

Exit status: 0 when every family is covered or declared, 1 when any is not, 2 when the
registry could not be found at all -- a registry of NOTHING is an unmeasured assistant, never a
clean one.
"""
from __future__ import annotations

import importlib
import json
import sys

from . import canon


def _registry_names() -> tuple[list[str], str]:
    """Every tool name this app registers, and where they were found.

    ``settings.ASSISTANT_TOOLS_MODULE`` wins; otherwise ``<app>.agent_tools`` for each
    installed app. A module answers through ``tool_names()``, ``build_registry()`` (anything
    with ``names()`` or iterable), or a ``SPECS``/``TOOLS`` mapping.
    """
    from django.conf import settings

    candidates = []
    if getattr(settings, "ASSISTANT_TOOLS_MODULE", ""):
        candidates.append(settings.ASSISTANT_TOOLS_MODULE)
    for app in getattr(settings, "INSTALLED_APPS", []):
        head = str(app).split(".")[0]
        if head != "django":
            candidates.append(f"{head}.agent_tools")

    for name in dict.fromkeys(c for c in candidates if c):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = str(getattr(exc, "name", "") or "")
            if missing and (name == missing or name.startswith(missing + ".")):
                continue               # this app simply has no such module
            raise                      # the module exists and one of ITS imports is missing
        fn = getattr(module, "tool_names", None)
        if callable(fn):
            return sorted(fn()), f"{name}.tool_names()"
        build = getattr(module, "build_registry", None)
        if callable(build):
            reg = build()
            names = reg.names() if hasattr(reg, "names") else list(reg)
            return sorted(names), f"{name}.build_registry()"
        for attr in ("SPECS", "TOOLS"):
            table = getattr(module, attr, None)
            if isinstance(table, dict) and table:
                return sorted(table), f"{name}.{attr}"
    return [], "nothing -- no tool registry was found"


def audit(names: list[str] | None = None) -> dict:
    """The coverage report: which families are answered, which are not, and which lanes."""
    from django.conf import settings

    source = "supplied by the caller"
    if names is None:
        names, source = _registry_names()
    declared = dict(getattr(settings, "ASSISTANT_GAPS", {}) or {})
    unknown_declared = sorted(k for k in declared if k not in canon.BY_KEY)

    covered: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for name in names:
        keys = canon.families_of(name)
        for key in keys:
            covered.setdefault(key, []).append(name)
        if not keys:
            unmatched.append(name)

    rows, gaps, decisions = [], [], []
    for fam in canon.FAMILIES:
        hits = sorted(covered.get(fam.key, []))
        row = {"family": fam.key, "title": fam.title, "tools": hits, "count": len(hits),
               "question": fam.question}
        if hits:
            row["status"] = "covered"
        elif fam.key in declared and str(declared[fam.key]).strip():
            row["status"] = "declared gap"
            row["reason"] = str(declared[fam.key]).strip()
            decisions.append(fam.key)
        else:
            row["status"] = "GAP"
            row["skippable_when"] = fam.skippable_when
            gaps.append(fam.key)
        rows.append(row)

    lanes = _lane_rows(names)
    library = _library_remedies(gaps)
    return {
        "tools": len(names),
        "read_from": source,
        "families": rows,
        "covered": len(canon.FAMILIES) - len(gaps) - len(decisions),
        "declared_gaps": decisions,
        "gaps": gaps,
        "total_families": len(canon.FAMILIES),
        # A key in ASSISTANT_GAPS that names no family declares nothing -- usually a typo,
        # which would otherwise leave the real family undeclared while the author believes
        # it is handled.
        "unknown_declarations": unknown_declared,
        "guarantees_to_confirm": [g for g, _ in canon.GUARANTEES],
        "app_specific_tools": unmatched,
        "gaps_the_library_covers": library,
        "gaps_of_your_own": [k for k in gaps if k not in dict(library)],
        # The routing half: the orchestrator can only pick a lane this app has staffed, so a
        # lane with no tools is a KIND of question this assistant will never be routed to.
        "lanes": lanes,
        "unstaffed_lanes": [row["lane"] for row in lanes if not row["count"]],
        "unrouted_families": list(canon.unrouted()),
    }


def _lane_rows(names: list[str]) -> list[dict]:
    from . import routing

    rows = []
    for lane in canon.LANES:
        tools = routing.lane_tools(lane.key, list(names))
        rows.append({"lane": lane.key, "title": lane.title, "count": len(tools),
                     "tools": list(tools[:8]), "owns": lane.owns,
                     "families": len(canon.families_in_lane(lane.key))})
    return rows


def _library_remedies(gaps: list[str]) -> list[tuple[str, str]]:
    """``(family, the source to wire)`` for each gap THIS COPY of the kit can already answer.

    Read from the registrar's own table (``families.REGISTRAR``), never from a list here: a
    second list would be wrong the first time a family module was added, and wrong in the
    costly direction -- telling an app to write from scratch what it could wire in one line.
    """
    from . import families as registrar

    covers: dict[str, str] = {}
    for module_name, needs, serves in registrar.REGISTRAR:
        for key in serves:
            covers[key] = needs[0] if needs else "the adapter alone"
    return [(k, covers[k]) for k in gaps if k in covers]


def _print(report: dict) -> int:
    print(f"\n  {report['tools']} tools, read from {report['read_from']}")
    print(f"  {report['covered']} of {report['total_families']} families covered, "
          f"{len(report['declared_gaps'])} declared as gaps, "
          f"{len(report['gaps'])} undeclared\n")
    for row in report["families"]:
        if row["status"] == "covered":
            tail = f"{row['count']:>2}  " + ", ".join(row["tools"][:5])
            if row["count"] > 5:
                tail += f", +{row['count'] - 5} more"
            mark = "  ok  "
        elif row["status"] == "declared gap":
            mark, tail = " skip ", " -  " + row["reason"]
        else:
            mark, tail = " GAP  ", " -  " + row["question"]
        print(f"  [{mark}] {row['title']:<44} {tail}")

    if report["unknown_declarations"]:
        print("\n  ASSISTANT_GAPS names families the canon does not have (a typo declares "
              "nothing): " + ", ".join(report["unknown_declarations"]))
    if report["gaps"]:
        print(f"\n  {len(report['gaps'])} UNDECLARED GAP(S): " + ", ".join(report["gaps"]))
        library, own = report["gaps_the_library_covers"], report["gaps_of_your_own"]
        if library:
            print(f"\n  {len(library)} of them this kit already implements -- wire the source "
                  "and register it:")
            for key, need in library:
                print(f"    {key:<26} families.build_all(ADAPTER, ...)  needs: {need}")
        if own:
            print(f"\n  {len(own)} have no generic implementation here: yours to write or to "
                  "declare:")
            for key in own:
                print(f"    {key:<26} {canon.BY_KEY[key].question}")
        print("\n  Declare a gap in settings.ASSISTANT_GAPS with the reason this app does not")
        print('  have that family. "We didn\'t get to it" is not a reason.')
    else:
        print("\n  No undeclared gaps.")

    staffed = [r for r in report["lanes"] if r["count"]]
    print(f"\n  ROUTING: {len(staffed)} of {len(report['lanes'])} specialist lanes are staffed "
          "by this app's tools.")
    for row in report["lanes"]:
        mark = "  ok  " if row["count"] else " NONE "
        print(f"  [{mark}] {row['title']:<44} {row['count']:>3} tools  {row['owns']}")
    if len(staffed) < 2:
        print("\n  Fewer than two staffed lanes: there is nothing to route, and every turn runs")
        print("  as one unscoped loop -- which routing.run_route does on its own.")
    if report["unrouted_families"]:
        print("\n  CANON families that name no lane (fix in canon.py, it affects every app): "
              + ", ".join(report["unrouted_families"]))

    print("\n  These cannot be counted from a registry -- confirm them yourself:")
    for key, text in canon.GUARANTEES:
        print(f"    - {key}: {text}")
    if report["app_specific_tools"]:
        n = len(report["app_specific_tools"])
        print(f"\n  {n} tool(s) matched no canonical family. Fine -- apps have their own "
              "domain --\n  but read them: a family may be hiding under a local name.")
        print("    " + ", ".join(report["app_specific_tools"][:12])
              + (f", +{n - 12} more" if n > 12 else ""))
    return 1 if (report["gaps"] or report["unknown_declarations"]) else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        import django
        django.setup()
    except Exception as exc:                                  # noqa: BLE001
        print(f"this must run inside a configured Django project ({exc})")
        return 2
    report = audit()
    if not report["tools"]:
        # A registry of nothing is not an assistant with every gap; it is an assistant this
        # audit could not find. Refuse to print a coverage table for it.
        print(f"  no tools found ({report['read_from']}). Set ASSISTANT_TOOLS_MODULE to the "
              "module that builds this app's registry.")
        return 2
    if "--json" in argv:
        print(json.dumps(report, indent=1))
        return 1 if (report["gaps"] or report["unknown_declarations"]) else 0
    return _print(report)


if __name__ == "__main__":
    raise SystemExit(main())
