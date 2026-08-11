"""Signed, single-use capabilities for local worker launches.

A custom URL protocol is a process-launch surface: any page can try to navigate to it.  Hub
therefore binds every browser launch to a short-lived grant covering the action, task, worker
count, issuer, and nonce.  The workstation burns the nonce at the issuing Hub before it starts a
process.  Browser code never receives the general Hub write token or the signing secret.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .process_lock import ProcessFileLock

DEFAULT_TTL_S = 120
MAX_TTL_S = 300
MAX_COUNT = 8

_HUB_DIR_OVERRIDE = contextvars.ContextVar("hub_launch_grant_dir", default=None)


def hub_dir() -> Path:
    override = _HUB_DIR_OVERRIDE.get()
    return Path(override or os.environ.get("HUB_DIR") or (Path.cwd() / "PROJECT" / ".hub"))


@contextlib.contextmanager
def using_hub_dir(path):
    """Bind grant I/O to an adapter's resolved board without changing process-global env state."""
    token = _HUB_DIR_OVERRIDE.set(Path(path))
    try:
        yield
    finally:
        _HUB_DIR_OVERRIDE.reset(token)


def _secret() -> bytes:
    env = os.environ.get("HUB_ATTEST_SECRET")
    if env:
        return env.encode("utf-8")
    root = hub_dir()
    path = root / ".attest-secret"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value.encode("utf-8")
    except OSError:
        pass
    root.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(root / "grants", name=".secret-init.lock", timeout=30):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value.encode("utf-8")
        except OSError:
            pass
        value = uuid.uuid4().hex + uuid.uuid4().hex
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return value.encode("utf-8")


