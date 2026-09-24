#!/usr/bin/env python3
"""kits.py -- record what the app-kit ships, vendor a kit into an app, and say whether it drifted.

    python app-kit/tools/kits.py record                  # re-derive app-kit/kits.json (fails closed)
    python app-kit/tools/kits.py list                    # every kit, its state and sha
    python app-kit/tools/kits.py add <kit> <app-dir>     # vendor a kit, with provenance
    python app-kit/tools/kits.py status <app-dir>        # current / stale / edited / retired

PROVENANCE IS A RECORD, NOT A MEMORY. Each kit is recorded in ``kits.json`` with a sha per file
and one sha over the kit (sha1 of the sorted ``path\\0sha`` pairs, line endings normalised so a
checkout's CRLF conversion is not "drift"). ``add`` writes the same numbers into the app's
``.app-kit.json``, so "is this copy current, and has anyone edited it?" is answered by
comparison, never by somebody remembering where a file came from.

FAIL CLOSED, EVERY PROBLEM AT ONCE. ``record`` refuses to write a manifest when a kit is not
self-sufficient -- a Python import that resolves neither inside the kit, to the standard library
or Django, nor to a seam declared in the kit's ``REQUIRES.md``; a ``{% static %}`` or
``{% include %}`` whose file the kit does not ship; a doc that types the bar's size instead of
quoting the tool; a documentation pointer to a kit path that does not exist -- and it lists every
problem in one run rather than one per attempt. A run that found no kits at all measured nothing
and exits 2.

``add`` refuses: a stale app-kit checkout (behind its upstream -- vendoring an old kit is how a
fixed bug comes back), a manifest that no longer matches the files (run ``record``), a RETIRED
kit (it names the replacement), and an app copy with local edits (they would be overwritten --
``--force`` keeps a backup). The swap is staged: the new copy is assembled beside the app, old
entries are moved aside, and if anything fails the old copy is put back.

Stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gitstate import StaleCheckout, refuse_if_behind, stale_note  # noqa: E402

KIT_ROOT = Path(__file__).resolve().parent.parent
KITS_DIR = KIT_ROOT / "kits"
MANIFEST = KIT_ROOT / "kits.json"
PROVENANCE = ".app-kit.json"
META = "KIT.json"
DOCS = ("README.md", "REQUIRES.md", META)
TEXT = {".py", ".html", ".css", ".js", ".md", ".json", ".txt"}
ALLOWED_TOP = {"django", "__future__"}
STATES = ("active", "retired")


def file_sha(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix in TEXT:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha1(data).hexdigest()


def kit_sha(files: dict) -> str:
    h = hashlib.sha1()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode())
    return h.hexdigest()


def kit_files(kit_dir: Path) -> dict:
    return {str(p.relative_to(kit_dir)).replace("\\", "/"): file_sha(p)
            for p in sorted(kit_dir.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def declared_seams(kit_dir: Path) -> set[str]:
    """Names the kit's REQUIRES.md declares as supplied by the app: backticked names in bullets."""
    req = kit_dir / "REQUIRES.md"
    if not req.is_file():
        return set()
    names = set()
    for line in req.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("- "):
            names |= set(re.findall(r"`([A-Za-z_][\w./-]*)`", line.split(" -- ")[0]))
    return names


# ---------------------------------------------------------------- self-sufficiency checks
def _module_roots(kit_dir: Path) -> set[str]:
    roots = set()
    for p in kit_dir.iterdir():
        if p.is_dir() and (p / "__init__.py").exists():
            roots.add(p.name)
        elif p.suffix == ".py":
            roots.add(p.stem)
    return roots


