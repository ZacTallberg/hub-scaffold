"""hub_core.materialize — Phase 2 ingest: turn a REVIEW-AND-REIMPL-PLAN.md gap ledger into `gap`
entities in the event log. Format-robust (finds the priority column by value, not position, so it
works across every project's table layout). Doc stays the source of truth; idempotent on legacy_ref.
Pure stdlib; identical across all hubs."""
import re

from . import ids
from .validate import validate

_SEV = {"P0", "P1", "P2", "P3"}
_GID = re.compile(r"^\|\s*\*{0,2}([A-Z]{1,3}\d+)\*{0,2}\s*\|")
_SIZE = re.compile(r"^[SMLX]+(\s*\(.*\))?$")
_EVID = re.compile(r"`[^`]+`|\.(py|ts|tsx|js|rs|html|json|toml|sh)\b|:\d")


def _clean(s):
    return re.sub(r"\*\*(.+?)\*\*", r"\1", (s or "").strip()).strip()


def parse_gap_rows(text):
    """Yield dicts {gid, title, severity, size, evidence} for each gap-ledger row, any column order."""
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _GID.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        gid = m.group(1)
        title = _clean(cells[1]) if len(cells) > 1 else gid
        rest = cells[2:]
        sev = next((c for c in rest if c in _SEV), "P2")
        size = next((c for c in rest if _SIZE.match(c)), "")
        evid = next((c for c in rest if _EVID.search(c)), "")
        note = _clean(rest[-1]) if rest else ""
        if note and note not in (evid, sev, size) and note != _clean(evid):
            evidence = (evid + " -- " + note) if evid else note
        else:
            evidence = evid
        yield {"gid": gid, "title": title, "severity": sev, "size": size, "evidence": _clean(evidence)}


_ADR_HDR = re.compile(r"ADRs?\s+to\s+add", re.I)
_ADR_NUM = re.compile(r"(\d{3,4})\s+([^;.]+)")


def parse_proposed_adrs(text):
    """Yield (number, title) from a 'ADRs to add: 0011 x; 0012 y' line. Cuts the 'Amend NNNN' clause
    (those are amendments to existing ADRs, not new ones)."""
    for line in text.splitlines():
        if not _ADR_HDR.search(line):
            continue
        body = re.split(r"add:?\*{0,2}", line, flags=re.I, maxsplit=1)[-1]
        body = re.split(r"\*{0,2}Amend", body, maxsplit=1)[0]   # drop "**Amend** 0006 ..."
        for m in _ADR_NUM.finditer(body):
            title = _clean(m.group(2)).strip(" -*")
            if title:
                yield int(m.group(1)), title


def materialize_proposed_adrs(store, reg, existing, project_key, git_head, review_text, dry_run=False):
    """Append proposed ADRs named in the REVIEW plan (idempotent; skips numbers that already exist)."""
    made = {"adr": 0, "skip": 0, "reject": 0, "rejects": []}
    for num, title in parse_proposed_adrs(review_text):
        nid = ids.make_id(project_key, "adr", "%04d" % num)
        if nid in existing:
            made["skip"] += 1
            continue
        payload = {"id": nid, "type": "adr", "number": num, "title": title[:200], "status": "proposed",
                   "context_md": "Proposed in REVIEW-AND-REIMPL-PLAN.md (Phase 2 materialization); to be authored.",
                   "legacy_ref": "adr:%04d" % num, "version": 1}
        errs = validate(payload, "adr", reg)
        if errs:
            made["reject"] += 1
            made["rejects"].append("ADR-%04d: %s" % (num, errs[0]))
            continue
        if not dry_run:
            store.append(aggregate=nid, type="adr.proposed", payload=payload, expected_version=0,
                         idem_key="materialize:adr:%04d" % num, agent_id="hubmaterialize", git_sha=git_head)
        existing[nid] = payload
        made["adr"] += 1
    return made


