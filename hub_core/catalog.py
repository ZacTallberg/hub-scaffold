"""Standard components and app skeletons, resolved from the capability graph on every read.

A **component** is a ``cap`` of ``kind: component`` — a building block any app can adopt, with
how to get it (``get``, ``entry``, ``delivery``), when it fits (``when``), a live ``exemplar``,
and what must already be in place (``depends_on``). A **skeleton** is a ``cap`` of
``kind: skeleton`` that names the components a new app starts from — ``applies`` (in the order
given) or ``applies_all`` (every component, placed after everything it depends on) — and
NEVER restates how to install one. That is the whole point: a fix to a component's own
``get``, or a newly registered component, reaches every skeleton on the next read, instead of
a copy of the steps drifting inside each skeleton.

Two refusals to guess, both reported rather than hidden:

- A skeleton naming something that is not a registered component keeps the name, marked
  ``found: false`` and listed under ``missing``. A skeleton that silently lost a component
  would build apps without it and nobody would see the gap until the app shipped.
- ``applies_all`` orders by ``depends_on``. A dependency on something that is not a
  component, or a cycle, is named in ``order_problems``; the components involved still appear
  (after the ones that resolved cleanly, in registration order), so nothing is dropped and the
  skeleton says its order is unproven instead of presenting a guess as the plan.

Pure and standard-library only: it reads a folded state and returns plain dicts.
"""

from __future__ import annotations

#: What a skeleton carries of each component it applies: enough to APPLY it, to know how it
#: arrives, whether it fits and what must come first — never a copy of the whole entry.
APPLIED_FIELDS = ("delivery", "hosted_at", "get", "entry", "exemplar", "when", "depends_on")

#: Older spellings a caller may still send for ``kind``.
KIND_ALIASES = {"template": "skeleton", "templates": "skeleton", "skeletons": "skeleton",
                "components": "component"}


def canonical_kind(kind) -> str:
    kind = str(kind or "").strip().lower()
    return KIND_ALIASES.get(kind, kind)


def _order_key(entity) -> tuple:
    """Registration order: when the entity was first created, then its id."""
    created = str(((entity or {}).get("provenance") or {}).get("created_at") or "")
    return (created, str((entity or {}).get("id") or ""))


def components(state) -> list:
    """Every registered component, in registration order."""
    ents = (state or {}).get("entities") or {}
    rows = [e for e in ents.values()
            if isinstance(e, dict) and e.get("type") == "cap" and e.get("kind") == "component"]
    return sorted(rows, key=_order_key)


def skeletons(state) -> list:
    ents = (state or {}).get("entities") or {}
    rows = [e for e in ents.values()
            if isinstance(e, dict) and e.get("type") == "cap" and e.get("kind") == "skeleton"]
    return sorted(rows, key=_order_key)


def _deps(entity) -> list:
    value = (entity or {}).get("depends_on")
    return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []


def dependency_order(comps) -> tuple:
    """Each component placed after every component it depends on; ties keep the given order.

    Returns ``(ordered, problems)``. A dependency that is not a component, or a cycle, is a
    PROBLEM, never a silent guess; the components involved are still returned."""
    by_id = {c.get("id"): c for c in comps}
    problems = []
    for c in comps:
        for dep in _deps(c):
            if dep not in by_id:
                problems.append("%s depends on %s, which is not a registered component"
                                % (c.get("name") or c.get("id"), dep))
    placed, ordered, pending = set(), [], list(comps)
    while pending:
        ready = [c for c in pending
                 if all(d in placed or d not in by_id for d in _deps(c))]
        if not ready:
            problems.append("the dependencies of %s form a cycle"
                            % ", ".join(str(c.get("name") or c.get("id")) for c in pending))
            ordered.extend(pending)
            break
        head = ready[0]
        ordered.append(head)
        placed.add(head.get("id"))
        pending.remove(head)
    return ordered, problems


def resolve_skeleton(skeleton, comps) -> dict:
    """The skeleton with ``applied`` (each component it applies, resolved NOW), ``missing`` and
    ``order_problems``."""
    by_id = {c.get("id"): c for c in comps}
    out = dict(skeleton)
    problems = []
    if skeleton.get("applies_all") is True:
        ordered, problems = dependency_order(comps)
        applies = [c.get("id") for c in ordered]
    else:
        value = skeleton.get("applies")
        applies = [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []
    applied = []
    for ref in applies:
        comp = by_id.get(ref)
        row = {"id": ref, "found": comp is not None}
        if comp is not None:
            row["name"] = comp.get("name") or ref
            row.update({k: comp[k] for k in APPLIED_FIELDS if comp.get(k)})
        applied.append(row)
    out["applied"] = applied
    out["missing"] = [r["id"] for r in applied if not r["found"]]
    out["order_problems"] = problems
    return out


def catalog(state, kind=None) -> dict:
    """The read model: components and resolved skeletons (optionally one kind only)."""
    kind = canonical_kind(kind)
    comps = components(state)
    out = {}
    if kind in ("", "component"):
        out["components"] = comps
    if kind in ("", "skeleton"):
        out["skeletons"] = [resolve_skeleton(s, comps) for s in skeletons(state)]
    return out
