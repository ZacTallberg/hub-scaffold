"""Small, durable authority registry for autonomous Hub workers.

The registry deliberately stores no usable bearer secret: an issued token is returned once and
only its SHA-256 digest is persisted.  Identity is immutable for the life of a credential; rotate
by issuing a new credential and revoke the old one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from .process_lock import ProcessFileLock


TOKEN_PREFIX = "hub-agent"
REGISTRY_FILE = "agent-credentials.json"
_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_SCOPE = re.compile(r"^(?:\*|[a-z][a-z0-9._-]*(?::(?:\*|[a-z][a-z0-9._-]*))?)$")


class CredentialError(ValueError):
    """A credential cannot be issued or authenticated."""


@dataclass(frozen=True)
class AuthContext:
    subject: str
    credential_id: str
    scopes: tuple[str, ...]
    actor_kind: str
    mode: str
    expires_at: float | None = None

    def allows(self, required: str | None) -> bool:
        if not required or "*" in self.scopes or required in self.scopes:
            return True
        namespace = required.split(":", 1)[0] + ":*"
        return namespace in self.scopes

    def public(self) -> dict:
        return {
            "subject": self.subject,
            "credential_id": self.credential_id,
            "scopes": list(self.scopes),
            "mode": self.mode,
            "expires_at": self.expires_at,
        }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CredentialRegistry:
    def __init__(self, hub_dir):
        self.root = Path(hub_dir)
        self.path = self.root / REGISTRY_FILE

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "credentials": {}}
        except (OSError, ValueError) as exc:
            raise CredentialError("credential registry is unavailable or malformed") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("credentials"), dict):
            raise CredentialError("credential registry is malformed")
        return raw

    def _write(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def issue(self, subject: str, scopes, ttl_s: int, *, issued_by: str) -> tuple[str, dict]:
        subject = str(subject or "").strip()
        if not _SUBJECT.fullmatch(subject):
            raise CredentialError("subject must be a stable 1..256 character agent id")
        if isinstance(scopes, str):
            scopes = [scopes]
        scopes = sorted({str(scope).strip() for scope in (scopes or [])})
        if not scopes or any(not _SCOPE.fullmatch(scope) for scope in scopes):
            raise CredentialError("scopes must be a non-empty list of operation names")
        try:
            ttl_s = int(ttl_s)
        except (TypeError, ValueError) as exc:
            raise CredentialError("ttl_s must be an integer") from exc
        if ttl_s < 60 or ttl_s > 31_536_000:
            raise CredentialError("ttl_s must be 60..31536000")

        now = time.time()
        credential_id = secrets.token_hex(12)
        token = f"{TOKEN_PREFIX}.{credential_id}.{secrets.token_urlsafe(32)}"
        record = {
            "credential_id": credential_id,
            "subject": subject,
            "scopes": scopes,
            "token_sha256": _digest(token),
            "issued_at": _iso(now),
            "issued_at_epoch": now,
            "expires_at": _iso(now + ttl_s),
            "expires_at_epoch": now + ttl_s,
            "issued_by": issued_by,
            "revoked_at": None,
            "revoked_at_epoch": None,
            "revoked_by": None,
        }
        with ProcessFileLock(self.root, name=".agent-credentials.lock", timeout=30):
            data = self._read()
            data["credentials"][credential_id] = record
            self._write(data)
        return token, self._public_record(record)

    def revoke(self, credential_id: str, *, revoked_by: str) -> dict:
        credential_id = str(credential_id or "").strip()
        with ProcessFileLock(self.root, name=".agent-credentials.lock", timeout=30):
            data = self._read()
            record = data["credentials"].get(credential_id)
            if not record:
                raise CredentialError("credential not found")
            if not record.get("revoked_at_epoch"):
                now = time.time()
                record["revoked_at"] = _iso(now)
                record["revoked_at_epoch"] = now
                record["revoked_by"] = revoked_by
                self._write(data)
        return self._public_record(record)

    def authenticate(self, token: str) -> AuthContext:
        parts = str(token or "").split(".", 2)
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1]:
            raise CredentialError("invalid agent credential")
        credential_id = parts[1]
        data = self._read()
        record = data["credentials"].get(credential_id)
        supplied = _digest(token)
        expected = str((record or {}).get("token_sha256") or "0" * 64)
        if not record or not hmac.compare_digest(supplied, expected):
            raise CredentialError("invalid agent credential")
        if record.get("revoked_at_epoch"):
            raise CredentialError("agent credential is revoked")
        expires = float(record.get("expires_at_epoch") or 0)
        if expires <= time.time():
            raise CredentialError("agent credential is expired")
        return AuthContext(
            subject=record["subject"],
            credential_id=credential_id,
            scopes=tuple(record.get("scopes") or ()),
            actor_kind="scoped-agent",
            mode="scoped-agent",
            expires_at=expires,
        )

    def list_public(self) -> list[dict]:
        rows = [self._public_record(row) for row in self._read()["credentials"].values()]
        return sorted(rows, key=lambda row: (row.get("subject") or "", row.get("credential_id") or ""))

    @staticmethod
    def _public_record(record: dict) -> dict:
        return {key: record.get(key) for key in (
            "credential_id", "subject", "scopes", "issued_at", "expires_at", "issued_by",
            "revoked_at", "revoked_by",
        )}


def shared_root_context() -> AuthContext:
    return AuthContext(
        subject="shared-root",
        credential_id="shared-root-compat",
        scopes=("*",),
        actor_kind="shared-root-compat",
        mode="shared-root-compat",
    )
