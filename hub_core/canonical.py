"""Deterministic canonical JSON (RFC-8785-style) + hashing. Stdlib only.

Canonical form = sorted keys, compact separators, non-ASCII preserved. Used for byte-identical
diffs, content etags, and the event hash-chain. Identical bytes across every project hub.
"""
import hashlib
import json


def canonical(obj) -> str:
    """Canonical JSON string: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj) -> bytes:
    return canonical(obj).encode("utf-8")


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(obj) -> str:
    """Stable content hash of an object (for etags / dedup)."""
    return sha256_hex(canonical_bytes(obj))