def _basis(action, task, count, nonce, expires, issuer="") -> bytes:
    return json.dumps(
        {
            "action": str(action or ""),
            "task": str(task or ""),
            "count": int(count),
            "nonce": str(nonce or ""),
            "expires": int(expires),
            "issuer": str(issuer or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign(basis: bytes) -> str:
    return hmac.new(_secret(), basis, hashlib.sha256).hexdigest()


def _safe_issuer(url: str) -> str:
    """Return a canonical trusted issuer URL or raise for unsafe network destinations."""
    value = str(url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("issuer URL cannot carry credentials, query parameters, or a fragment")
    loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("issuer URL must use HTTPS (HTTP is allowed only for loopback)")
    if not parsed.netloc or not parsed.path.endswith("/api/launch-grant/consume"):
        raise ValueError("issuer URL must name the Hub launch-grant consume endpoint")
    return value


def mint(action="start", task="", count=1, ttl_s=DEFAULT_TTL_S, issuer="") -> dict:
    count = int(count)
    ttl_s = int(ttl_s)
    if action != "start":
        raise ValueError("only the 'start' launch action is supported")
    if count < 1 or count > MAX_COUNT:
        raise ValueError(f"count must be 1..{MAX_COUNT}, got {count}")
    if ttl_s < 1 or ttl_s > MAX_TTL_S:
        raise ValueError(f"ttl_s must be 1..{MAX_TTL_S}, got {ttl_s}")
    if issuer:
        issuer = _safe_issuer(issuer)
    nonce = uuid.uuid4().hex
    expires = int(time.time()) + ttl_s
    basis = _basis(action, task, count, nonce, expires, issuer)
    grant = {
        "action": action,
        "task": str(task or ""),
        "count": count,
        "nonce": nonce,
        "expires": expires,
        "issuer": issuer,
        "sig": _sign(basis),
    }
    _record("launch.granted", grant)
    return grant


def encode(grant: dict) -> str:
    raw = json.dumps(grant, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(token: str) -> dict:
    pad = "=" * (-len(token or "") % 4)
    result = json.loads(base64.urlsafe_b64decode((token or "") + pad).decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("grant is not an object")
    return result


def _nonce_path(nonce) -> Path:
    safe = "".join(c for c in str(nonce or "") if c.isalnum())[:64]
    if not safe:
        raise ValueError("grant nonce is empty")
    directory = hub_dir() / "grants"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe}.used"


def _record(kind, grant, reason=None) -> None:
    try:
        directory = hub_dir() / "grants"
        directory.mkdir(parents=True, exist_ok=True)
        row = {
            "at": int(time.time()),
            "kind": kind,
            "reason": reason,
            "action": (grant or {}).get("action"),
            "task": (grant or {}).get("task"),
            "count": (grant or {}).get("count"),
            "nonce": (grant or {}).get("nonce"),
            "issuer": (grant or {}).get("issuer"),
        }
        with ProcessFileLock(directory, name=".decisions.lock", timeout=30):
            with (directory / "decisions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        pass


def consume(token, action="start", task="", count=1):
    """Validate, bind, and atomically burn a locally issued grant."""
    try:
        grant = decode(token)
        granted_count = int(grant.get("count") or 0)
        expires = int(grant.get("expires") or 0)
        expected = _sign(
            _basis(
                grant.get("action"),
                grant.get("task"),
                granted_count,
                grant.get("nonce"),
                expires,
                grant.get("issuer") or "",
            )
        )
    except Exception:
        _record("launch.refused", None, "malformed grant")
        return False, "malformed grant"
    if not hmac.compare_digest(expected, str(grant.get("sig") or "")):
        _record("launch.refused", grant, "signature mismatch")
        return False, "signature mismatch"
    if expires <= time.time():
        _record("launch.refused", grant, "expired")
        return False, "grant expired"
    if str(grant.get("action") or "") != str(action or ""):
        _record("launch.refused", grant, "action mismatch")
        return False, "grant action does not match the requested action"
    if str(grant.get("task") or "") != str(task or ""):
        _record("launch.refused", grant, "task mismatch")
        return False, "grant task does not match the requested task"
    try:
        wanted = int(count)
    except (TypeError, ValueError):
        wanted = 0
    if wanted < 1 or wanted > granted_count:
        _record("launch.refused", grant, "count above grant")
        return False, f"grant authorizes at most {granted_count} worker(s)"
    try:
        path = _nonce_path(grant.get("nonce"))
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(int(time.time())).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        _record("launch.refused", grant, "replayed")
        return False, "grant already used"
    except OSError as exc:
        _record("launch.refused", grant, f"nonce store unwritable: {exc}")
        return False, "cannot record single use; launch refused"
    _record("launch.consumed", grant)
    return True, wanted


def _read_write_token(token_file=None) -> str:
    candidate = token_file or os.environ.get("HUB_SYNC_TOKEN_FILE")
    if candidate:
        token = Path(candidate).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise OSError("Hub token file is empty")
        return token
    env = os.environ.get("HUB_WRITE_TOKEN", "").strip()
    if env:
        return env
    raise OSError("set HUB_WRITE_TOKEN or configure a Hub token file")


def _consume_remote(issuer, token, action, task, count, token_file=None):
    try:
        write_token = _read_write_token(token_file)
    except OSError as exc:
        return False, str(exc)
    body = json.dumps(
        {"consume": token, "action": action, "task": task, "count": int(count)}
    ).encode("utf-8")
    request = urllib.request.Request(
        issuer,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Write-Token": write_token,
            "User-Agent": "hub-worker-grant-consumer/1",
        },
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        # A redirect must never carry the workstation's Hub token to another origin. The configured
        # issuer is exact; a moved endpoint is a configuration error to fix, not one to follow.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
            message = ((payload.get("errors") or [{}])[0].get("msg") or f"HTTP {exc.code}")
        except Exception:
            message = f"HTTP {exc.code}"
        return False, f"issuing Hub refused grant: {message}"
    except (OSError, ValueError) as exc:
        return False, f"issuing Hub unreachable: {exc}"
    data = payload.get("data") or {}
    if not data.get("authorized"):
        return False, "issuing Hub did not authorize the grant"
    return True, int(data.get("count") or count)


def consume_authoritative(
    token,
    action="start",
    task="",
    count=1,
    *,
    trusted_issuer="",
    token_file=None,
    remote_consumer=None,
):
    """Consume at the grant's issuer, never at a destination controlled by the grant itself."""
    try:
        grant = decode(token)
    except Exception:
        return consume(token, action, task, count)
    issuer = str(grant.get("issuer") or "")
    if not issuer:
        return consume(token, action, task, count)
    try:
        expected = _safe_issuer(trusted_issuer)
        actual = _safe_issuer(issuer)
    except ValueError as exc:
        return False, str(exc)
    if not trusted_issuer or not hmac.compare_digest(actual, expected):
        return False, "grant issuer does not match this workstation's configured Hub"
    consumer = remote_consumer or _consume_remote
    return consumer(actual, token, action, task, count, token_file)


def main(argv=None):
    parser = argparse.ArgumentParser(description="mint or consume a single-use worker launch grant")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mint", action="store_true")
    mode.add_argument("--consume", metavar="GRANT")
    mode.add_argument("--consume-authoritative", metavar="GRANT")
    parser.add_argument("--action", default="start")
    parser.add_argument("--task", default="")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    parser.add_argument("--issuer-url", default="")
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)
    if args.mint:
        print(encode(mint(args.action, args.task, args.count, args.ttl_s, args.issuer_url)))
        return 0
    if args.consume is not None:
        ok, detail = consume(args.consume, args.action, args.task, args.count)
    else:
        ok, detail = consume_authoritative(
            args.consume_authoritative,
            args.action,
            args.task,
            args.count,
            trusted_issuer=args.issuer_url,
            token_file=args.token_file,
        )
    if ok:
        print(f"authorized: {detail} worker(s)")
        return 0
    print(f"REFUSED: {detail}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
