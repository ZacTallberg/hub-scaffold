"""Bounded, redacted operational error telemetry — the board's answer to "is anything
broken?", covering surfaces the audit cannot see (a served 500, a browser failure, a
satellite service's exception, a worker whose tooling died).

Framework-free; every function takes the hub dir explicitly. This is observed runtime
state, not project truth, so it lives beside presence/ and claims/ in HUB_DIR, never in the
append-only ledger. The store is deliberately small and fail-soft: error reporting must
never become the reason a request fails.

Rules this module holds, each paid for in production on the origin system:

* REDACT AT THE DOOR. Messages routinely contain pasted command output; secrets are
  stripped before the row exists.
* A REPEATING ERROR IS ONE FACT. Per-fingerprint throttling with a preserved occurrence
  count — an unthrottled flood does not just add noise, it EVICTS every other error from a
  bounded store.
* CLASSIFY EXTERNAL NOISE BEFORE THE THROTTLE. Foreign-client traffic (a scanner's bad
  Host header) is recorded as a warning and flagged external, so a suppressed repeat
  reports the same severity its written sibling did and can never crowd the top of the
  stream.
* ACK COLLAPSES, NEVER DELETES; a clear is bounded by AGE or by ACK, never "everything" —
  a clear must never be the operation that destroys evidence of a failure nobody has
  looked at. only_acked is a RESTRICTION: an age bound beside it narrows the acked set.
* AN EMPTY LIST MUST SAY WHETHER IT IS EVERYTHING. coverage() names each channel that CAN
  report, whether it has, and what is knowingly out of scope — silence about a gap reads
  as coverage.
* A BROKEN STORE MUST NOT READ AS A QUIET ONE. A failed write marks the read surface
  impaired instead of silently returning fewer rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .process_lock import ProcessFileLock

MAX_BYTES = 2 * 1024 * 1024
KEEP_ROWS = 600
READ_LIMIT = 120
THROTTLE_S = 900
_LAST_SEEN_MAX = 2000

# Process-local, keyed per hub dir so two boards in one process (a test hub beside the real
# one) never share throttle windows or impairment flags.
_LAST_SEEN: dict = {}
_WRITE_FAILURE: dict = {}

_SECRET_PAIR = re.compile(
    r"(?i)\b(authorization|password|passwd|secret|token|cookie|api[-_ ]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL_SECRET = re.compile(r"(?i)([?&](?:k|key|token|secret|code)=)[^&#\s]+")

# Sources that are not THIS application failing: an external client sending a Host header
# we do not serve is a scanner or a stale bookmark, worth counting and worth never letting
# near the top of the stream.
_EXTERNAL_NOISE = ("django.security.disallowedhost", "django.security.suspiciousoperation")


def _errors_path(hub_dir) -> Path:
    return Path(hub_dir) / "errors.jsonl"


def _acked_path(hub_dir) -> Path:
    return Path(hub_dir) / "errors-acked.json"


def _clean(value, limit=800) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").strip()
    text = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _URL_SECRET.sub(r"\1[REDACTED]", text)
    return text[:limit]


def _context(value) -> dict:
    if not isinstance(value, dict):
        return {}
    # "machine", "app" and "agent" are the three things a reader most needs: a row that
    # says WHAT broke and nothing about WHERE makes a worker's failure and a service's
    # failure look identical.
    allowed = ("component", "method", "path", "status", "reason", "operation", "release",
               "machine", "app", "agent", "url")
    return {k: _clean(value.get(k), 240) for k in allowed if value.get(k) not in (None, "")}


def _origin(source: str, context: dict) -> dict:
    """Where a row came from, as a FIELD — provenance encoded only in a source-naming
    convention is provenance a reader has to know to decode."""
    src = (source or "").lower()
    out = {}
    if src.startswith("agent."):
        parts = source.split(".")
        out["origin"] = "agent"
        if len(parts) > 1:
            out["origin_agent"] = parts[1]
    elif src.startswith("browser."):
        out["origin"] = "browser"
    elif src.startswith("app."):
        out["origin"] = "app"
        parts = source.split(".")
        if len(parts) > 1:
            out["origin_app"] = parts[1]
    elif src.startswith(_EXTERNAL_NOISE):
        out["origin"] = "external"
    else:
        out["origin"] = "hub-server"
    # An explicitly reported machine BEATS anything decoded from the source string: the
    # source key names the agent, which is a person's identity, not a computer's.
    if context.get("machine"):
        out["origin_machine"] = context["machine"]
    if context.get("app"):
        out["origin_app"] = context["app"]
    return out


# Every way an error can reach this stream, and the ones that CANNOT. A list of errors
# answers "what broke"; it never answers "is this everything?" — and a reader who takes an
# empty card as a healthy system while a channel is dark is worse off than one with no card.
CHANNELS = (
    {"key": "django.request", "label": "Host app 5xx",
     "wired": "LOGGING -> hub_core.errorlog.HubErrorHandler (django.request, ERROR+)",
     "match": ("django.request", "django.server")},
    {"key": "hub", "label": "Hub internals",
     "wired": "explicit record() calls in hub code (auth refusals included)",
     "match": ("hub.",)},
    {"key": "browser", "label": "Board browser failures",
     "wired": "POST /hub/api/client-error from the board (same-origin, CSRF)",
     "match": ("browser.",)},
    {"key": "agent", "label": "Worker-side failures",
     "wired": "POST /hub/api/agent-error from a worker whose own tooling failed",
     "match": ("agent.",)},
    {"key": "app", "label": "Satellite services",
     "wired": "POST /hub/api/app-error from each service's error forwarder",
     "match": ("app.",)},
    {"key": "external", "label": "Foreign/scanner traffic",
     "wired": "django.security.* — recorded as warnings, never as our errors",
     "match": ("django.security.",)},
)

# What this stream provably does NOT see — stated, because silence about a gap reads as
# coverage, which is the failure this surface exists to prevent.
NOT_COVERED = (
    "A service reaches this stream only once its forwarder is wired and pointed here; a "
    "service that forwards nothing is silent, not healthy.",
    "Process stdout/stderr before logging starts (a boot crash) never reaches here.",
    "A worker with no network path to the hub fails locally and appears only if it "
    "reports after reconnecting.",
)


def coverage(rows) -> dict:
    """Which reporting channels are live, which are silent, and what is out of scope."""
    now = time.time()
    out = []
    for ch in CHANNELS:
        seen = [r for r in rows
                if str(r.get("source") or "").lower().startswith(
                    tuple(m.lower() for m in ch["match"]))]
        newest = max((float(r.get("epoch") or 0) for r in seen), default=0.0)
        out.append({
            "key": ch["key"], "label": ch["label"], "wired": ch["wired"],
            "rows": len(seen),
            "age_s": int(now - newest) if newest else None,
            # "silent" is not "broken" — it is "nothing in the window", which is exactly
            # what a reader needs in order to judge an empty card for themselves.
            "silent": not seen,
        })
    return {"channels": out, "window_rows": len(rows), "not_covered": list(NOT_COVERED)}


def _compact_locked(hub_dir) -> None:
    path = _errors_path(hub_dir)
    try:
        if not path.exists() or path.stat().st_size <= MAX_BYTES:
            return
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_ROWS:]
        temp = path.with_suffix(".compact.tmp")
        temp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        return


def record(hub_dir, source, message, *, severity="error", code="runtime_error",
           details="", context=None) -> dict:
    """Append one safe diagnostic row and return its public representation. Throttled per
    fingerprint (count preserved on the next written row); redacted; bounded; fail-soft."""
    key = str(hub_dir)
    now = time.time()
    clean_source = _clean(source or "runtime", 120)
    clean_code = _clean(code or "runtime_error", 120)
    clean_message = _clean(message or "Unspecified operational error")
    clean_details = _clean(details, 2000)
    fingerprint = hashlib.sha256(
        f"{clean_source}\0{clean_code}\0{clean_message}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    row = {
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
        "epoch": round(now, 3),
        "severity": severity if severity in {"warning", "error", "critical"} else "error",
        "source": clean_source,
        "code": clean_code,
        "message": clean_message,
        "fingerprint": fingerprint,
    }
    ctx = _context(context)
    if ctx:
        row["context"] = ctx
    row.update(_origin(clean_source, ctx))
    if clean_details:
        row["details"] = clean_details
    if str(clean_source).lower().startswith(_EXTERNAL_NOISE):
        # Classified BEFORE the throttle, so a suppressed repeat reports the same severity
        # its written sibling did.
        row["severity"] = "warning"
        row["external"] = True
    seen_map = _LAST_SEEN.setdefault(key, {})
    seen = seen_map.get(fingerprint)
    if seen and (now - seen[0]) < THROTTLE_S:
        seen_map[fingerprint] = (seen[0], seen[1] + 1)
        row["suppressed_since"] = seen[1] + 1
        return row
    if seen:
        row["occurrences_since_last"] = seen[1]
    seen_map[fingerprint] = (now, 1)
    if len(seen_map) > _LAST_SEEN_MAX:
        # A long-lived process sees an unbounded stream of distinct fingerprints; windows
        # older than the throttle have nothing left to suppress. The map stays BOUNDED.
        for stale in [fp for fp, (first, _) in seen_map.items() if (now - first) >= THROTTLE_S]:
            seen_map.pop(stale, None)

    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            _compact_locked(hub_dir)
            with _errors_path(hub_dir).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
        _WRITE_FAILURE.pop(key, None)
    except Exception as exc:                                 # noqa: BLE001 - must not raise
        # Logging cannot raise into the failing request, but the board must not call a
        # broken store "quiet": this flag makes the read surface explicitly impaired.
        _WRITE_FAILURE[key] = {"reason": _clean(type(exc).__name__, 80), "at": row["ts"]}
    return row


def read(hub_dir, limit=READ_LIMIT) -> tuple[list, dict]:
    """Newest-first rows plus honest availability/retention metadata (the WINDOW is named:
    a bounded log that does not say what it is showing lies about what it holds)."""
    key = str(hub_dir)
    path = _errors_path(hub_dir)
    failure = _WRITE_FAILURE.get(key)
    try:
        if not path.exists():
            hub = Path(hub_dir)
            writable = os.access(hub if hub.exists() else hub.parent, os.W_OK)
            metadata = {"available": writable and failure is None,
                        "retention": KEEP_ROWS, "stored": 0, "bytes": 0}
            if failure:
                metadata.update(failure)
            elif not writable:
                metadata["reason"] = "store_not_writable"
            return [], metadata
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        decoded, malformed = [], 0
        for line in raw:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    decoded.append(item)
                else:
                    malformed += 1
            except (TypeError, ValueError):
                malformed += 1
        window = max(1, min(int(limit), READ_LIMIT))
        rows = decoded[-window:]
        rows.reverse()
        acked = read_acked(hub_dir)
        for row in rows:
            mark = acked.get(row.get("fingerprint"))
            if mark:
                row["acked"] = mark
        metadata = {
            "available": failure is None,
            "retention": KEEP_ROWS,
            "stored": len(raw),
            "window": window,
            "returned": len(rows),
            "acked_count": sum(1 for row in rows if row.get("acked")),
            "bytes": path.stat().st_size,
            "malformed": malformed,
        }
        if failure:
            metadata.update(failure)
        return rows, metadata
    except OSError as exc:
        return [], {"available": False, "retention": KEEP_ROWS, "stored": None,
                    "reason": _clean(type(exc).__name__, 80)}


def read_acked(hub_dir) -> dict:
    """Signature -> when it was acknowledged and by whom."""
    try:
        value = json.loads(_acked_path(hub_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def ack(hub_dir, fingerprint: str, actor: str = "", note: str = "") -> dict:
    """Acknowledge one error SIGNATURE. Acking is not deleting: the rows stay in the stream
    and stay countable — they just stop competing for attention with failures nobody has
    looked at yet. Returns {} on failure so a caller can report honestly."""
    fingerprint = _clean(fingerprint, 32)
    if not fingerprint:
        return {}
    entry = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "by": _clean(actor, 60), "note": _clean(note, 240)}
    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            current = read_acked(hub_dir)
            current[fingerprint] = entry
            if len(current) > KEEP_ROWS:      # bounded like the stream itself
                for stale in sorted(current, key=lambda k: current[k].get("at", "")
                                    )[:len(current) - KEEP_ROWS]:
                    current.pop(stale, None)
            temp = _acked_path(hub_dir).with_suffix(".tmp")
            temp.write_text(json.dumps(current), encoding="utf-8")
            os.replace(temp, _acked_path(hub_dir))
    except Exception:                                        # noqa: BLE001
        return {}
    return entry


def unack(hub_dir, fingerprint: str) -> bool:
    """Reopen a signature — an ack that cannot be undone is a delete with extra steps.
    False when the signature was not acked (a reopen of nothing must not report success)."""
    fingerprint = _clean(fingerprint, 32)
    try:
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=5):
            current = read_acked(hub_dir)
            if fingerprint not in current:
                return False
            current.pop(fingerprint)
            temp = _acked_path(hub_dir).with_suffix(".tmp")
            temp.write_text(json.dumps(current), encoding="utf-8")
            os.replace(temp, _acked_path(hub_dir))
        return True
    except Exception:                                        # noqa: BLE001
        return False


def clear(hub_dir, before_epoch=None, only_acked=False) -> dict:
    """Remove rows outright — the operation a person wants after a fix ships. Bounded by
    design: by AGE or by ACK, never "everything". only_acked is a RESTRICTION: with it set
    an unacknowledged row never drops however old, and an age bound beside it narrows the
    acked set rather than widening the clear."""
    if before_epoch is None and not only_acked:
        return {"removed": 0, "kept": 0, "reason": "refused: pass an age or only_acked"}
    acked = read_acked(hub_dir)
    kept, removed = [], 0
    path = _errors_path(hub_dir)
    try:
        with ProcessFileLock(Path(hub_dir), name=".errors.lock", timeout=10):
            if not path.exists():
                return {"removed": 0, "kept": 0}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    removed += 1          # malformed rows are not evidence of anything
                    continue
                old = before_epoch is None or float(row.get("epoch") or 0) < float(before_epoch)
                if only_acked:
                    drop = bool(acked.get(row.get("fingerprint"))) and old
                else:
                    drop = old
                if drop:
                    removed += 1
                else:
                    kept.append(line)
            temp = path.with_suffix(".clear.tmp")
            temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            os.replace(temp, path)
    except Exception as exc:                                 # noqa: BLE001
        return {"removed": 0, "kept": 0, "reason": type(exc).__name__}
    return {"removed": removed, "kept": len(kept)}


def stamp(hub_dir) -> tuple:
    """Cheap SSE change fingerprint over BOTH sidecar files — acknowledging is a change to
    the queue, and it writes a different file than the stream. A watcher on the stream
    alone shows a claimed row as unclaimed on every other open board until something
    unrelated appends, which is exactly the double-pickup the ack exists to prevent."""
    size = mtime = 0
    for path in (_errors_path(hub_dir), _acked_path(hub_dir)):
        try:
            stat = path.stat()
        except OSError:
            continue
        size += stat.st_size
        mtime = max(mtime, stat.st_mtime_ns)
    return size, mtime


class HubErrorHandler(logging.Handler):
    """Logging handler that records ERROR+ without exposing args or request bodies. Wire it
    on django.request (or any logger) with the hub dir:

        LOGGING["handlers"]["hub_errors"] = {
            "()": "hub_core.errorlog.HubErrorHandler", "hub_dir": str(HUB_DIR),
            "level": "ERROR"}
    """

    def __init__(self, hub_dir, level=logging.ERROR):
        super().__init__(level=level)
        self.hub_dir = str(hub_dir)

    def emit(self, log_record) -> None:  # pragma: no cover - exercised by logging itself
        try:
            if log_record.name == __name__:
                return
            details = ""
            if log_record.exc_info:
                details = "".join(traceback.format_exception(*log_record.exc_info))
            record(
                self.hub_dir, log_record.name, log_record.getMessage(),
                severity="critical" if log_record.levelno >= logging.CRITICAL else "error",
                code=getattr(log_record, "code", None) or log_record.levelname.lower(),
                details=details,
                context={"component": getattr(log_record, "module", "")},
            )
        except Exception:
            self.handleError(log_record)
