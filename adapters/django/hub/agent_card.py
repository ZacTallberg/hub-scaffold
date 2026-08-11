"""A2A AgentCard at /.well-known/agent-card.json — the standards-speaking edge (ADR-0009 gap B).

A READ-ONLY discovery document in the Agent2Agent v1.0.1 AgentCard shape, so any A2A consumer can
discover this hub's worker roles (one skill per task work_kind, derived live from
PROJECT/schema/task.schema.json — the card can never drift from the enum) and how to authenticate
(securitySchemes DESCRIBES the X-Write-Token header; the token VALUE never appears — the
self-test seeds a canary and asserts absence). Interop lives at the edge: this module reads the
schema and signs a dict; it never appends to the ledger.

Signature: an ES256 JWS (RFC 7515) in the card's `signatures` array. The payload is the card
without `signatures`, canonicalized as UTF-8 JSON with sorted keys and minimal separators (the
verifier MUST canonicalize the same way — documented here as the card's one canonical form). The
public JWK rides in the protected header, so verification is self-contained; the private key
lives OUTSIDE the repo (AGENT_CARD_KEY_FILE env, default ~/.ssh/<app_name>_agent_card_es256.pem) and is
generated once on first use — a fresh box mints its own identity rather than shipping one.
"""
import base64
import json
import os
from pathlib import Path

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from hub_core import identity

from . import hub_app

_IDENT = identity.load()

PROTOCOL_VERSION = "1.0.1"
def _default_key_file():
    app = "hub"
    try:
        from hub_core import identity
        app = identity.load().get("app_name") or app
    except Exception:
        pass  # absorbs: identity file absent/corrupt - the generic "hub" key name still works
    return str(Path.home() / ".ssh" / f"{app}_agent_card_es256.pem")


DEFAULT_KEY_FILE = _default_key_file()

_SKILL_BLURBS = {
    "product": "Build a product change to its task's own acceptance and prove it with a typed exit-0 receipt.",
    "content": "Author content the board tracks as queryable, non-executable work.",
    "corpus": "Curate corpus records that stay queryable without masquerading as executable work.",
    "governance": "Mechanize a board law: guards, gates, and audits with both-directions self-tests.",
    "verification": "Write the falsifiable proof for a surface: a self-test that fires on a seeded defect and stays quiet on a healthy run.",
    "decision": "Record a ruling as an ADR and re-point the affected work to it.",
    "research": "Ground a question in dereferenceable evidence and file the findings as board entities.",
    "migration": "Move a surface between representations without losing coverage or history.",
    "duplicate": "Fold duplicate work into its canonical task.",
    "legacy": "Keep a legacy expansion queryable without presenting it as live work.",
}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(obj) -> bytes:
    """The card's ONE canonical byte form (signer and verifier must agree byte-for-byte)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def work_kinds():
    """The task work_kind enum, read from the schema itself so the card tracks it verbatim.

    An adopter who has removed work_kind gets a card with no skills rather than a 500: the card is
    a DISCOVERY document, and failing to serve it hides the whole hub from every A2A consumer over
    a field one instance chose not to keep."""
    try:
        doc = json.loads((hub_app.BASE_DIR / "PROJECT" / "schema" / "task.schema.json")
                         .read_text(encoding="utf-8"))
        return list(doc["properties"]["work_kind"]["enum"])
    except (OSError, ValueError, KeyError, TypeError):
        return []  # absorbs: schema absent/unreadable or work_kind removed by the adopter


def signing_key():
    """Load (or mint once) the ES256 signing key at AGENT_CARD_KEY_FILE — never inside the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    path = Path(os.environ.get("AGENT_CARD_KEY_FILE") or DEFAULT_KEY_FILE)
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return key


def public_jwk(key) -> dict:
    nums = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256",
            "x": _b64u(nums.x.to_bytes(32, "big")),
            "y": _b64u(nums.y.to_bytes(32, "big"))}


def build_card(base_url=None) -> dict:
    """The card. `base_url` is where a consumer should talk to this hub.

    Preference order is deliberate: an explicit HUB_PUBLIC_URL (the operator knows the public
    name behind a proxy), then whatever the REQUEST arrived on, then a relative path. Deriving it
    from the live request is more truthful than any config — a card that advertises a hostname
    the caller cannot reach is a discovery document that mis-routes every client that trusts it.
    """
    base_url = os.environ.get("HUB_PUBLIC_URL") or base_url or "/hub/"
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": f"{_IDENT['key']}-hub-worker",
        "description": ("Interchangeable worker fleet sequenced by the project hub's hash-chained "
                        "board: pull the top ready task, claim its lease, build to the task's own "
                        "acceptance, prove it with a typed exit-0 verification receipt."),
        "url": base_url,
        "preferredTransport": "JSONRPC",
        "version": hub_app._git_head() or "unversioned",
        "capabilities": {"streaming": True,            # /hub/api/live SSE lane
                         "pushNotifications": False,
                         "stateTransitionHistory": True},  # append-only hash-chained ledger
        "securitySchemes": {"hubWriteToken": {
            "type": "apiKey", "in": "header", "name": "X-Write-Token",
            "description": "Write token for /hub/api/ mutations; reads under /hub need a grant. "
                           "Obtain out-of-band from the operator — never from this card."}},
        "security": [{"hubWriteToken": []}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": f"work_kind.{k}",
            "name": f"{k} work",
            "description": _SKILL_BLURBS.get(k, f"{k} work on the hub board."),
            "tags": ["hub", "worker", k],
        } for k in work_kinds()],
    }


def sign_card(card: dict, key=None) -> dict:
    """Attach an A2A AgentCardSignature: ES256 JWS over protected + '.' + b64u(canonical(card))."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    key = key or signing_key()
    unsigned = {k: v for k, v in card.items() if k != "signatures"}
    protected = _b64u(canonical({"alg": "ES256", "jwk": public_jwk(key)}))
    payload = _b64u(canonical(unsigned))
    der = key.sign(f"{protected}.{payload}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {**unsigned, "signatures": [{"protected": protected, "signature": sig}]}


@require_GET
def agent_card_view(request):
    """The card, signed when this box CAN sign and honestly unsigned when it cannot.

    Signing needs `cryptography`, which the scaffold does not install by default, and a writable
    key path. Neither is a reason to 500: this is the document a standard A2A client fetches to
    discover the hub at all, so failing to serve it hides everything behind it. An unsigned card
    says `signatures: []` and states why in `signatureStatus`, which a consumer can act on — a 500
    is indistinguishable from "no such hub"."""
    card = build_card(base_url=request.build_absolute_uri("/hub/"))
    try:
        return JsonResponse(sign_card(card))
    except ImportError:
        card["signatureStatus"] = ("unsigned: the `cryptography` package is not installed on this "
                                   "host — install it to serve a verifiable ES256 JWS card")
    except (OSError, ValueError) as exc:
        # absorbs: key path unwritable/unreadable, or a malformed existing key
        card["signatureStatus"] = "unsigned: the signing key could not be loaded or minted (%s)" % (
            type(exc).__name__)
    card.setdefault("signatures", [])
    return JsonResponse(card)
