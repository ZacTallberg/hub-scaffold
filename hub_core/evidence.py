"""The shared evidence store: probes, traces, measurements and timings, keyed by subject and commit.

Several consoles on one problem each re-measure the same slowness from scratch, because a probe
one console ran is visible only in that console's transcript. A finding or a lesson is a
CONCLUSION; this is the raw reading behind one -- the timing, the trace excerpt, the row count
at a named commit -- so the next console on the same subject reads it instead of re-running it,
and can see which commit it applies to.

Stored in HUB_DIR/evidence.jsonl, never the ledger: a reading is not a board fact, and the store
is CAPPED (oldest rows fall off first), which the append-only ledger must never be. Nothing ever
rewrites a row. Stdlib only; the adapter serves it:

    POST /hub/api/evidence  {subject, kind, summary, body?, repo?, commit?}
    GET  /hub/evidence.json?q=&repo=&commit=&limit=
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from . import atomic, bm25
from .process_lock import ProcessFileLock

#: The body carries a probe's printed output, not a log file.
MAX_BODY_CHARS = 16 * 1024
MAX_REQUEST_BYTES = 24 * 1024
#: The store keeps this many rows; older ones are dropped oldest first.
KEEP = 4000
KINDS = ("probe", "trace", "measurement", "timing")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_REPO = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,119}$")
_MEMO = {"key": None, "rows": None, "index": None}
_MEMO_LOCK = threading.Lock()


class Refused(ValueError):
    def __init__(self, code: str, msg: str = "", status: int = 422, **extra):
        super().__init__(msg or code)
        self.code, self.msg, self.status, self.extra = code, msg, status, extra

    def body(self) -> dict:
        return {"errors": [dict({"code": self.code, "msg": self.msg}, **self.extra)]}


def _path(hub_dir) -> Path:
    return Path(hub_dir) / "evidence.jsonl"


def _read(hub_dir) -> list:
    p = _path(hub_dir)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def stamp(hub_dir) -> str:
    try:
        st = _path(hub_dir).stat()
        return "%d-%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        return "empty"


def clean_repo(value) -> str:
    """`group/name`, lower-cased, with any URL scheme, credentials, host or `.git` removed, so
    the same project matches however a console spelled it (remote URL, path, bare name) and a
    credential pasted inside a remote URL is never stored."""
    v = str(value or "").strip().lower()
    if "://" in v or "@" in v:
        v = re.sub(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?[^/:]+[:/]", "", v)
    if v.endswith(".git"):
        v = v[:-4]
    return v.strip("/")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def put(hub_dir, fields: dict, *, by: str = "", machine: str = "", session: str = "") -> dict:
    """Validate and append one reading; returns the stored row or raises Refused."""
    subject = " ".join(str(fields.get("subject") or "").split())[:140]
    if len(subject) < 3:
        raise Refused("need_subject", "subject: a short name for what this is evidence about "
                                      "(e.g. 'export latency')")
    kind = str(fields.get("kind") or "probe").strip().lower()
    if kind not in KINDS:
        raise Refused("bad_kind", "kind is one of " + ", ".join(KINDS), allowed=list(KINDS))
    summary = " ".join(str(fields.get("summary") or "").split())[:400]
    if not summary:
        raise Refused("need_summary", "summary: one line saying what was measured and what it showed")
    body = str(fields.get("body") or "")
    if len(body) > MAX_BODY_CHARS:
        raise Refused("body_too_large", "the body is the probe's printed output; cut it to the "
                                        "lines that show the result", status=413,
                      max_body_chars=MAX_BODY_CHARS)
    repo = clean_repo(fields.get("repo"))
    if repo and not _REPO.match(repo):
        raise Refused("bad_repo", "repo: a project name like group/name")
    commit = str(fields.get("commit") or "").strip().lower()
    if commit and not _COMMIT.match(commit):
        raise Refused("bad_commit", "commit: 7-40 hex")
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row = {"id": "ev-%s-%s" % (time.strftime("%Y%m%d%H%M%S", time.gmtime()),
                               _digest(subject + summary + body + at)),
           "repo": repo, "commit": commit, "subject": subject, "kind": kind,
           "summary": summary, "body": body, "by": str(by or "")[:64],
           "machine": str(machine or "")[:64], "session": str(session or "")[:64], "at": at}
    Path(hub_dir).mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(Path(hub_dir), name=".evidence.lock", timeout=5):
        rows = _read(hub_dir) + [row]
        if len(rows) > KEEP:
            rows = rows[len(rows) - KEEP:]
        atomic.write_text(_path(hub_dir),
                          "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return row


def _index(hub_dir):
    key = (str(hub_dir), stamp(hub_dir))
    with _MEMO_LOCK:
        if _MEMO["key"] == key and _MEMO["index"] is not None:
            return _MEMO["rows"], _MEMO["index"]
    rows = _read(hub_dir)
    docs = {i: {"title": r.get("subject") or "", "body": r.get("summary") or "",
                "tags": " ".join(x for x in (r.get("kind"), r.get("repo")) if x)}
            for i, r in enumerate(rows)}
    index = bm25.Bm25F(docs)
    with _MEMO_LOCK:
        _MEMO.update(key=key, rows=rows, index=index)
    return rows, index


def find(hub_dir, q: str = "", repo: str = "", commit: str = "", limit: int = 20) -> tuple:
    """(rows, stored): matching rows, best first when `q` is given, else newest first. `commit`
    matches as a prefix either way, so a 7-char sha finds a row stored with 40 and the reverse."""
    rows, index = _index(hub_dir)
    repo = clean_repo(repo)
    commit = str(commit or "").strip().lower()

    def keep(r):
        if repo and r.get("repo") != repo:
            return False
        rc = str(r.get("commit") or "")
        return not commit or bool(rc and (rc.startswith(commit) or commit.startswith(rc)))

    if (q or "").strip():
        ranked = [(s, rows[i]) for s, i in index.score(q) if keep(rows[i])]
        return [dict(r, score=round(s, 3)) for s, r in ranked[:limit]], len(rows)
    return [r for r in reversed(rows) if keep(r)][:limit], len(rows)


def etag(hub_dir, q, repo, commit, limit) -> str:
    return '"ev-%s-%s"' % (stamp(hub_dir), _digest("%s|%s|%s|%d" % (q, repo, commit, limit)))
