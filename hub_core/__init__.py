"""hub_core — the portable, dependency-free project Hub engine.

Identical across every project hub (Django + single-file WSGI). Provides:
- canonical JSON + hashing (canonical, sha256_hex, content_hash)
- the append-only hash-chained OCC + idempotent event store (EventStore, ConflictError)
- a stdlib JSON-Schema 2020-12 subset validator + schema registry (Registry, validate)

Projections (state.json + generated docs + /hub.json) and the audit are built on this core by
each framework adapter. Repository doctrine and architecture documents define its contracts.
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
