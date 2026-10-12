#!/usr/bin/env python3
"""kits.py -- record what the app-kit ships, vendor a kit into an app, and say whether it drifted.

    python app-kit/tools/kits.py record                  # re-derive app-kit/kits.json (fails closed)
    python app-kit/tools/kits.py list                    # every kit, its state and sha
    python app-kit/tools/kits.py add <kit> <app-dir> [--slug budget_app]
                                                         # vendor a kit, with provenance
    python app-kit/tools/kits.py status <app-dir>        # current / stale / edited / retired
    python app-kit/tools/kits.py update <kit> <app-dir> [--dry-run]
                                                         # bring a vendored copy to the current
                                                         # kit WITHOUT losing the app's edits

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

``update`` is the three-way merge that ``add --force`` is not: per file, between the kit as the
app recorded it, the kit now and the app's copy -- untouched files take the kit's, the app's own
edits are kept, both-changed files are merged by ``git merge-file``, and a conflict anywhere
writes NOTHING (the material lands in ``.kit-update/<kit>/``). See the block above ``update()``.

``add`` refuses: a stale app-kit checkout (behind its upstream -- vendoring an old kit is how a
fixed bug comes back), a manifest that no longer matches the files (run ``record``), a RETIRED
kit (it names the replacement), and an app copy with local edits (they would be overwritten --
``--force`` keeps a backup). The swap is staged: the new copy is assembled beside the app, old
entries are moved aside, and if anything fails the old copy is put back.

SHARED-DATABASE MIGRATIONS ARE NAMED FROM THE APP'S SLUG. Django keys an applied migration by
(app label, migration name), and every app built from a kit shares the kit's app label. Two apps
on one database that ship the same ``0001_initial`` for that label look like ONE app to
``migrate``: the second migrates cleanly and creates none of its tables -- a green deploy with a
dead feature. A kit whose tables are already per-app (``"retarget_migrations": true`` in its
``KIT.json``) therefore has its migration files renamed on ``add`` from ``NNNN_<name>`` to
``NNNN_<slug>_<name>``, and every reference to them (``dependencies``, ``run_before``) rewritten
in the same staged copy, so the graph can never point at a half-renamed name. The slug is
``--slug``, else the one this app recorded before, else the app directory's name. The choice is
RECORDED per kit and kept on every later ``add``: renaming migrations an app has already applied
would make ``migrate`` create tables that exist, so a different ``--slug`` on a copy that was
vendored under other names is refused, never applied.

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
_MIGRATION = re.compile(r"^(?P<pkg>[^/]+(?:/[^/]+)*)/migrations/(?P<num>\d{4})_(?P<tail>\w+)\.py$")


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
                     "retarget_migrations": bool(meta.get("retarget_migrations")),
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
    """``{kit path: (sha of the app's copy or None, the sha that copy was written with)}``.

    A retargeted file lives at its RENAMED path and was written with rewritten references, so
    both come from the entry's own ``renamed`` / ``installed`` record, never re-derived."""
    renamed, written = entry.get("renamed") or {}, entry.get("installed") or {}
    out = {}
    for rel, sha in entry["files"].items():
        if rel in DOCS:
            continue
        where = renamed.get(rel, rel)
        p = app / where
        out[rel] = (file_sha(p) if p.is_file() else None, written.get(where, sha))
    return out


def slug_of(text: str) -> str:
    """A migration-name-safe slug: lower case, [a-z0-9_], never empty, never digit-first."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug if slug and not slug[0].isdigit() else f"app_{slug}".rstrip("_")


def retarget_plan(files: list, slug: str) -> dict:
    """``{kit path: renamed path}`` for every migration module in ``files``. Idempotent: a name
    already carrying the slug is left as it is, so a re-add never renames anything twice."""
    out = {}
    for rel in files:
        m = _MIGRATION.match(rel)
        if not m:
            continue
        tail = m["tail"] if m["tail"].startswith(slug + "_") else f"{slug}_{m['tail']}"
        if tail != m["tail"]:
            out[rel] = f"{m['pkg']}/migrations/{m['num']}_{tail}.py"
    return out


def retarget_stage(stage: Path, plan: dict) -> list:
    """Rename the planned migration files inside ``stage`` and rewrite every quoted reference
    to their module names in the staged ``.py`` files. Returns one line per change."""
    names = {Path(old).stem: Path(new).stem for old, new in plan.items()}
    log = []
    for old, new in sorted(plan.items()):
        (stage / old).rename(stage / new)
        log.append(f"{old} -> {Path(new).name}")
    if not names:
        return log
    # Whole quoted names only, longest first: '0001_initial' never matches inside
    # '0001_initial_extra', and nothing outside a string literal is touched.
    pattern = re.compile(r"""(["'])(%s)\1""" % "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)))
    for path in sorted(stage.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        new_text, n = pattern.subn(lambda m: m[1] + names[m[2]] + m[1], text)
        if n:
            path.write_text(new_text, encoding="utf-8", newline="")
            log.append(f"{path.relative_to(stage).as_posix()}: {n} reference(s) rewritten")
    return log


def add(kit: str, app: Path, *, anyway: bool, force: bool, slug: str = "") -> int:
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
        edited = [rel for rel, (sha, wrote) in installed_files(app, previous).items()
                  if sha is not None and sha != wrote]
        if edited and not force:
            print(f"ADD_REFUSED: this app has edited {len(edited)} file(s) of its {kit} copy -- an "
                  "update would overwrite them:")
            for rel in edited:
                print(f"  - {rel}")
            print(f"`kits.py update {kit} {app}` brings the copy current and KEEPS those edits "
                  "(a three-way merge); move a change into the kit so every app gets it; --force "
                  "replaces the copy and keeps a backup of the edited one.")
            return 1

    # The migration names this copy gets. A copy vendored before keeps the names it was
    # vendored with (its database may already record them); only a first add chooses.
    plan: dict = {}
    chosen = ""
    if rec.get("retarget_migrations"):
        wanted = slug_of(slug) if slug else ""
        if previous is not None:
            chosen = previous.get("migration_slug", "")
            if wanted and wanted != chosen:
                print(f"ADD_REFUSED: this app's {kit} migrations were vendored under "
                      + (f"the slug {chosen!r}" if chosen else "their original names")
                      + f", not {wanted!r}. If its database has applied them, renaming them makes "
                      "`migrate` create tables that already exist. Re-add without --slug to keep "
                      "the recorded names; changing them is a hand migration of the "
                      "django_migrations rows, done by a person.")
                return 1
        else:
            chosen = wanted or prov.get("slug") or slug_of(app.resolve().name)
        if chosen:
            plan = retarget_plan([r for r in rec["files"] if r not in DOCS
                                  and r.split("/", 1)[0] in rec["installs"]], chosen)

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
        retargeted = retarget_stage(stage, plan)
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
    if rec.get("retarget_migrations"):
        # What was written where, and with which bytes, so `status` compares the app's copy
        # with what add actually put there rather than with the kit's un-renamed file.
        rewritten = [r for r in files if (app / plan.get(r, r)).is_file()
                     and file_sha(app / plan.get(r, r)) != files[r]]
        prov["kits"][kit].update(
            migration_slug=chosen, renamed=plan,
            installed={plan.get(r, r): file_sha(app / plan.get(r, r)) for r in sorted(rewritten)})
        if chosen:
            prov["slug"] = chosen
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
    if retargeted:
        print(f"Migrations named from the slug {chosen!r}, so a shared database keeps them apart:")
        for line in retargeted:
            print(f"  {line}")
    elif rec.get("retarget_migrations") and not chosen:
        print("Migrations keep their original names: this copy was vendored under them before, "
              "and its database may already record them.")
    req = kit_dir / "REQUIRES.md"
    if req.is_file():
        print(f"\nWhat this app must now supply ({kit}/REQUIRES.md):\n")
        print(req.read_text(encoding="utf-8").strip())
    return 0


# ---------------------------------------------------------------- update: a three-way merge
#
# ``add`` refuses an edited copy (or replaces it with --force). ``update`` brings the copy to the
# current kit WITHOUT losing the app's edits, one file at a time, between three versions:
#
#   base   the kit as the app RECORDED it (its files at the recorded source commit, retargeted
#          the way ``add`` wrote them), verified against the per-file shas of the record;
#   theirs the kit now (this checkout, recorded in kits.json), retargeted the same way;
#   ours   the app's file.
#
#   ours == base            untouched here -> TAKE theirs
#   theirs == base          the kit did not change it -> KEEP ours (an edit here stays an edit)
#   both changed            git merge-file: clean -> MERGE, otherwise CONFLICT
#   new in the kit          ADD (only inside what the kit installs); an app file already there
#                           with other bytes is a CONFLICT
#   dropped by the kit      REPORTED, never deleted
#   deleted here            kept deleted when the kit did not change it, else CONFLICT
#
# ALL OR NOTHING. A conflict, or a base that cannot be verified, writes nothing to the app; the
# conflict material (ours, base, theirs, the marked merge) is written to .kit-update/<kit>/ for
# whoever resolves it. A base is verified file by file: a file whose bytes at the recorded commit
# do not match the record is looked up by its EXACT sha in the kit's history (a copy re-vendored
# from another commit under an older record would otherwise merge against the wrong base). On
# success the record carries the KIT's bytes per file -- never the app's -- so the next update
# still sees every local edit as an edit. Written in the app's own line endings, so the diff is
# the kit's change, not a whole-file CRLF churn.

def _norm(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _sha_bytes(rel: str, data: bytes) -> str:
    """The manifest's sha for ``data`` as file ``rel`` (text normalised, as ``file_sha`` does)."""
    return hashlib.sha1(_norm(data) if Path(rel).suffix in TEXT else data).hexdigest()


def _git_kit(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(KIT_ROOT), *args], capture_output=True, timeout=timeout)


def _kit_blob(commit: str, kit: str, rel: str) -> bytes | None:
    """The kit file ``rel`` at ``commit`` (None when absent there or unreadable)."""
    if not commit or commit == "unknown":
        return None
    out = _git_kit(["show", f"{commit}:./kits/{kit}/{rel}"])
    return out.stdout if out.returncode == 0 else None


def _blob_by_sha(kit: str, rel: str, want: str, limit: int = 200) -> bytes | None:
    """The bytes of kit file ``rel`` at whichever commit of its history matches ``want`` exactly."""
    log = _git_kit(["log", "--format=%H", f"-{limit}", "--", f"kits/{kit}/{rel}"])
    if log.returncode != 0:
        return None
    for commit in log.stdout.decode("ascii", "replace").split():
        data = _kit_blob(commit, kit, rel)
        if data is not None and _sha_bytes(rel, data) == want:
            return data
    return None


def _retargeted(files: dict, plan: dict) -> dict:
    """``{app path: bytes}`` for kit files ``{kit path: bytes}``, migrations renamed and their
    references rewritten exactly as ``add`` stages them (``retarget_stage``)."""
    import tempfile
    if not plan:
        return dict(files)
    with tempfile.TemporaryDirectory(prefix="kit-update-") as tmp:
        stage = Path(tmp)
        for rel, data in files.items():
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / rel).write_bytes(data)
        retarget_stage(stage, {k: v for k, v in plan.items() if k in files})
        return {plan.get(rel, rel): (stage / plan.get(rel, rel)).read_bytes() for rel in files}


def _merge3(ours: bytes, base: bytes, theirs: bytes, work: Path) -> tuple[bytes, bool]:
    """git merge-file -p over LF-normalised copies: (result, clean?)."""
    work.mkdir(parents=True, exist_ok=True)
    o, b, t = work / "ours", work / "base", work / "theirs"
    o.write_bytes(_norm(ours))
    b.write_bytes(_norm(base))
    t.write_bytes(_norm(theirs))
    r = subprocess.run(["git", "merge-file", "-p", "-L", "ours (this app)", "-L",
                        "base (recorded kit)", "-L", "theirs (current kit)", str(o), str(b), str(t)],
                       capture_output=True, timeout=60)
    return r.stdout, r.returncode == 0


def _like(ours: bytes | None, out: bytes) -> bytes:
    if ours is not None and b"\r\n" in ours and b"\r\n" not in out:
        return _norm(out).replace(b"\n", b"\r\n")
    return out


def update(kit: str, app: Path, *, anyway: bool, dry_run: bool) -> int:
    import tempfile
    try:
        lag = refuse_if_behind(KIT_ROOT, anyway=anyway)
    except StaleCheckout as exc:
        print(f"UPDATE_REFUSED: {exc}")
        return 1
    manifest = load_manifest()
    prov = read_provenance(app)
    entry = prov["kits"].get(kit)
    if entry is None:
        print(f"UPDATE_REFUSED: {app} records no {kit} copy -- `kits.py add {kit} {app}` vendors it")
        return 1
    rec = manifest.get(kit)
    if rec is None or rec["state"] == "retired":
        print(f"UPDATE_REFUSED: {kit} is " + ("not shipped any more" if rec is None else
                                              f"retired; use {rec['replaced_by']} instead"))
        return 1
    kit_dir = KITS_DIR / kit
    if kit_sha(kit_files(kit_dir)) != rec["kit_sha"]:
        print(f"UPDATE_REFUSED: kits.json does not match kits/{kit} -- run `kits.py record` first")
        return 1
    if rec["kit_sha"] == entry["kit_sha"]:
        print(f"CURRENT: {app}'s {kit} copy is already the recorded kit {rec['kit_sha'][:12]}")
        return 0
    recorded = {r: s for r, s in entry["files"].items() if r not in DOCS}
    current = {r: s for r, s in rec["files"].items() if r not in DOCS
               and r.split("/", 1)[0] in rec["installs"]}
    plan = dict(entry.get("renamed") or {})
    if rec.get("retarget_migrations") and entry.get("migration_slug"):
        plan.update({k: v for k, v in retarget_plan(sorted(current), entry["migration_slug"])
                     .items() if k not in plan})

    # BASE: the kit as recorded, every file verified against the record's own sha.
    commit = str(entry.get("source_commit") or "")
    base_raw, unverified = {}, []
    for rel, want in sorted(recorded.items()):
        data = _kit_blob(commit, kit, rel)
        if data is None or _sha_bytes(rel, data) != want:
            data = _blob_by_sha(kit, rel, want)
        if data is None:
            unverified.append(rel)
        else:
            base_raw[rel] = data
    if unverified:
        print(f"UPDATE_REFUSED: the recorded base cannot be verified for {len(unverified)} file(s) "
              f"(not at {commit or 'an unrecorded commit'}, and no commit of the kit's history "
              "has those exact bytes) -- nothing was written. Re-vendor with `kits.py add --force` "
              "after reading what this app changed:")
        for rel in unverified:
            print(f"  - {rel}")
        return 1
    theirs_raw = {rel: (kit_dir / rel).read_bytes() for rel in current}
    base = _retargeted(base_raw, plan)
    theirs = _retargeted(theirs_raw, plan)

    actions, out_bytes, conflicts = [], {}, []
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    material = app / ".kit-update" / kit / stamp
    for kit_rel in sorted(set(recorded) | set(current)):
        where = plan.get(kit_rel, kit_rel)
        path = app / where
        ours = path.read_bytes() if path.is_file() else None
        b, t = base.get(where), theirs.get(where)
        same = (lambda x, y: x is not None and y is not None and _norm(x) == _norm(y))
        if t is None:
            actions.append(("dropped", where, "the kit no longer ships it; left in place"))
            continue
        if b is None:
            if ours is None:
                actions.append(("add", where, "new in the kit"))
                out_bytes[where] = t
            elif same(ours, t):
                actions.append(("same", where, "already the kit's bytes"))
            else:
                actions.append(("CONFLICT", where, "new in the kit, and this app has its own file there"))
                conflicts.append((where, ours, b"", t, None))
            continue
        if ours is None:
            if same(b, t):
                actions.append(("kept-deleted", where, "removed here; the kit did not change it"))
            else:
                actions.append(("CONFLICT", where, "removed here, and the kit changed it"))
                conflicts.append((where, b"", b, t, None))
            continue
        if same(ours, t):
            actions.append(("same", where, "already the kit's bytes"))
        elif same(ours, b):
            actions.append(("take", where, "untouched here"))
            out_bytes[where] = _like(ours, t)
        elif same(b, t):
            actions.append(("keep", where, "edited here; the kit did not change it"))
        else:
            with tempfile.TemporaryDirectory(prefix="kit-merge-") as tmp:
                merged, clean = _merge3(ours, b, t, Path(tmp))
            if clean:
                actions.append(("merge", where, "edited here AND in the kit; merged cleanly"))
                out_bytes[where] = _like(ours, merged)
            else:
                actions.append(("CONFLICT", where, "edited here AND in the kit; the edits overlap"))
                conflicts.append((where, ours, b, t, merged))

    width = max([len(a[0]) for a in actions] or [4])
    for verb, where, why in actions:
        print(f"  {verb:<{width}}  {where}  -- {why}")
    if conflicts:
        for where, ours, b, t, merged in conflicts:
            d = material / where
            d.mkdir(parents=True, exist_ok=True)
            (d / "ours").write_bytes(ours)
            (d / "base").write_bytes(b)
            (d / "theirs").write_bytes(t)
            if merged is not None:
                (d / "merged").write_bytes(merged)
        print(f"UPDATE_REFUSED: {len(conflicts)} conflict(s) -- NOTHING was written to the app. "
              f"Each conflict's ours/base/theirs/merged is under {material}; resolve them in the "
              "app (or move the change into the kit), then run update again.")
        return 1
    if dry_run:
        print(f"DRY RUN: {sum(1 for a in actions if a[0] in ('take', 'merge', 'add'))} file(s) "
              f"would be written; nothing was. ({kit} {entry['kit_sha'][:12]} -> "
              f"{rec['kit_sha'][:12]})")
        return 0
    written = []
    try:
        for where, data in sorted(out_bytes.items()):
            path = app / where
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".kit-update.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            written.append(where)
    except OSError as exc:
        print(f"UPDATE_FAILED after writing {len(written)} file(s) ({', '.join(written)}): "
              f"{type(exc).__name__}: {exc}. The record is unchanged; rerun update once the "
              "file can be written (a written file now reads as an edit the next run keeps).")
        return 1
    # The record carries the KIT's bytes per file (retargeted as written by add), never the
    # app's, so a kept or merged local edit still reads as an edit next time.
    new_entry = {k: v for k, v in entry.items()
                 if k not in ("kit_sha", "files", "source_commit", "installed")}
    new_entry.update(kit_sha=rec["kit_sha"], source_commit=source_commit(),
                     updated_at=dt.date.today().isoformat(),
                     files={r: s for r, s in rec["files"].items() if r not in DOCS})
    if plan:
        new_entry["renamed"] = {k: v for k, v in plan.items() if k in current}
        new_entry["installed"] = {plan[r]: _sha_bytes(r, theirs[plan[r]])
                                  for r in current if r in plan}
    prov["kits"][kit] = new_entry
    (app / PROVENANCE).write_text(json.dumps(prov, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")
    print(f"UPDATED {kit} {entry['kit_sha'][:12]} -> {rec['kit_sha'][:12]} in {app}: "
          + ", ".join(f"{n} {v}" for v, n in sorted(
              {v: sum(1 for a in actions if a[0] == v) for v, _w, _y in actions}.items()))
          + (f"  [app-kit is {lag} behind its upstream; --anyway]" if lag else ""))
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
        edited = [rel for rel, (sha, wrote) in installed_files(app, entry).items()
                  if sha is None or sha != wrote]
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
        where = entry.get("renamed") or {}
        edited = [where.get(rel, rel) for rel in edited]      # the app's own path
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
    a_add.add_argument("--slug", default="",
                       help="the app's slug, for kits whose migrations are named per app "
                            "(default: the slug recorded before, else the directory name)")
    a_status = sub.add_parser("status")
    a_status.add_argument("app", type=Path)
    a_update = sub.add_parser("update")
    a_update.add_argument("kit")
    a_update.add_argument("app", type=Path)
    a_update.add_argument("--anyway", action="store_true", help="update from a checkout that is behind")
    a_update.add_argument("--dry-run", action="store_true", help="say what would happen; write nothing")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        return record()
    if a.cmd == "list":
        for name, k in sorted(load_manifest().items()):
            extra = f" -> {k['replaced_by']}" if k["state"] == "retired" else ""
            print(f"  {name:<16} {k['state']:<8}{extra:<14} {k['kit_sha'][:12]}  {k['summary']}")
        return 0
    if a.cmd == "add":
        return add(a.kit, a.app, anyway=a.anyway, force=a.force, slug=a.slug)
    if a.cmd == "update":
        return update(a.kit, a.app, anyway=a.anyway, dry_run=a.dry_run)
    return status(a.app)


if __name__ == "__main__":
    raise SystemExit(main())