def _local_from_legacy(legacy, fallback):
    s = (legacy or "").split(":", 1)[-1] or fallback
    s = re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-.")
    return s[:48] or "x"


def materialize_manifest(store, reg, existing, project_key, git_head, manifest, dry_run=False):
    """Ingest a Phase-2 MoE manifest deterministically. Validates against schema; drops task/adr refs
    that don't point to a real task/adr entity (anti-hallucination); demotes orphan shipped feats to
    'planned' (no false-green); reassigns ADR numbers gap-free; idempotent on legacy_ref. Returns counts."""
    seen = {e.get("legacy_ref") for e in existing.values() if e.get("legacy_ref")}
    by_legacy = {e.get("legacy_ref"): e for e in existing.values() if e.get("legacy_ref")}
    adr_max = max([e.get("number", 0) for e in existing.values() if e.get("type") == "adr"] + [0])
    c = {"feat": 0, "gap": 0, "cap": 0, "adr": 0, "link": 0, "skip": 0, "reject": 0,
         "demoted": 0, "droppedrefs": 0, "rejects": []}

    def emit(nid, type_, payload, legacy, evkind):
        errs = validate(payload, type_, reg)
        if errs:
            c["reject"] += 1
            c["rejects"].append("%s %s: %s" % (type_, legacy, errs[0]))
            return
        if not dry_run:
            store.append(aggregate=nid, type=evkind, payload=payload, expected_version=0,
                         idem_key="manifest:" + legacy, agent_id="phase2-moe", git_sha=git_head)
        existing[nid] = payload
        seen.add(legacy)
        c[type_] += 1

    def refs_of(ids_list, want_type):
        return [x for x in (ids_list or []) if existing.get(x, {}).get("type") == want_type]

    for f in manifest.get("feats", []):
        legacy = f.get("legacy_ref") or ("feat:" + f.get("name", "x")[:40])
        if legacy in seen:
            c["skip"] += 1
            continue
        nid = ids.make_id(project_key, "feat", _local_from_legacy(legacy, f.get("name", "x")))
        if nid in existing:
            c["skip"] += 1
            continue
        tasks = refs_of(f.get("task_ids"), "task")
        if len(tasks) < len(f.get("task_ids") or []):
            c["droppedrefs"] += 1
        adrs = refs_of(f.get("adr_ids"), "adr")
        status = f.get("status", "planned")
        if status in ("shipped", "partial") and not tasks:
            status = "planned"
            c["demoted"] += 1
        payload = {"id": nid, "type": "feat", "name": f.get("name", "?")[:200], "status": status,
                   "legacy_ref": legacy, "version": 1}
        if f.get("summary"):
            payload["summary"] = f["summary"][:800]
        if tasks:
            payload["tasks"] = tasks
        if adrs:
            payload["adrs"] = adrs
        emit(nid, "feat", payload, legacy, "feat.materialized")

    for g in manifest.get("gaps", []):
        legacy = g.get("legacy_ref") or ("gap:" + g.get("title", "x")[:40])
        if legacy in seen:
            c["skip"] += 1
            continue
        nid = ids.make_id(project_key, "gap", _local_from_legacy(legacy, g.get("title", "x")))
        if nid in existing:
            c["skip"] += 1
            continue
        sev = g.get("severity") if g.get("severity") in _SEV else "P2"
        payload = {"id": nid, "type": "gap", "title": g.get("title", "?")[:300], "status": "open",
                   "severity": sev, "evidence": (g.get("evidence") or "")[:600],
                   "source": (g.get("source") or "MoE")[:120], "legacy_ref": legacy, "version": 1}
        emit(nid, "gap", payload, legacy, "gap.identified")

    for cap in manifest.get("caps", []):
        legacy = cap.get("legacy_ref") or ("cap:" + cap.get("name", "x")[:40])
        if legacy in seen:
            c["skip"] += 1
            continue
        nid = ids.make_id(project_key, "cap", _local_from_legacy(legacy, cap.get("name", "x")))
        if nid in existing:
            c["skip"] += 1
            continue
        mat = cap.get("maturity") if cap.get("maturity") in {"reusable", "prototype", "concept"} else "concept"
        payload = {"id": nid, "type": "cap", "name": cap.get("name", "?")[:120], "maturity": mat,
                   "pivot_notes": (cap.get("pivot_notes") or "")[:600], "needs": (cap.get("needs") or "")[:500],
                   "legacy_ref": legacy, "version": 1}
        emit(nid, "cap", payload, legacy, "capability.registered")

    for a in manifest.get("proposed_adrs", []):
        legacy = a.get("legacy_ref") or ("adr:" + a.get("title", "x")[:40])
        if legacy in seen:
            c["skip"] += 1
            continue
        adr_max += 1
        nid = ids.make_id(project_key, "adr", "%04d" % adr_max)
        while nid in existing:
            adr_max += 1
            nid = ids.make_id(project_key, "adr", "%04d" % adr_max)
        payload = {"id": nid, "type": "adr", "number": adr_max, "title": a.get("title", "?")[:200],
                   "status": "proposed",
                   "context_md": "Proposed via Phase-2 MoE materialization; to be authored.",
                   "legacy_ref": legacy, "version": 1}
        emit(nid, "adr", payload, legacy, "adr.proposed")

    for link in manifest.get("gap_task_links", []):
        ent = by_legacy.get(link.get("gap_legacy_ref"))
        if not ent or ent.get("type") != "gap":
            c["skip"] += 1
            continue
        tids = refs_of(link.get("task_ids"), "task")
        cur = ent.get("addressed_by") or []
        merged = sorted(set(cur) | set(tids))
        if not tids or merged == sorted(cur):
            c["skip"] += 1
            continue
        cand = dict(ent)
        cand["addressed_by"] = merged
        errs = validate(cand, "gap", reg)
        if errs:
            c["reject"] += 1
            c["rejects"].append("link %s: %s" % (link.get("gap_legacy_ref"), errs[0]))
            continue
        if not dry_run:
            store.append(aggregate=ent["id"], type="gap.addressed_by.set",
                         payload={"id": ent["id"], "type": "gap", "addressed_by": merged},
                         expected_version=ent.get("version", 1),
                         idem_key="manifest-link:" + link.get("gap_legacy_ref") + ":" + ",".join(merged),
                         agent_id="phase2-moe", git_sha=git_head)
        ent["addressed_by"] = merged
        c["link"] += 1
    return c


