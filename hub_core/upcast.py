"""Upcasting registry — read-side ledger evolution without ever rewriting a stored byte.

Old event payloads are transformed to the current shape ON READ via a {(event_type, from_v):
(fn, to_v)} registry. Payloads carry a `schema_v` convention (absent == 1); project.state()
applies the chain before folding, so the hash-chained bytes in events.jsonl stay immutable while
every consumer sees the current shape. `schema_v` is transport metadata: apply() strips it after
upcasting so folded entities never carry it into schema validation.

The registry is deliberately linear per type (v1->v2->v3...); current_version is the highest
registered target. The schema_evolution adapter (hub_app) audits that every stored event can
reach its type's current version — a gap in the chain is a ledger you can no longer read.
"""

REGISTRY = {}   # (event_type, from_v) -> (fn, to_v)

# A verification_run receipt names its OWN shape (in-toto/SLSA predicate style) instead of leaving
# a future migration to sniff which fields happen to be present — "has subject_tree_sha" is a
# guess, "predicate .../verification_run/v1" is a fact. Built from the project identity, never a
# literal host: a re-instantiated project renames itself by editing PROJECT/project.json alone.
RECEIPT_PREDICATE_SUFFIX = "verification_run/v1"


def receipt_predicate_type() -> str:
    from . import identity
    ident = identity.load()
    host = str(ident.get("app_host") or f"urn:hub:{ident.get('key') or 'hub'}").rstrip("/")
    return f"{host}/{RECEIPT_PREDICATE_SUFFIX}"


def receipt_predicate_version(receipt) -> int:
    """The receipt's declared shape version — the deterministic discriminator an upcaster keys on.
    0 when the receipt carries no predicate_type (every completion before the receipt store) or
    one this project does not issue, so 'untyped' is a distinct answer from 'v1', never a guess."""
    declared = str((receipt or {}).get("predicate_type") or "")
    if not declared:
        return 0
    prefix, _, version = declared.rpartition("/v")
    try:
        expected = receipt_predicate_type().rpartition("/v")[0]
    except (OSError, KeyError, ValueError):
        return 0
    if prefix != expected or not version.isdigit():
        return 0
    return int(version)


def register(event_type, from_v, fn, to_v=None):
    REGISTRY[(event_type, from_v)] = (fn, to_v if to_v is not None else from_v + 1)


def unregister(event_type, from_v):
    REGISTRY.pop((event_type, from_v), None)


def current_version(event_type) -> int:
    """The highest version any registered upcaster for this type produces (1 when none)."""
    targets = [to_v for (t, _f), (_fn, to_v) in REGISTRY.items() if t == event_type]
    return max(targets) if targets else 1


def apply_versioned(event_type, payload):
    """(upcasted payload copy, terminal version). Never mutates the input; `schema_v` is kept
    on the result so callers can inspect the terminal version — apply() strips it for folding."""
    v = payload.get("schema_v") or 1
    out = payload
    while (event_type, v) in REGISTRY:
        fn, to_v = REGISTRY[(event_type, v)]
        out = dict(fn(dict(out)))
        v = to_v
        out["schema_v"] = v
    return out, v


def apply(event_type, payload):
    """The fold-side entry: upcast to current shape, strip the transport version key. Fast path:
    an empty registry and an unversioned payload pass through untouched (zero copies)."""
    if not REGISTRY and "schema_v" not in payload:
        return payload
    out, _v = apply_versioned(event_type, payload)
    if "schema_v" in out:
        out = {k: val for k, val in out.items() if k != "schema_v"}
    return out
