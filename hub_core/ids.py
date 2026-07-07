"""Stable opaque id allocation. Numeric types (task/adr/gap/deploy) get zero-padded monotonic
locals from a high-water mark; dotted types (feat/cap) use caller-supplied validated slugs.
IDs are allocated once and never reused/renumbered."""
import re

_NUMERIC = {"task", "adr", "gap", "deploy"}
_PAD = 4
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*:(task|adr|feat|gap|cap|deploy|note):[a-z0-9][a-z0-9._-]*$")


def valid_id(s) -> bool:
    return bool(ID_RE.match(s or ""))


def _local(entity_id: str) -> str:
    parts = entity_id.split(":")
    return parts[2] if len(parts) >= 3 else ""


def high_water(entities, project: str, type_: str) -> int:
    hi = 0
    pref = f"{project}:{type_}:"
    for eid in entities:
        if eid.startswith(pref):
            loc = _local(eid)
            if loc.isdigit():
                hi = max(hi, int(loc))
    return hi


def next_id(entities, project: str, type_: str) -> str:
    if type_ not in _NUMERIC:
        raise ValueError(f"{type_} ids use validated slugs, not auto-numbering")
    return f"{project}:{type_}:{high_water(entities, project, type_) + 1:0{_PAD}d}"


def make_id(project: str, type_: str, local: str) -> str:
    cid = f"{project}:{type_}:{local}"
    if not valid_id(cid):
        raise ValueError(f"invalid id {cid!r}")
    return cid
