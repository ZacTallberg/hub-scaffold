"""hub_core — the portable, dependency-free core of the hub doctrine.

Identical across every project hub (Django + single-file WSGI). Provides:
- canonical JSON + hashing (canonical, sha256_hex, content_hash)
- the append-only hash-chained OCC + idempotent event store (EventStore, ConflictError)
- a stdlib JSON-Schema 2020-12 subset validator + schema registry (Registry, validate)

Projections (state.json + .md docs + /hub.json) and the audit are built ON TOP of this core,
per stack. See PROJECT/DOCTRINE.md in the host repo.
"""
from .canonical import canonical, canonical_bytes, content_hash, sha256_hex
from .store import ConflictError, EventStore
from .validate import Registry, validate

__all__ = [
    "canonical", "canonical_bytes", "content_hash", "sha256_hex",
    "EventStore", "ConflictError",
    "Registry", "validate",
]
__version__ = "0.1.0"