def materialize_gaps(store, reg, existing, project_key, git_head, review_text, dry_run=False):
    """Append missing gaps (idempotent on legacy_ref 'gap:<GID>'). Returns counts dict."""
    seen = {e.get("legacy_ref") for e in existing.values() if e.get("legacy_ref")}
    made = {"gap": 0, "skip": 0, "reject": 0, "rejects": []}
    for row in parse_gap_rows(review_text):
        legacy = "gap:" + row["gid"]
        if legacy in seen:
            made["skip"] += 1
            continue
        ev = row["evidence"]
        if row["size"]:
            ev = (ev + "  [size:%s]" % row["size"]).strip()
        nid = ids.make_id(project_key, "gap", row["gid"].lower())
        payload = {"id": nid, "type": "gap", "title": row["title"][:300], "status": "open",
                   "severity": row["severity"], "evidence": ev[:600],
                   "source": "REVIEW-" + row["gid"], "legacy_ref": legacy, "version": 1}
        errs = validate(payload, "gap", reg)
        if errs:
            made["reject"] += 1
            made["rejects"].append("%s: %s" % (row["gid"], errs[0]))
            continue
        if not dry_run:
            store.append(aggregate=nid, type="gap.identified", payload=payload, expected_version=0,
                         idem_key="materialize:" + legacy, agent_id="hubmaterialize", git_sha=git_head)
        existing[nid] = payload
        seen.add(legacy)
        made["gap"] += 1
    return made
