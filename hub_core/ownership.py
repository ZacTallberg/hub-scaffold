"""Resolve an executable task to its owner, from the generated ownership register.

The register (`PROJECT/design/ownership-register.json`, compiled by `tools/build_owner_register.py`)
says who builds, verifies, supports and rolls back each GDD requirement family. This module answers
the inverse for a task: which family owns it, and therefore which role is responsible, who
collaborates, what evidence closes it, how support and rollback behave, and who verifies it without
having built it.

Rules applied live to the board, never a compiled table of task rows: the board gains tasks
continuously, and a committed per-task materialisation would be stale — and red — the moment the
next task is filed.

The ladder, in order, recorded per task as `owner_basis`:
  contract_ref -> the highest-priority reference that resolves to a family
  title_area   -> the `area.` prefix the board's titles carry
  work_kind    -> the last resort
A task none of the three resolves is OWNERLESS. There is deliberately no catch-all default: an
unownable task is the exact defect this register exists to surface.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = ROOT / "PROJECT/design/ownership-register.json"

# What the resolution copies onto a task from its owning family.
FAMILY_FIELDS = ("product_authority", "evidence_type", "human_gate", "support_behavior",
                 "rollback", "verifying_authority")

_CACHE = {"key": None, "value": None}


def load(path=None):
    """The register, cached on (path, mtime, size). Returns None when the file is absent — a
    missing register must not 500 the read surfaces; the governance guard is what refuses it."""
    p = Path(path) if path else REGISTER_PATH
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _CACHE["key"] != key:
        _CACHE["value"] = json.loads(p.read_text(encoding="utf-8"))
        _CACHE["key"] = key
    return _CACHE["value"]


def _rules(reg):
    return (reg or {}).get("ownership_resolution") or {}


def families_by_name(reg):
    return {f["family"]: f for f in (reg or {}).get("families", [])}


def revision_ok(reg):
    """(ok, recorded, floor) — the register's GDD revision against its own floor. A register
    compiled from a superseded GDD hands out ownership for the wrong product."""
    floor = _rules(reg).get("min_gdd_revision") or "0.0"
    rec = (reg or {}).get("gdd_revision") or "0.0"

    def t(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (0,)

    return t(rec) >= t(floor), rec, floor


def classify_ref(ref, reg):
    """(kind, family) for one contract reference. `family` is None for a board cross-link (a
    legitimate reference to no family) and for an unmapped value (a defect). The two are told
    apart by `kind`: `board_idref` versus `unmapped`."""
    r = _rules(reg)
    ref = (ref or "").strip()
    if not ref:
        return "unmapped", None
    if ref.startswith(r.get("board_idref_prefix", "ember:")):
        return "board_idref", None
    if ref in families_by_name(reg):
        return "family", ref
    for add in r.get("revision_additions", []):
        if ref == add["ref"]:
            return "revision_addition", add["primary_family"]
    screen = r.get("screen_prefix", "GDD-SCREEN:")
    if screen and ref.startswith(screen):
        return "screen", r.get("screen_family")
    wbs = r.get("wbs_prefix", "WBS:")
    if wbs and ref.startswith(wbs):
        area = ref[len(wbs):].split(".", 1)[0]
        fam = (r.get("wbs_areas") or {}).get(area)
        return ("wbs_area", fam) if fam else ("unmapped", None)
    m = re.match(r.get("roadmap_prefix_pattern") or r"^roadmap-v[0-9]+:", ref)
    if m:
        area = ref[m.end():].split(".", 1)[0]
        fam = (r.get("roadmap_areas") or {}).get(area)
        return ("roadmap_area", fam) if fam else ("unmapped", None)
    fam = (r.get("legacy_aliases") or {}).get(ref)
    if fam:
        return "legacy_alias", fam
    return "unmapped", None


def is_operative(task):
    """An operative task is one somebody is expected to execute. Dropped and duplicate rows are
    board bookkeeping and carry no owner."""
    non = {"status": ["dropped"], "disposition": ["drop"], "work_kind": ["duplicate"]}
    for field, bad in non.items():
        if (task or {}).get(field) in bad:
            return False
    return True


def _title_family(task, reg):
    r = _rules(reg)
    m = re.match(r.get("title_area_pattern") or r"^([a-z0-9-]+)\.", (task.get("title") or ""))
    return (r.get("title_areas") or {}).get(m.group(1)) if m else None


def resolve(task, reg):
    """The full ownership row for one task, or None when nothing resolves it.

    `owner_families` lists every family the task's references touch; `primary_owner_family` is the
    one that decides the owner — so a multi-family task declares a primary owner instead of
    becoming ownerless."""
    if reg is None:
        return None
    fams = families_by_name(reg)
    priority = _rules(reg).get("reference_priority") or []
    hits, touched = [], []
    for ref in (task.get("contract_refs") or []):
        kind, fam = classify_ref(ref, reg)
        if not fam or fam not in fams:
            continue
        rank = priority.index(kind) if kind in priority else len(priority)
        # Family order inside a priority class, so the answer never depends on the order the refs
        # happen to be listed in.
        hits.append((rank, list(fams).index(fam), fam, ref))
        if fam not in touched:
            touched.append(fam)

    if hits:
        _, _, primary, ref = min(hits)
        basis, basis_detail = "contract_ref", ref
    else:
        primary = _title_family(task, reg)
        basis, basis_detail = "title_area", (task.get("title") or "").split(".", 1)[0]
        if not primary:
            primary = (_rules(reg).get("work_kind_families") or {}).get(task.get("work_kind"))
            basis, basis_detail = "work_kind", task.get("work_kind")
        if not primary:
            return None
        touched = [primary]

    fam = fams[primary]
    row = {
        "owner_role": fam["responsible_domain"],
        "collaborator_roles": list(fam.get("collaborators") or []),
        "primary_owner_family": primary,
        "owner_families": touched,
        "owner_basis": basis,
        "owner_basis_detail": basis_detail,
    }
    row.update({k: fam.get(k) for k in FAMILY_FIELDS})
    return row


def decorate(tasks, reg):
    """Shallow-merge each task row with its ownership. Tasks that are not operative are left
    alone; an ownerless operative task is marked as such rather than silently skipped, so the
    read surfaces show the hole the guard refuses."""
    if reg is None:
        return tasks
    out = []
    for t in tasks:
        if not is_operative(t):
            out.append(t)
            continue
        row = resolve(t, reg)
        out.append(dict(t, **row) if row else dict(t, ownerless=True))
    return out


def audit(tasks, reg, register_path=None):
    """Every complaint about ownership across a board, as (code, subject) records.

    Codes: `register-missing`, `revision-stale`, `ownerless`, `self-certified`, `ref-unmapped`,
    `family-unknown`. Structured so the fires-on-positive pass matches an exact finding instead of
    a substring."""
    bad = []
    if reg is None:
        return [("register-missing", str(register_path or REGISTER_PATH))]
    ok, rec, floor = revision_ok(reg)
    if not ok:
        bad.append(("revision-stale", f"register at GDD {rec}, floor is {floor}"))
    fams = families_by_name(reg)
    for tid, task in sorted(tasks.items()):
        if not is_operative(task):
            continue
        for ref in (task.get("contract_refs") or []):
            kind, fam = classify_ref(ref, reg)
            if kind == "unmapped":
                bad.append(("ref-unmapped", f"{tid} -> {ref}"))
            elif fam and fam not in fams:
                bad.append(("family-unknown", f"{tid} -> {ref} -> {fam}"))
        row = resolve(task, reg)
        if not row:
            bad.append(("ownerless", tid))
        elif row["owner_role"] == row["verifying_authority"]:
            bad.append(("self-certified", f"{tid} -> {row['owner_role']}"))
    return bad
