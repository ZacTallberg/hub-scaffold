#!/usr/bin/env python3
"""Manifest-driven, non-destructive whole-unit upgrades for Hub adopters.

Only canonical Hub/runtime-contract paths and the adopter manifest are written.
PROJECT identity, seed, state, runtime ledger, and adopter-only files are never
part of the write set. This command never discovers, creates, or runs tests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCAFFOLD_ROOT = Path(__file__).resolve().parent.parent
if str(SCAFFOLD_ROOT) not in sys.path:
    sys.path.insert(0, str(SCAFFOLD_ROOT))

from hub_core.grandfather import LEGACY_ENTITY_SCHEMA_POLICY, last_event_seq
from hub_core.project import fold
from hub_core.validate import Registry, validate_portable, validation_signature

UNIT_SPECS: dict[str, dict[str, Any]] = {
    "hub_core": {"tree": Path("hub_core")},
    "django_hub": {"tree": Path("adapters/django/hub")},
    "project_schema": {"tree": Path("PROJECT/schema")},
    "project_contracts": {
        "files": [Path("PROJECT/HUB-QUALITY.md"), Path("PROJECT/DOCTRINE.md")]
    },
}
DOCTRINE_UNIT = "project_contracts"
DOCTRINE_FILE = "DOCTRINE.md"
DOCTRINE_HEADING = "## §6 Project laws"
EXCLUDED_NAMES = {"__pycache__", ".DS_Store"}
PROTECTED_PROJECT_FILES = {
    Path("PROJECT/project.json"),
    Path("PROJECT/seed.json"),
    Path("PROJECT/state.json"),
}
PROTECTED_PROJECT_TREE = Path("PROJECT/.hub")


class UpgradeError(RuntimeError):
    """A safe refusal raised before any adopter file is changed."""


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def tree_sha256(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def first_symlink(path: Path, root: Path) -> Path | None:
    """Return the first existing symlink between root and path, inclusive."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def parse_destination(value: Any, unit_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise UpgradeError(f"units.{unit_name}.destination must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise UpgradeError(f"units.{unit_name}.destination must be relative: {value!r}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UpgradeError(f"units.{unit_name}.destination contains an unsafe segment: {value!r}")
    if parts[0].casefold() in {".git", ".hub"}:
        raise UpgradeError(f"units.{unit_name}.destination enters protected {parts[0]!r}")
    return Path(*parts)


def parse_managed_relative(value: Any, unit_name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise UpgradeError(f"prior ownership record for {unit_name} has an invalid file path")
    raw = value.replace("\\", "/")
    parts = raw.split("/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise UpgradeError(f"prior ownership record for {unit_name} is unsafe: {value!r}")
    return Path(*parts)


def is_test_path(relative: Path) -> bool:
    lowered = [part.casefold() for part in relative.parts]
    filename = lowered[-1]
    return (
        any(part in {"test", "tests", "testing"} for part in lowered[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def collect_tree(source_root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for dirname in directories:
            candidate = current_path / dirname
            if dirname in EXCLUDED_NAMES:
                continue
            if candidate.is_symlink():
                raise UpgradeError(f"canonical unit contains a directory symlink: {candidate}")
            kept_directories.append(dirname)
        directories[:] = kept_directories

        for filename in filenames:
            source = current_path / filename
            if filename in EXCLUDED_NAMES or source.suffix in {".pyc", ".pyo"}:
                continue
            if source.is_symlink() or not source.is_file():
                raise UpgradeError(f"canonical unit contains a non-regular file: {source}")
            relative = source.relative_to(source_root)
            if is_test_path(relative):
                raise UpgradeError(
                    "canonical unit contains a test-like path; refusing to create a test artifact "
                    f"in the adopter: {source}"
                )
            found[relative.as_posix()] = source
    if not found:
        raise UpgradeError(f"canonical unit is empty: {source_root}")
    return dict(sorted(found.items()))


def collect_unit(spec: dict[str, Any]) -> tuple[str | list[str], dict[str, Path]]:
    if "tree" in spec:
        relative_root = spec["tree"]
        return relative_root.as_posix(), collect_tree(SCAFFOLD_ROOT / relative_root)

    source_files: dict[str, Path] = {}
    descriptions: list[str] = []
    for relative in spec["files"]:
        source = SCAFFOLD_ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise UpgradeError(f"canonical contract file is missing or non-regular: {source}")
        if is_test_path(relative):
            raise UpgradeError(f"canonical contract may not be a test artifact: {source}")
        source_files[relative.name] = source
        descriptions.append(relative.as_posix())
    return descriptions, dict(sorted(source_files.items()))


def doctrine_parts(text: str) -> tuple[str, str]:
    """Return the canonical-managed prefix and adopter-owned §6 body."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(rf"(?m)^{re.escape(DOCTRINE_HEADING)}[^\n]*$", normalized)
    if not match:
        return normalized.rstrip("\n") + "\n", ""

    line_end = normalized.find("\n", match.end())
    if line_end < 0:
        return normalized.rstrip("\n") + "\n", ""
    prefix = normalized[: line_end + 1]
    remainder = normalized[line_end + 1 :]
    leading = len(remainder) - len(remainder.lstrip("\n"))
    after_blank = remainder[leading:]
    if after_blank.startswith("<!--"):
        comment_end = after_blank.find("-->")
        if comment_end >= 0:
            comment_end += 3
            prefix += remainder[:leading] + after_blank[:comment_end]
            remainder = after_blank[comment_end:]
    return prefix.rstrip("\n") + "\n", remainder.lstrip("\n")


def doctrine_payload(source: Path, destination: Path) -> tuple[bytes, str]:
    source_prefix, source_suffix = doctrine_parts(source.read_text(encoding="utf-8"))
    if source_suffix.strip():
        raise UpgradeError(
            "scaffold PROJECT/DOCTRINE.md contains project-specific §6 laws; "
            "canonical source must remain adopter-agnostic"
        )
    adopter_suffix = ""
    if destination.exists() and destination.is_file() and not destination.is_symlink():
        _, adopter_suffix = doctrine_parts(destination.read_text(encoding="utf-8"))
    rendered = source_prefix
    if adopter_suffix:
        rendered += adopter_suffix.rstrip("\n") + "\n"
    return rendered.encode("utf-8"), sha256_bytes(source_prefix.encode("utf-8"))


def git_value(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(SCAFFOLD_ROOT), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def managed_source_dirty() -> bool | None:
    source_paths: list[str] = []
    for spec in UNIT_SPECS.values():
        if "tree" in spec:
            source_paths.append(spec["tree"].as_posix())
        else:
            source_paths.extend(path.as_posix() for path in spec["files"])
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(SCAFFOLD_ROOT),
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                *source_paths,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise UpgradeError(f"manifest may not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpgradeError(f"manifest does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UpgradeError("manifest root must be a JSON object")
    if value.get("schema_version") != 1:
        raise UpgradeError("manifest schema_version must be 1")
    units = value.get("units")
    if not isinstance(units, dict) or set(units) != set(UNIT_SPECS):
        raise UpgradeError("manifest units must contain exactly: " + ", ".join(UNIT_SPECS))
    for name, unit in units.items():
        if not isinstance(unit, dict):
            raise UpgradeError(f"units.{name} must be a JSON object")
    return value


def ledger_events(path: Path) -> list[dict[str, Any]]:
    """Read the protected adopter ledger for one-time compatibility capture.

    The upgrader never changes the ledger. A malformed non-empty ledger is a refusal, because a
    cutoff captured from ambiguous history would be capable of hiding current receipt failures.
    Full hash-chain verification remains the Hub audit's responsibility.
    """
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise UpgradeError(f"protected Hub ledger is not a regular file: {path}")
    events = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                seq = value.get("seq") if isinstance(value, dict) else None
                event_hash = value.get("hash") if isinstance(value, dict) else None
                if (
                    not isinstance(seq, int)
                    or seq <= 0
                    or not isinstance(event_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", event_hash) is None
                ):
                    raise ValueError(f"line {number} lacks a valid seq/hash")
                if events and seq <= events[-1]["seq"]:
                    raise ValueError(f"line {number} does not advance sequence")
                events.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise UpgradeError(f"cannot anchor legacy receipt cutoff from {path}: {exc}") from exc
    return events


def common_ledger_cutoff(
    repository_events: list[dict[str, Any]], authoritative_events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the last event in the exact immutable prefix shared by two ledgers."""
    cutoff = None
    for repository_event, authoritative_event in zip(repository_events, authoritative_events):
        if (
            repository_event.get("seq") != authoritative_event.get("seq")
            or repository_event.get("hash") != authoritative_event.get("hash")
        ):
            break
        cutoff = {
            "seq": repository_event["seq"],
            "anchor_hash": repository_event["hash"],
        }
    return cutoff


def capture_legacy_entity_schema(
    events: list[dict[str, Any]], receipt_cutoff: dict[str, Any]
) -> dict[str, Any]:
    """Capture exact canonical schema-debt signatures at the original immutable cutoff."""
    seq = receipt_cutoff.get("seq") if isinstance(receipt_cutoff, dict) else None
    anchor_hash = receipt_cutoff.get("anchor_hash") if isinstance(receipt_cutoff, dict) else None
    if (
        not isinstance(seq, int)
        or seq <= 0
        or not isinstance(anchor_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", anchor_hash) is None
        or not any(event.get("seq") == seq and event.get("hash") == anchor_hash for event in events)
    ):
        raise UpgradeError(
            "legacy_done_receipts is not anchored to this ledger; refusing entity-schema capture"
        )

    registry = Registry.from_dir(SCAFFOLD_ROOT / "PROJECT" / "schema")
    entities = fold(events)
    last_events = last_event_seq(events)
    signatures: dict[str, set[str]] = {}
    for entity_id in sorted(entities):
        entity = entities[entity_id]
        entity_type = entity.get("type")
        if not isinstance(entity_type, str) or last_events.get(entity_id, seq + 1) > seq:
            continue

        # Receipt debt remains owned by legacy_done_receipts.  Sentinels classify it in memory so
        # a second, exact signature can describe only the adopter extension/schema debt.
        validation_entity = entity
        if entity_type == "task" and entity.get("status") == "done":
            missing = [field for field in ("verified_by", "evidence_uri") if not entity.get(field)]
            if missing:
                validation_entity = dict(entity)
                for field in missing:
                    validation_entity[field] = ["legacy-receipt-classification-only"]

        errors = validate_portable(validation_entity, entity_type, registry)
        if errors:
            signatures.setdefault(entity_type, set()).add(
                validation_signature(entity_type, errors)
            )

    return {
        "seq": seq,
        "anchor_hash": anchor_hash,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "policy": LEGACY_ENTITY_SCHEMA_POLICY,
        "signatures": {
            entity_type: sorted(values) for entity_type, values in sorted(signatures.items())
        },
    }


def protected_project_path(relative: Path) -> bool:
    if relative in PROTECTED_PROJECT_FILES:
        return True
    return relative == PROTECTED_PROJECT_TREE or is_within(relative, PROTECTED_PROJECT_TREE)


def atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.hub-upgrade-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_transaction(
    writes: list[tuple[bytes, Path, int]],
    deletions: list[Path],
    final_write: tuple[bytes, Path, int],
) -> None:
    """Apply every write or restore the exact pre-upgrade file bytes."""
    originals: list[tuple[Path, bytes | None, int | None]] = []
    operation_paths = [path for _, path, _ in writes] + deletions + [final_write[1]]
    for path in operation_paths:
        if path.exists():
            originals.append(
                (path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            )
        else:
            originals.append((path, None, None))

    try:
        for payload, path, mode in writes:
            atomic_write_bytes(path, payload, mode)
        for path in deletions:
            path.unlink()
        atomic_write_bytes(final_write[1], final_write[0], final_write[2])
    except OSError as error:
        rollback_errors: list[str] = []
        for path, payload, mode in reversed(originals):
            try:
                if payload is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write_bytes(path, payload, mode or 0o644)
            except OSError as rollback_error:
                rollback_errors.append(f"{path}: {rollback_error}")
        detail = ""
        if rollback_errors:
            detail = "; rollback problems: " + "; ".join(rollback_errors)
        raise UpgradeError(f"upgrade write failed and was rolled back: {error}{detail}") from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upsert canonical Hub units into an existing manifest-owned adopter."
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="tracked adoption manifest located at the adopter repository root",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the planned upsert without writing files or provenance",
    )
    parser.add_argument(
        "--overwrite-managed",
        action="store_true",
        help="replace conflicting canonical paths after review; adopter-only extras remain",
    )
    parser.add_argument(
        "--compatibility-ledger",
        type=Path,
        help=(
            "read-only snapshot of the authoritative production events.jsonl; compatibility "
            "uses only its exact immutable prefix shared with the repository ledger"
        ),
    )
    parser.add_argument(
        "--reanchor-compatibility",
        action="store_true",
        help=(
            "explicitly move an existing local-only compatibility cutoff backward to the "
            "shared production prefix (requires --compatibility-ledger; never moves forward)"
        ),
    )
    arguments = parser.parse_args()

    supplied_manifest = arguments.manifest.expanduser().absolute()
    if supplied_manifest.is_symlink():
        raise UpgradeError(f"manifest may not be a symlink: {supplied_manifest}")
    manifest_path = supplied_manifest.resolve()
    manifest = load_manifest(manifest_path)
    target_root = manifest_path.parent.resolve()
    compatibility = manifest.get("compatibility")
    if compatibility is None:
        compatibility = {}
        manifest["compatibility"] = compatibility
    if not isinstance(compatibility, dict):
        raise UpgradeError("manifest compatibility must be a JSON object")
    ledger = ledger_events(target_root / "PROJECT" / ".hub" / "events.jsonl")
    compatibility_cutoff = (
        {"seq": ledger[-1]["seq"], "anchor_hash": ledger[-1]["hash"]} if ledger else None
    )
    authoritative_ledger = ledger
    if arguments.reanchor_compatibility and not arguments.compatibility_ledger:
        raise UpgradeError("--reanchor-compatibility requires --compatibility-ledger")
    if arguments.compatibility_ledger:
        authoritative_path = arguments.compatibility_ledger.expanduser().absolute().resolve()
        authoritative_ledger = ledger_events(authoritative_path)
        compatibility_cutoff = common_ledger_cutoff(ledger, authoritative_ledger)
        if (ledger or authoritative_ledger) and compatibility_cutoff is None:
            raise UpgradeError(
                "repository and authoritative production ledgers share no immutable prefix"
            )

    if "legacy_done_receipts" not in compatibility:
        if compatibility_cutoff:
            compatibility["legacy_done_receipts"] = {
                **compatibility_cutoff,
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "policy": "missing verified_by/evidence_uri only",
            }
    elif arguments.compatibility_ledger:
        receipt = compatibility.get("legacy_done_receipts")
        receipt_seq = receipt.get("seq") if isinstance(receipt, dict) else None
        receipt_hash = receipt.get("anchor_hash") if isinstance(receipt, dict) else None
        anchor_is_authoritative = any(
            event.get("seq") == receipt_seq and event.get("hash") == receipt_hash
            for event in authoritative_ledger
        )
        if not anchor_is_authoritative:
            if not arguments.reanchor_compatibility:
                raise UpgradeError(
                    "legacy compatibility cutoff is absent from the authoritative ledger; "
                    "review the shared prefix and rerun with --reanchor-compatibility"
                )
            if not compatibility_cutoff or not isinstance(receipt_seq, int):
                raise UpgradeError("cannot derive a safe backward compatibility cutoff")
            if compatibility_cutoff["seq"] >= receipt_seq:
                raise UpgradeError(
                    "refusing compatibility reanchor that does not move strictly backward"
                )
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            compatibility["legacy_done_receipts"] = {
                **receipt,
                **compatibility_cutoff,
                "reanchored_at": now,
                "reanchored_from": {"seq": receipt_seq, "anchor_hash": receipt_hash},
            }
            # Entity signatures are cutoff-specific. Recompute them below against the smaller,
            # authoritative common prefix rather than preserving a now-misaddressed record.
            compatibility.pop("legacy_entity_schema", None)
    if "legacy_entity_schema" not in compatibility and ledger:
        compatibility["legacy_entity_schema"] = capture_legacy_entity_schema(
            ledger, compatibility.get("legacy_done_receipts")
        )
    previous_adoption = manifest.get("adopted_from", {})
    previous_units = (
        previous_adoption.get("units", {}) if isinstance(previous_adoption, dict) else {}
    )

    configured: dict[str, dict[str, Any]] = {}
    for name, spec in UNIT_SPECS.items():
        destination_relative = parse_destination(
            manifest["units"][name].get("destination"), name
        )
        destination = target_root / destination_relative
        resolved_destination = destination.resolve(strict=False)
        if not is_within(resolved_destination, target_root):
            raise UpgradeError(f"units.{name}.destination escapes the adopter root")
        linked = first_symlink(destination, target_root)
        if linked:
            raise UpgradeError(f"units.{name}.destination crosses symlink: {linked}")
        if destination.exists() and not destination.is_dir():
            raise UpgradeError(f"units.{name}.destination is not a directory: {destination}")
        source_description, source_files = collect_unit(spec)
        configured[name] = {
            "source": source_description,
            "source_files": source_files,
            "destination_relative": destination_relative,
            "destination": destination,
        }

    plan: list[tuple[bytes, Path, int]] = []
    deletions: list[Path] = []
    conflicts: list[str] = []
    unchanged = 0
    target_owners: dict[Path, str] = {}
    unit_records: dict[str, dict[str, Any]] = {}
    retirement_context: dict[str, dict[str, Any]] = {}

    for name, unit in configured.items():
        previous = previous_units.get(name, {}) if isinstance(previous_units, dict) else {}
        previous_destination = previous.get("destination") if isinstance(previous, dict) else None
        previous_installed = previous.get("installed_files", {}) if isinstance(previous, dict) else {}
        previous_prefixes = previous.get("managed_prefixes", {}) if isinstance(previous, dict) else {}
        if not previous_installed and isinstance(previous, dict):
            previous_installed = previous.get("files", {})

        source_hashes: dict[str, str] = {}
        installed_hashes: dict[str, str] = {}
        managed_prefixes: dict[str, str] = {}
        destination_string = unit["destination_relative"].as_posix()

        for relative, source in unit["source_files"].items():
            source_hashes[relative] = sha256_file(source)
            destination = unit["destination"] / Path(relative)
            linked = first_symlink(destination, target_root)
            if linked:
                conflicts.append(f"{name}:{relative} crosses destination symlink {linked}")
                continue
            resolved_destination = destination.resolve(strict=False)
            if not is_within(resolved_destination, target_root):
                conflicts.append(f"{name}:{relative} resolves outside the adopter root")
                continue
            destination_relative_to_root = resolved_destination.relative_to(target_root)
            if protected_project_path(destination_relative_to_root):
                conflicts.append(f"{name}:{relative} enters protected {destination_relative_to_root}")
                continue
            if resolved_destination == manifest_path:
                conflicts.append(f"{name}:{relative} collides with the adoption manifest")
                continue
            prior_owner = target_owners.get(resolved_destination)
            if prior_owner:
                conflicts.append(f"{name}:{relative} collides with managed unit {prior_owner}")
                continue
            target_owners[resolved_destination] = name
            if destination.is_symlink():
                conflicts.append(f"{name}:{relative} is a destination symlink")
                continue
            if destination.exists() and not destination.is_file():
                conflicts.append(f"{name}:{relative} is not a regular destination file")
                continue

            if name == DOCTRINE_UNIT and relative == DOCTRINE_FILE:
                try:
                    payload, prefix_hash = doctrine_payload(source, destination)
                except (OSError, UnicodeError) as exc:
                    conflicts.append(f"{name}:{relative} cannot preserve §6 laws: {exc}")
                    continue
                managed_prefixes[relative] = prefix_hash
            else:
                payload = source.read_bytes()
            installed_hashes[relative] = sha256_bytes(payload)
            mode = stat.S_IMODE(source.stat().st_mode)

            if not destination.exists():
                plan.append((payload, destination, mode))
                continue
            destination_hash = sha256_file(destination)
            if destination_hash == installed_hashes[relative]:
                unchanged += 1
                continue

            prior_hash = previous_installed.get(relative) if isinstance(previous_installed, dict) else None
            known_managed = previous_destination == destination_string and isinstance(prior_hash, str)
            allowed_doctrine_extension = False
            if (
                known_managed
                and name == DOCTRINE_UNIT
                and relative == DOCTRINE_FILE
                and isinstance(previous_prefixes, dict)
                and isinstance(previous_prefixes.get(relative), str)
            ):
                try:
                    current_prefix, _ = doctrine_parts(destination.read_text(encoding="utf-8"))
                    allowed_doctrine_extension = (
                        sha256_bytes(current_prefix.encode("utf-8"))
                        == previous_prefixes[relative]
                    )
                except (OSError, UnicodeError):
                    allowed_doctrine_extension = False

            locally_modified = known_managed and destination_hash != prior_hash
            if not arguments.overwrite_managed:
                if locally_modified and not allowed_doctrine_extension:
                    conflicts.append(f"{name}:{relative} has adopter modifications")
                    continue
                if not known_managed:
                    conflicts.append(f"{name}:{relative} exists without prior ownership record")
                    continue
            plan.append((payload, destination, mode))

        record: dict[str, Any] = {
            "source": unit["source"],
            "destination": destination_string,
            "tree_sha256": tree_sha256(source_hashes),
            "files": source_hashes,
            "installed_files": installed_hashes,
        }
        if managed_prefixes:
            record["managed_prefixes"] = managed_prefixes
        unit_records[name] = record
        retirement_context[name] = {
            "previous_destination": previous_destination,
            "previous_installed": previous_installed,
            "destination": unit["destination"],
            "destination_string": destination_string,
            "current_files": set(source_hashes),
        }

    # A path removed upstream is pruned only when the previous manifest owned it and the adopter
    # has not changed its installed bytes. Unknown extras are never candidates for deletion.
    deletion_owners: dict[Path, str] = {}
    for name, context in retirement_context.items():
        previous_installed = context["previous_installed"]
        if (
            context["previous_destination"] != context["destination_string"]
            or not isinstance(previous_installed, dict)
        ):
            continue
        retired = set(previous_installed) - context["current_files"]
        for recorded_relative in sorted(retired):
            relative_path = parse_managed_relative(recorded_relative, name)
            destination = context["destination"] / relative_path
            linked = first_symlink(destination, target_root)
            if linked:
                conflicts.append(
                    f"{name}:{recorded_relative} retired upstream but crosses symlink {linked}"
                )
                continue
            resolved_destination = destination.resolve(strict=False)
            if not is_within(resolved_destination, target_root):
                conflicts.append(f"{name}:{recorded_relative} retired path escapes adopter root")
                continue
            relative_to_root = resolved_destination.relative_to(target_root)
            if protected_project_path(relative_to_root) or resolved_destination == manifest_path:
                conflicts.append(f"{name}:{recorded_relative} retired path is protected")
                continue
            if resolved_destination in target_owners:
                continue
            prior_deletion_owner = deletion_owners.get(resolved_destination)
            if prior_deletion_owner:
                conflicts.append(
                    f"{name}:{recorded_relative} retired path is also owned by "
                    f"{prior_deletion_owner}"
                )
                continue
            if not destination.exists():
                continue
            if destination.is_symlink() or not destination.is_file():
                conflicts.append(
                    f"{name}:{recorded_relative} retired managed path is not a regular file"
                )
                continue
            prior_hash = previous_installed.get(recorded_relative)
            if not isinstance(prior_hash, str) or sha256_file(destination) != prior_hash:
                conflicts.append(
                    f"{name}:{recorded_relative} was removed upstream but has adopter modifications"
                )
                continue
            deletion_owners[resolved_destination] = name
            deletions.append(destination)

    if conflicts:
        rendered = "\n  - ".join(conflicts)
        raise UpgradeError(
            "refusing before writes because canonical destinations contain conflicts:\n"
            f"  - {rendered}\n"
            "Move extensions to distinct paths or rerun with --overwrite-managed after reviewing "
            "the replacement."
        )

    combined_source_hashes = {
        f"{unit_name}/{relative}": file_hash
        for unit_name, record in unit_records.items()
        for relative, file_hash in record["files"].items()
    }
    source_record = {
        "repository": git_value("remote", "get-url", "origin"),
        "commit": git_value("rev-parse", "HEAD"),
        "managed_tree_dirty": managed_source_dirty(),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tree_sha256": tree_sha256(combined_source_hashes),
        "units": unit_records,
    }

    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "would_upsert": len(plan),
                    "would_prune": len(deletions),
                    "unchanged": unchanged,
                    "source": source_record,
                },
                indent=2,
            )
        )
        return 0

    manifest["adopted_from"] = source_record
    manifest_payload = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
    apply_transaction(plan, deletions, (manifest_payload, manifest_path, manifest_mode))
    print(
        f"Upgraded {len(plan)} canonical files, pruned {len(deletions)} retired managed files; "
        f"{unchanged} already current. "
        f"Adopter extras and protected PROJECT state were untouched. Provenance: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpgradeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
