"""A dependency-free JSON-Schema 2020-12 *subset* validator + schema registry.

Supports exactly the keywords the hub schemas use: type, const, enum, properties,
additionalProperties (bool), required, items (single schema), minItems, minLength, minimum,
maximum, pattern, format ('date-time', lenient), $ref (by $id + JSON-pointer), allOf, anyOf, if/then/else.

Prefers the real `jsonschema` lib (Draft 2020-12) when installed; otherwise uses this fallback so a
single-file WSGI app needs no dependency. Returns a list of "path: message" error strings;
empty list = valid.
"""
import re
from pathlib import Path

try:  # canonical path: real validator when available
    import json as _json

    import jsonschema  # type: ignore
    from jsonschema import Draft202012Validator  # type: ignore
    _HAVE_JSONSCHEMA = True
except Exception:  # pragma: no cover - fallback path
    import json as _json
    _HAVE_JSONSCHEMA = False

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


class Registry:
    """Maps schema $id -> schema doc, loaded from a directory of *.schema.json."""

    def __init__(self):
        self.by_id = {}

    @classmethod
    def from_dir(cls, schema_dir) -> "Registry":
        reg = cls()
        for p in sorted(Path(schema_dir).glob("*.schema.json")):
            doc = _json.loads(Path(p).read_text(encoding="utf-8"))
            sid = doc.get("$id") or p.stem
            reg.by_id[sid] = doc
        return reg

    def schema_for(self, entity_type: str):
        return self.by_id.get("hub:" + entity_type)

    # --- jsonschema-backed registry (for the real validator) ---
    def _js_registry(self):
        from referencing import Registry as RefRegistry  # type: ignore
        from referencing.jsonschema import DRAFT202012  # type: ignore
        resources = [(sid, DRAFT202012.create_resource(doc)) for sid, doc in self.by_id.items()]
        return RefRegistry().with_resources(resources)


def _resolve_ref(ref: str, root_doc, registry: "Registry"):
    base, _, pointer = ref.partition("#")
    doc = registry.by_id.get(base) if base else root_doc
    if doc is None:
        return None, None
    target = doc
    if pointer:
        for raw in pointer.split("/"):
            if raw == "":
                continue
            tok = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and tok in target:
                target = target[tok]
            else:
                return None, None
    return target, doc


def _vtype(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "unknown"


def _check(schema, value, path, root_doc, registry, errors):
    if schema is True or schema == {}:
        return
    if schema is False:
        errors.append(f"{path}: schema false (value not allowed)")
        return
    if "$ref" in schema:
        target, sub_root = _resolve_ref(schema["$ref"], root_doc, registry)
        if target is None:
            errors.append(f"{path}: unresolved $ref {schema['$ref']}")
        else:
            _check(target, value, path, sub_root, registry, errors)
        # a $ref node may also carry sibling keywords (2020-12) - fall through to check them

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        vt = _vtype(value)
        ok = vt in types or (vt == "integer" and "number" in types)
        if not ok:
            errors.append(f"{path}: expected type {types}, got {vt}")
            return

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "pattern" in schema:
            pat = schema["pattern"]
            # JSON-Schema/ECMA-262: $ is end-of-INPUT (no trailing-\n exception that Python's re.search
            # allows). For anchored ^...$ patterns use fullmatch so e.g. "demo:task:0001\n" is rejected.
            ok = (re.fullmatch(pat, value) is not None) if pat.startswith("^") else (re.search(pat, value) is not None)
            if not ok:
                errors.append(f"{path}: does not match pattern {pat}")
        if schema.get("format") == "date-time" and not _DATETIME_RE.match(value):
            errors.append(f"{path}: not an ISO date-time")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(value):
                _check(item_schema, item, f"{path}[{i}]", root_doc, registry, errors)

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required '{req}'")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errors.append(f"{path}: additional property '{k}' not allowed")
        for k, sub in props.items():
            if k in value:
                _check(sub, value[k], f"{path}.{k}", root_doc, registry, errors)

    for sub in schema.get("allOf", []):
        _check(sub, value, path, root_doc, registry, errors)
    if "anyOf" in schema:
        if not any(not _trial(sub, value, root_doc, registry) for sub in schema["anyOf"]):
            errors.append(f"{path}: matches none of anyOf")
    if "if" in schema:
        if not _trial(schema["if"], value, root_doc, registry):
            if "then" in schema:
                _check(schema["then"], value, path, root_doc, registry, errors)
        elif "else" in schema:
            _check(schema["else"], value, path, root_doc, registry, errors)


def _trial(schema, value, root_doc, registry) -> list:
    errs = []
    _check(schema, value, "$", root_doc, registry, errs)
    return errs


def validate(entity: dict, entity_type: str, registry: "Registry") -> list:
    """Validate one entity against hub:<entity_type>. Returns list of error strings."""
    schema = registry.schema_for(entity_type)
    if schema is None:
        return [f"$: no schema for type '{entity_type}'"]
    if _HAVE_JSONSCHEMA:
        try:
            v = Draft202012Validator(schema, registry=registry._js_registry())
            return [f"{'.'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in v.iter_errors(entity)]
        except Exception as e:
            # Do NOT silently swallow: surface the fast-path failure to stderr, THEN fall back to the
            # stdlib subset validator so validation still happens (never silently passes).
            import sys
            print("hub_core.validate: jsonschema fast-path failed (%s: %s); using subset validator"
                  % (type(e).__name__, e), file=sys.stderr)
    errors = []
    _check(schema, entity, "$", schema, registry, errors)
    return errors