def import_problems(kit: str, kit_dir: Path) -> list[str]:
    roots, seams = _module_roots(kit_dir), declared_seams(kit_dir)
    stdlib = set(sys.stdlib_module_names)
    problems = []
    for py in sorted(kit_dir.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(kit_dir).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            problems.append(f"{kit}/{rel}: does not parse ({exc.msg}, line {exc.lineno})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            elif isinstance(node, ast.ImportFrom):
                base = py.parent
                for _ in range(node.level - 1):
                    base = base.parent
                target = base.joinpath(*(node.module or "").split(".")) if node.module else base
                if not (target.with_suffix(".py").exists() or (target / "__init__.py").exists()
                        or (not node.module and base.exists())):
                    problems.append(f"{kit}/{rel}:{node.lineno}: relative import "
                                    f"{'.' * node.level}{node.module or ''} leaves the kit")
                continue
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top in ALLOWED_TOP or top in roots or top in seams \
                        or name in seams:
                    continue
                problems.append(f"{kit}/{rel}:{node.lineno}: imports {name!r}, which is neither "
                                "in the kit, the standard library, Django, nor declared in "
                                "REQUIRES.md")
    return problems


def reference_problems(kit: str, kit_dir: Path) -> list[str]:
    seams = declared_seams(kit_dir)
    shipped_static = {p.relative_to(s).as_posix() for s in kit_dir.rglob("static") if s.is_dir()
                      for p in s.rglob("*") if p.is_file()}
    shipped_tpl = {p.relative_to(t).as_posix() for t in kit_dir.rglob("templates") if t.is_dir()
                   for p in t.rglob("*") if p.is_file()}
    problems = []
    for html in sorted(kit_dir.rglob("*.html")):
        text = html.read_text(encoding="utf-8")
        rel = html.relative_to(kit_dir).as_posix()
        for ref in re.findall(r"\{%\s*static\s+['\"]([^'\"]+)['\"]", text):
            if ref not in shipped_static and ref not in seams:
                problems.append(f"{kit}/{rel}: {{% static '{ref}' %}} is not shipped by the kit")
        for m in re.finditer(r"\{#(.*?)#\}", text, re.S):
            if "\n" in m.group(1):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{kit}/{rel}:{line}: a multi-line {{# #}} comment renders as page "
                                "text -- Django comments are single-line; use {% comment %}")
        for ref in re.findall(r"\{%\s*(?:include|extends)\s+['\"]([^'\"]+)['\"]", text):
            if ref not in shipped_tpl and ref not in seams:
                problems.append(f"{kit}/{rel}: template '{ref}' is not shipped by the kit")
    return problems


def doc_problems(kits: dict) -> list[str]:
    """Docs must point at kits that exist and must not TYPE the bar's size."""
    problems = []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app_audit                                            # noqa: E402

    size = app_audit.counts()
    docs = sorted(KIT_ROOT.glob("*.md")) + sorted(KITS_DIR.glob("*/README.md"))
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(KIT_ROOT).as_posix()
        for ref in sorted(set(re.findall(r"\bkits/([a-z][a-z0-9-]*)", text))):
            if ref not in kits:
                problems.append(f"{rel}: points at kits/{ref}, which does not exist")
        for n, what in re.findall(r"\b(\d+)\s+(rules|MUST rules|dimensions)\b", text):
            key = {"rules": "rules", "MUST rules": "must", "dimensions": "dimensions"}[what]
            if int(n) != size[key]:
                problems.append(f"{rel}: says '{n} {what}' but the audit defines "
                                f"{size[key]} -- quote `app_audit.py --count`, do not type it")
    # BAR.md is the prose of the rule table: every rule listed once, MUST marked exactly where
    # the code marks it. The table lives in the code; the prose must agree with it.
    bar = KIT_ROOT / "BAR.md"
    if bar.is_file():
        listed = dict(re.findall(r"^- \*\*([a-z0-9-]+)\*\*( \(MUST\))?", bar.read_text(
            encoding="utf-8"), re.M))
        for r in app_audit.RULES:
            if r.id not in listed:
                problems.append(f"BAR.md: does not describe rule {r.id}")
            elif bool(listed[r.id]) != r.must:
                problems.append(f"BAR.md: marks {r.id} as {'MUST' if listed[r.id] else 'not MUST'}"
                                f" but the audit says {'MUST' if r.must else 'not MUST'}")
        for rid in sorted(set(listed) - {r.id for r in app_audit.RULES}):
            problems.append(f"BAR.md: describes {rid}, which the audit does not define")
    return problems


def record() -> int:
    kits, problems = {}, []
    for kit_dir in sorted(p for p in KITS_DIR.iterdir() if p.is_dir()):
        kit = kit_dir.name
        meta_path = kit_dir / META
        if not meta_path.is_file():
            problems.append(f"{kit}: no {META} (state, summary, installs)")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        if meta.get("state") not in STATES:
            problems.append(f"{kit}: {META} state must be one of {STATES}")
        if meta.get("state") == "retired" and not meta.get("replaced_by"):
            problems.append(f"{kit}: a retired kit must name replaced_by")
        if not (kit_dir / "README.md").is_file():
            problems.append(f"{kit}: no README.md")
        installs = meta.get("installs") or []
        for entry in installs:
            if not (kit_dir / entry).exists():
                problems.append(f"{kit}: installs {entry!r}, which the kit does not contain")
        if not installs:
            problems.append(f"{kit}: {META} names nothing to install")
        problems += import_problems(kit, kit_dir) + reference_problems(kit, kit_dir)
        files = kit_files(kit_dir)
        kits[kit] = {"state": meta.get("state"), "replaced_by": meta.get("replaced_by", ""),
                     "summary": meta.get("summary", ""), "installs": installs,
                     "kit_sha": kit_sha(files), "files": files}
    if not kits:
        print(f"RECORD_REFUSED: no kits found under {KITS_DIR} -- a record of nothing is not a "
              "record")
        return 2
    # A retirement pointer is an instruction `add` and `status` print to a person: it must name
    # a kit that exists and can be vendored, or it sends them to nothing.
    for name, k in kits.items():
        target = k["replaced_by"]
        if k["state"] != "retired" or not target:
            continue
        if target == name or target not in kits:
            problems.append(f"{name}: replaced_by {target!r} is not a kit under {KITS_DIR.name}/")
        elif kits[target]["state"] != "active":
            problems.append(f"{name}: replaced_by {target!r} is {kits[target]['state']}, "
                            "not an active kit")
    problems += doc_problems(kits)
    if problems:
        print(f"RECORD_REFUSED: {len(problems)} problem(s) -- fix them all, then record again:")
        for p in problems:
            print(f"  - {p}")
        return 1
    payload = {"generated_by": "app-kit/tools/kits.py record", "kits": kits}
    MANIFEST.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(k["files"]) for k in kits.values())
    print(f"RECORDED {len(kits)} kits, {total} files -> {MANIFEST.relative_to(KIT_ROOT.parent)}")
    for name, k in kits.items():
        print(f"  {name:<16} {k['state']:<8} {k['kit_sha'][:12]}  {len(k['files'])} files")
    return 0


# ---------------------------------------------------------------- consuming the manifest
def load_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit(f"no {MANIFEST.name}: run `kits.py record` first")
    return json.loads(MANIFEST.read_text(encoding="utf-8-sig"))["kits"]


def source_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(KIT_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip()[:12] if out.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def read_provenance(app: Path) -> dict:
    path = app / PROVENANCE
    if not path.is_file():
        return {"kits": {}}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def installed_files(app: Path, entry: dict) -> dict:
    """The app's copy of each recorded file, hashed the same way (missing -> None)."""
    out = {}
    for rel in entry["files"]:
        if rel in DOCS:
            continue
        p = app / rel
        out[rel] = file_sha(p) if p.is_file() else None
    return out


def add(kit: str, app: Path, *, anyway: bool, force: bool) -> int:
    try:
        lag = refuse_if_behind(KIT_ROOT, anyway=anyway)
    except StaleCheckout as exc:
        print(f"ADD_REFUSED: {exc}")
        return 1
    manifest = load_manifest()
    if kit not in manifest:
        print(f"ADD_REFUSED: no kit {kit!r}. Kits: {', '.join(sorted(manifest))}")
        return 1
    rec = manifest[kit]
    kit_dir = KITS_DIR / kit
    if kit_sha(kit_files(kit_dir)) != rec["kit_sha"]:
        print(f"ADD_REFUSED: kits.json does not match the files in kits/{kit} -- the manifest is "
              "stale. Run `kits.py record` so the provenance you write is true.")
        return 1
    if rec["state"] == "retired":
        print(f"ADD_REFUSED: {kit} is retired; use {rec['replaced_by']} instead.")
        return 1
    if not app.is_dir():
        print(f"ADD_REFUSED: {app} is not a directory")
        return 1

    prov = read_provenance(app)
    previous = prov["kits"].get(kit)
    if previous:
        edited = [rel for rel, sha in installed_files(app, previous).items()
                  if sha is not None and sha != previous["files"].get(rel)]
        if edited and not force:
            print(f"ADD_REFUSED: this app has edited {len(edited)} file(s) of its {kit} copy -- an "
                  "update would overwrite them:")
            for rel in edited:
                print(f"  - {rel}")
            print("Move the change into the kit (so every app gets it), or pass --force to "
                  "replace the copy and keep a backup of the edited one.")
            return 1

    stage = app / ".app-kit-staging"
    backup = app / ".app-kit-backup" / f"{kit}-{dt.datetime.now():%Y%m%d%H%M%S}"
    if stage.exists():
        print(f"ADD_REFUSED: {stage} exists -- an earlier add did not finish. Inspect it, "
              "then remove it by hand.")
        return 1
    moved: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        stage.mkdir()
        for entry in rec["installs"]:
            src = kit_dir / entry
            dst = stage / entry
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst)
        for entry in rec["installs"]:
            target = app / entry
            if target.exists():
                backup.mkdir(parents=True, exist_ok=True)
                aside = backup / entry
                shutil.move(str(target), str(aside))
                moved.append((aside, target))
            shutil.move(str(stage / entry), str(target))
            placed.append(target)
    except Exception as exc:                                  # noqa: BLE001
        # Put the app back exactly as it was: remove what was placed, restore what was moved.
        for target in placed:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        for aside, target in reversed(moved):
            shutil.move(str(aside), str(target))
        print(f"ADD_FAILED and RESTORED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    files = {rel: sha for rel, sha in rec["files"].items() if rel not in DOCS}
    prov["kits"][kit] = {"kit_sha": rec["kit_sha"], "source_commit": source_commit(),
                         "added_at": dt.date.today().isoformat(), "files": files}
    (app / PROVENANCE).write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")
    kept = ""
    if moved:
        if previous and force:
            kept = f"; the replaced copy is kept at {backup}"
        else:
            for aside, _ in moved:                   # unedited: identical to the recorded kit
                shutil.rmtree(aside, ignore_errors=True) if aside.is_dir() else aside.unlink()
            shutil.rmtree(backup, ignore_errors=True)
    print(f"ADDED {kit} {rec['kit_sha'][:12]} -> {app} ({', '.join(rec['installs'])}){kept}"
          + (f"  [app-kit is {lag} behind its upstream; --anyway]" if lag else ""))
    req = kit_dir / "REQUIRES.md"
    if req.is_file():
        print(f"\nWhat this app must now supply ({kit}/REQUIRES.md):\n")
        print(req.read_text(encoding="utf-8").strip())
    return 0


def status(app: Path) -> int:
    manifest, prov = load_manifest(), read_provenance(app)
    note = stale_note(KIT_ROOT)
    if not prov["kits"]:
        print(f"{app}: no {PROVENANCE} -- no vendored kit can be judged (unknown, not current)")
        return 1
    bad = 0
    for kit, entry in sorted(prov["kits"].items()):
        rec = manifest.get(kit)
        edited = [rel for rel, sha in installed_files(app, entry).items()
                  if sha is None or sha != entry["files"].get(rel)]
        if rec is None:
            state = "missing (the app-kit no longer ships this kit)"
        elif rec["state"] == "retired":
            state = f"retired -> use {rec['replaced_by']}"
        elif rec["kit_sha"] == entry["kit_sha"]:
            state = "current"
        else:
            changed = sorted(set(rec["files"]) ^ set(entry["files"]) | {
                r for r in rec["files"] if r in entry["files"] and rec["files"][r] != entry["files"][r]})
            changed = [r for r in changed if r not in DOCS]
            state = f"stale ({len(changed)} kit file(s) changed since {entry.get('added_at', '?')})"
        bad += state != "current" or bool(edited)
        print(f"  {kit:<16} {state}" + (f"; EDITED here: {', '.join(edited)}" if edited else ""))
    if note:
        print(" " + note)
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record")
    sub.add_parser("list")
    a_add = sub.add_parser("add")
    a_add.add_argument("kit")
    a_add.add_argument("app", type=Path)
    a_add.add_argument("--anyway", action="store_true", help="vendor from a checkout that is behind")
    a_add.add_argument("--force", action="store_true", help="replace an edited copy (kept as a backup)")
    a_status = sub.add_parser("status")
    a_status.add_argument("app", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "record":
        return record()
    if a.cmd == "list":
        for name, k in sorted(load_manifest().items()):
            extra = f" -> {k['replaced_by']}" if k["state"] == "retired" else ""
            print(f"  {name:<16} {k['state']:<8}{extra:<14} {k['kit_sha'][:12]}  {k['summary']}")
        return 0
    if a.cmd == "add":
        return add(a.kit, a.app, anyway=a.anyway, force=a.force)
    return status(a.app)


if __name__ == "__main__":
    raise SystemExit(main())
