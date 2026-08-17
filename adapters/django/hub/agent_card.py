"""Signed agent discovery at ``/.well-known/agent-card.json``.

This hub does not implement an A2A task transport. The document therefore uses current AgentCard
discovery vocabulary but advertises NO A2A ``supportedInterfaces`` and NO A2A streaming. Its
explicit ``x-hub.callableProtocols`` extension points only to the MCP endpoint that really exists.
That distinction matters: the board's SSE read lane is not an A2A streaming method, and the human
``/hub/`` page is not a JSON-RPC endpoint.

Skills are derived live from ``PROJECT/schema/task.schema.json`` so discovery cannot drift from
the task ``work_kind`` enum. Authentication metadata describes the ``X-Write-Token`` header; the
token value never appears.

When available, an ES256 JWS signature covers the card without ``signatures``, canonicalized as
UTF-8 JSON with sorted keys and minimal separators. The private key lives outside the repo
(``AGENT_CARD_KEY_FILE``, default ``~/.ssh/<app_name>_agent_card_es256.pem``) and is minted once.
"""
import base64
import json
import os
from pathlib import Path

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from hub_core import identity

from . import hub_app, mcp_server

_SKILL_BLURBS = {
    "product": "Build a product change to its task's own acceptance and prove it with a typed exit-0 receipt.",
    "content": "Author content the board tracks as queryable, non-executable work.",
    "corpus": "Curate corpus records that stay queryable without masquerading as executable work.",
    "governance": "Mechanize a board law with both-directions proof.",
    "verification": "Write falsifiable proof for a surface and capture its receipt.",
    "decision": "Record a ruling as an ADR and re-point the affected work to it.",
    "research": "Ground a question in dereferenceable evidence and file the result as a base entity.",
    "migration": "Move a surface between representations without losing coverage or history.",
    "duplicate": "Fold duplicate work into its canonical task.",
    "legacy": "Keep a legacy expansion queryable without presenting it as live work.",
}


def _default_key_file():
    return str(Path.home() / ".ssh" / f"{identity.app_name()}_agent_card_es256.pem")


DEFAULT_KEY_FILE = _default_key_file()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(obj) -> bytes:
    """The card's one canonical byte form (signer and verifier must agree byte-for-byte)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def work_kinds():
    """Read the task ``work_kind`` enum; an adopter may intentionally remove it."""
    try:
        doc = json.loads((hub_app.PROJECT / "schema" / "task.schema.json")
                         .read_text(encoding="utf-8"))
        return list(doc["properties"]["work_kind"]["enum"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def signing_key():
    """Load or mint the ES256 signing key at ``AGENT_CARD_KEY_FILE``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key_path = Path(os.environ.get("AGENT_CARD_KEY_FILE") or DEFAULT_KEY_FILE)
    if key_path.exists():
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return key


def public_jwk(key) -> dict:
    nums = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256",
            "x": _b64u(nums.x.to_bytes(32, "big")),
            "y": _b64u(nums.y.to_bytes(32, "big"))}


def _origin(value):
    """Normalize a configured public app origin; tolerate the old ``.../hub`` form."""
    value = str(value or "").strip().rstrip("/")
    if value.endswith("/hub"):
        value = value[:-4]
    return value


def build_card(hub_url=None, mcp_url=None) -> dict:
    """Build truthful discovery metadata for the board and its real MCP transport."""
    ident = identity.load()
    configured = _origin(os.environ.get("HUB_PUBLIC_URL") or ident["app_host"])
    if configured.startswith(("http://", "https://")):
        hub_url = hub_url or configured + "/hub/"
        mcp_url = mcp_url or configured + "/hub/api/mcp"
    else:
        hub_url = hub_url or "/hub/"
        mcp_url = mcp_url or "/hub/api/mcp"

    return {
        "name": f"{ident['key']}-hub-worker",
        "description": ("Discovery metadata for the interchangeable worker fleet coordinated by "
                        "this project's hash-chained Hub. This document is discovery-only; use "
                        "the advertised MCP endpoint for callable board operations."),
        "version": hub_app._git_head() or "unversioned",
        "documentationUrl": hub_url,
        # These are A2A capabilities. The board's SSE cursor is a separate read API and must not
        # be mislabeled as A2A streaming.
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "extendedAgentCard": False},
        # There is no A2A SendMessage/GetTask transport in this adapter. An empty declaration is
        # more useful than routing a standards client into the human page or the MCP server.
        "supportedInterfaces": [],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": f"work_kind.{kind}",
            "name": f"{kind} work",
            "description": _SKILL_BLURBS.get(kind, f"{kind} work on the hub board."),
            "tags": ["hub", "worker", kind],
        } for kind in work_kinds()],
        "x-hub": {
            "discoveryOnly": True,
            "identity": {field: ident[field] for field in
                         ("key", "brand", "app_name", "app_host", "worker_scheme")},
            "readUrl": hub_url,
            "callableProtocols": [{
                "name": "MCP",
                "protocolVersion": mcp_server.PROTOCOL_VERSION,
                "transport": "streamable-http-stateless",
                "url": mcp_url,
                "authentication": {
                    "type": "apiKey", "in": "header", "name": "X-Write-Token",
                    "valueIncluded": False,
                },
            }],
        },
    }


def sign_card(card: dict, key=None) -> dict:
    """Attach an ES256 JWS over ``protected + '.' + b64u(canonical(card))``."""
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
    """Serve signed discovery when possible and an explicitly unsigned card otherwise."""
    configured = _origin(os.environ.get("HUB_PUBLIC_URL") or identity.app_host())
    if configured.startswith(("http://", "https://")):
        card = build_card(configured + "/hub/", configured + "/hub/api/mcp")
    else:
        card = build_card(request.build_absolute_uri("/hub/"),
                          request.build_absolute_uri("/hub/api/mcp"))
    try:
        return JsonResponse(sign_card(card))
    except ImportError:
        card["signatureStatus"] = ("unsigned: the `cryptography` package is not installed on this "
                                   "host — install it to serve a verifiable ES256 JWS card")
    except (OSError, ValueError) as exc:
        card["signatureStatus"] = "unsigned: the signing key could not be loaded or minted (%s)" % (
            type(exc).__name__)
    card.setdefault("signatures", [])
    return JsonResponse(card)
