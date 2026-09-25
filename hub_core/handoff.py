"""THE PUBLISH HAND-OFF: a worker whose machine cannot push still ships its work.

An unattended run that makes a change, proves it, and then cannot push (a per-machine key that
was never registered with the forge, a credential manager that cannot prompt with nobody at the
keyboard) has answered the question without delivering it. Left alone it writes a patch file on
its own disk -- work nobody else can reach -- and a person redoes it by hand.

So the PUSH moves to a machine that can push, using rights that machine already has. No new
credential, no forge admin token, nothing added to anyone's account:

  1. the author uploads a git bundle of ``origin/<branch>..HEAD``        POST /hub/api/handoff
  2. a publisher takes a short, FENCED lease on the oldest open one     POST /hub/api/handoff/claim
     and downloads the bundle under that lease                         POST /hub/api/handoff/bundle
  3. it rebases onto the current branch head in a scratch clone, pushes (never forced) and
     reports what happened                                             POST /hub/api/handoff/result
  4. the adapter writes a ``pushed`` checkpoint with the sha onto the task -- the typed field a
     verified deploy reads to close it -- and messages the author's machine.

STORAGE. A sidecar under ``<hub dir>/handoffs``, never the ledger: ``events.jsonl`` is
append-only (one line per submit, claim, release and result; the current state is its fold) and
each bundle is a file beside it. A bundle is source code and can be megabytes; the hash-chained
ledger is the wrong place for either.

THE LEASE is the task lease at a smaller scale: a random token (only its hash is stored) plus a
FENCE that increases with every claim, and a short TTL. Only the NEWEST claim may download or
report, so a publisher whose lease lapsed and was re-granted can never overwrite the newer
holder's outcome. A hand-off that lapses ``MAX_CLAIMS`` times without a result is declared
failed rather than offered forever; a publisher that RELEASES ("not me": it found it cannot push
either) hands its lapse back.

WHERE A HAND-OFF MAY POINT is an adopter setting (the adapter passes ``hosts``). A publisher
pushes with ITS OWN rights, so a hand-off that could name any host would turn every publisher
into a push relay for whoever uploads. No configured host means every hand-off is refused: the
lane fails closed. The pseudo-host ``file`` admits ``file://`` remotes and must be listed
explicitly (a single-machine install, or a local proof).

NEVER A FALSE GREEN. ``published`` needs the full pushed sha; when the commit resolver can show
the sha is NOT on a server the report is refused, and when it cannot ask the record says
``verified: null`` rather than claiming a verification that did not happen.

Standard-library only. The adapter supplies the hub directory, the allowed hosts, a task-exists
check and the commit resolver.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from . import atomic as _atomic
from .process_lock import ProcessFileLock

#: The decoded bundle ceiling. A responder's change is a handful of commits; 20 MB is room for a
#: vendored asset without turning the hub into a file store.
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
#: The request ceiling: the base64 of the bundle plus a little JSON around it.
MAX_BODY_BYTES = (MAX_BUNDLE_BYTES * 4) // 3 + 64 * 1024
LEASE_TTL_S = 900
LEASE_TTL_MIN_S = 60
LEASE_TTL_MAX_S = 1800
#: Claims that lapse without a result before the hand-off is declared failed. A lapse is a
#: publisher that died or ran out of clock; three of them is not bad luck any more.
MAX_CLAIMS = 3

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9][A-Za-z0-9._\-]*)*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TASK_RE = re.compile(r"^[a-z0-9][a-z0-9-]*:task:[a-z0-9][a-z0-9._-]*$")
HANDOFF_RE = re.compile(r"^h-[0-9a-f]{12}$")
OPEN = ("pending",)
TERMINAL = ("published", "failed")
OUTCOMES = ("published", "failed", "released")


class Refusal(Exception):
    """A refused write: ``code``, a human message, and the HTTP status to answer with."""

    def __init__(self, code: str, msg: str, status: int = 422):
        super().__init__(msg)
        self.code, self.msg, self.status = code, msg, status

    def body(self) -> dict:
        return {"errors": [{"code": self.code, "msg": self.msg}]}


# ---------------------------------------------------------------------------- storage

def _dir(hub_dir) -> Path:
    d = Path(hub_dir) / "handoffs"
    (d / "bundles").mkdir(parents=True, exist_ok=True)
    return d


def _lock(hub_dir):
    return ProcessFileLock(_dir(hub_dir), name=".handoffs.lock", timeout=15)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _events(hub_dir) -> list:
    try:
        text = (_dir(hub_dir) / "events.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue                      # a torn tail line is skipped, never fatal
        if isinstance(ev, dict) and ev.get("id"):
            out.append(ev)
    return out


def _emit(hub_dir, ev: dict) -> None:
    # append_line writes exactly what it is given -- the newline is ours to add.
    _atomic.append_line(_dir(hub_dir) / "events.jsonl", json.dumps(ev, sort_keys=True) + "\n")


def _bundle_path(hub_dir, hid: str) -> Path:
    if not HANDOFF_RE.match(str(hid or "")):
        raise Refusal("bad_id", "id is h-<12 hex>")
    return _dir(hub_dir) / "bundles" / ("%s.bundle" % hid)


def fold(events, now: float | None = None) -> dict:
    """{id: record} from the event log. Pure over the events it is given."""
    now = time.time() if now is None else now
    recs: dict = {}
    for ev in events:
        kind, hid = ev.get("ev"), ev["id"]
        if kind == "submitted":
            rec = {k: v for k, v in ev.items() if k != "ev"}
            rec.update(status="pending", claims=0, fence=0, claim=None, result=None)
            recs[hid] = rec
            continue
        rec = recs.get(hid)
        if rec is None:
            continue
        if kind == "claimed":
            rec["claims"] = int(rec.get("claims") or 0) + 1
            rec["fence"] = int(ev.get("fence") or 0)
            rec["claim"] = {k: ev.get(k) for k in ("token_sha", "fence", "agent", "machine",
                                                   "at", "expires")}
        elif kind == "released":
            if rec.get("claim") and rec["claim"].get("fence") == ev.get("fence"):
                rec["claim"] = None
                rec["released"] = int(rec.get("released") or 0) + 1
                # A release is a publisher saying "not me", not a failed attempt: it hands the
                # lapse budget back.
                rec["claims"] = max(0, int(rec.get("claims") or 0) - 1)
                rec["last_release"] = {"by": ev.get("agent"), "machine": ev.get("machine"),
                                       "reason": ev.get("reason"), "at": ev.get("at")}
        elif kind in TERMINAL:
            rec["status"] = kind
            rec["result"] = {k: ev.get(k) for k in ("agent", "machine", "fence", "pushed_sha",
                                                   "reason", "conflicts", "note", "at",
                                                   "verified")}
            rec["claim"] = None
    for rec in recs.values():
        claim = rec.get("claim")
        rec["claim_live"] = bool(claim and float(claim.get("expires") or 0) > now)
    return recs


def records(hub_dir, now: float | None = None) -> dict:
    return fold(_events(hub_dir), now)


def public(rec: dict, now: float | None = None) -> dict:
    """A record as a reader sees it: no token hash, no idempotency key, ages computed."""
    now = time.time() if now is None else now
    out = {k: v for k, v in rec.items() if k not in ("claim", "idem")}
    claim = rec.get("claim") or None
    if claim:
        out["claim"] = {"agent": claim.get("agent"), "machine": claim.get("machine"),
                        "fence": claim.get("fence"), "at": claim.get("at"),
                        "expires_in_s": max(0, int(float(claim.get("expires") or 0) - now))}
    out["age_s"] = max(0, int(now - float(rec.get("ts") or now)))
    return out


def listing(hub_dir, want: str = "open", task: str = "", now: float | None = None) -> dict:
    """The queue as ``/hub/handoffs.json`` serves it: open oldest first, or ?status=all|<s>."""
    now = time.time() if now is None else now
    recs = sorted(records(hub_dir, now).values(), key=lambda r: float(r.get("ts") or 0))
    want = (want or "open").strip().lower()
    if want == "open":
        rows = [r for r in recs if r["status"] in OPEN]
    elif want == "all":
        rows = recs[-200:]
    else:
        rows = [r for r in recs if r["status"] == want][-200:]
    if task:
        rows = [r for r in rows if r.get("task") == task]
    return {"data": [public(r, now) for r in rows], "count": len(rows),
            "metadata": {"generated": int(now),
                         "open": sum(1 for r in recs if r["status"] in OPEN)}}


# ---------------------------------------------------------------------------- validation

def parse_bundle_header(data: bytes):
    """(prerequisites, refs) from a git bundle's text header, or None when it is not one.

    v2/v3 header: a signature line, v3 capability lines (``@...``), then ``-<sha> [comment]``
    for each prerequisite and ``<sha> <refname>`` for each ref, ended by an empty line."""
    head = data[:65536]
    end = head.find(b"\n\n")
    if end < 0:
        return None
    lines = head[:end].decode("utf-8", "replace").split("\n")
    if lines[0] not in ("# v2 git bundle", "# v3 git bundle"):
        return None
    prereqs, refs = [], []
    for line in lines[1:]:
        if line.startswith("@"):
            continue
        if line.startswith("-"):
            prereqs.append(line[1:].split(" ", 1)[0].lower())
        elif line:
            sha, _, ref = line.partition(" ")
            refs.append((sha.lower(), ref.strip()))
    return prereqs, refs


def strip_credentials(url: str) -> str:
    """A remote URL with any user:password@ removed. A credential never rides a record."""
    return re.sub(r"^([a-z][a-z0-9+.\-]*://)[^/@]*@", r"\1", str(url or "").strip(), flags=re.I)


def remote_project(url: str) -> tuple:
    """(host, project path) of a git remote URL; ('', '') when it is not one we can read.
    ``file://`` remotes answer the pseudo-host ``file`` and their full path."""
    url = str(url or "").strip()
    m = re.match(r"^file://(?:localhost)?(/.+?)(?:\.git)?/?$", url, re.I)
    if m:
        return "file", m.group(1).strip("/")
    m = re.match(r"^[a-z][a-z0-9+.\-]*://(?:[^/@]*@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$",
                 url, re.I)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+?)(?:\.git)?/?$", url)
    if not m:
        return "", ""
    return m.group(1).lower(), m.group(2).strip("/")


def _project_matches(host: str, path: str, project: str) -> bool:
    path, project = path.lower(), project.lower()
    if host == "file":
        # A filesystem path carries its parent directories; the project is its tail.
        return path == project or path.endswith("/" + project)
    return path == project


def validate(b: dict, agent: str, *, hosts, task_exists, resolver=None):
    """(fields, bundle bytes) for a submission, or raise Refusal.

    ``hosts`` is the adopter's allowlist (empty refuses everything). ``task_exists(task_id)``
    says whether the task is on the board. ``resolver(project, sha)`` answers True (a server
    has it) / False (it provably does not) / None (could not ask); a False base is refused."""
    hosts = tuple(str(h).strip().lower() for h in (hosts or ()) if str(h).strip())
    if not hosts:
        raise Refusal("handoff_disabled", "this hub accepts no hand-offs: the operator has not "
                      "listed any git host a publisher may push to (HUB_HANDOFF_GIT_HOSTS)", 403)
    task = str(b.get("task") or "").strip()
    if not TASK_RE.match(task):
        raise Refusal("bad_task", "task is a full board task id, e.g. proj:task:0042")
    if not task_exists(task):
        raise Refusal("task_not_found", "%s is not a task on the board" % task, 404)
    project = str(b.get("project") or "").strip().strip("/")
    if not PROJECT_RE.match(project):
        raise Refusal("bad_project", "project is the repository path, e.g. team/budget-app")
    remote = strip_credentials(b.get("remote"))
    host, path = remote_project(remote)
    if host not in hosts:
        raise Refusal("host_not_allowed",
                      "a hand-off may only name a repository on %s; this repository's origin is "
                      "on %r. A publisher pushes with its own rights, so it never pushes "
                      "anywhere else." % (", ".join(hosts), host or remote[:120]), 403)
    if not _project_matches(host, path, project):
        raise Refusal("project_mismatch", "origin names %s but the hand-off says %s"
                      % (path, project))
    branch = str(b.get("branch") or "main").strip()
    if not BRANCH_RE.match(branch) or ".." in branch:
        raise Refusal("bad_branch", "branch is the branch to publish onto, e.g. main")
    base = str(b.get("base") or "").strip().lower()
    head = str(b.get("head") or "").strip().lower()
    if not SHA_RE.match(base) or not SHA_RE.match(head):
        raise Refusal("bad_sha", "base and head are full 40-character commit ids")
    if base == head:
        raise Refusal("nothing_to_publish", "head equals base: there are no commits to publish")
    raw = b.get("bundle_b64")
    if not isinstance(raw, str) or not raw:
        raise Refusal("need_bundle", "bundle_b64 is the base64 of `git bundle create`")
    if len(raw) > MAX_BODY_BYTES:
        raise Refusal("bundle_too_large", "the bundle is over %d MB" % (MAX_BUNDLE_BYTES >> 20),
                      413)
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise Refusal("bad_bundle", "bundle_b64 is not valid base64")
    if len(data) > MAX_BUNDLE_BYTES:
        raise Refusal("bundle_too_large", "the bundle is %d bytes; the ceiling is %d"
                      % (len(data), MAX_BUNDLE_BYTES), 413)
    parsed = parse_bundle_header(data)
    if parsed is None:
        raise Refusal("bad_bundle", "that is not a git bundle (no v2/v3 bundle header)")
    prereqs, refs = parsed
    if head not in {sha for sha, _ref in refs}:
        raise Refusal("bundle_head_mismatch", "the bundle does not carry head %s" % head[:12])
    if base not in prereqs:
        # A bundle without base as its prerequisite is either the whole history (too big and
        # unnecessary) or built from some other range than the one the record names.
        raise Refusal("bundle_base_mismatch", "the bundle's prerequisites do not include base "
                      "%s -- build it as `git bundle create <file> %s..HEAD`"
                      % (base[:12], base[:12]))
    known = resolver(project, base) if resolver else None
    if known is False:
        raise Refusal("base_not_on_server",
                      "%s is not on a server for %s: either the project path is wrong or the "
                      "base was never pushed. The base must be a commit of origin/%s."
                      % (base[:12], project, branch))
    commits = str(b.get("commits") or "0")
    fields = {"task": task, "project": project, "remote": remote, "remote_host": host,
              "branch": branch, "base": base, "head": head, "bytes": len(data),
              "base_verified": known if known in (True, False) else None,
              "commits": int(commits) if commits.isdigit() else 0,
              "subject": str(b.get("subject") or "")[:200],
              "machine": str(b.get("machine") or "")[:120].lower(),
              "session": str(b.get("session") or "")[:64], "agent": agent,
              "note": str(b.get("note") or "")[:600],
              "idem": str(b.get("idem_key") or "")[:200]}
    return fields, data


# ---------------------------------------------------------------------------- the lane

def submit(hub_dir, fields: dict, data: bytes, now: float | None = None) -> dict:
    """Record a hand-off (idempotently) and store its bundle."""
    now = time.time() if now is None else now
    with _lock(hub_dir):
        recs = records(hub_dir, now)
        idem = fields.get("idem") or ""
        for rec in recs.values():
            # A retry whose first attempt landed: answer with the record it made.
            if idem and rec.get("idem") == idem:
                return {"data": dict(public(rec, now), replayed=True)}
        for rec in recs.values():
            # The same commits already waiting: one publish is enough.
            if (rec.get("status") in OPEN and rec.get("project") == fields["project"]
                    and rec.get("head") == fields["head"]):
                return {"data": dict(public(rec, now), duplicate=True)}
        seed = "%s|%s|%s|%s" % (idem or secrets.token_hex(8), fields["project"],
                                fields["head"], now)
        hid = "h-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        # The bundle is on disk BEFORE the event that names it, so no reader ever sees a record
        # whose bundle is missing.
        _atomic.write_bytes(_bundle_path(hub_dir, hid), data)
        _emit(hub_dir, dict(fields, ev="submitted", id=hid, at=_iso(now), ts=now,
                            sha256=hashlib.sha256(data).hexdigest()))
        rec = records(hub_dir, now)[hid]
    return {"data": public(rec, now)}


def claim_next(hub_dir, agent: str, machine: str, ttl_s=LEASE_TTL_S, want: str = "",
               now: float | None = None) -> dict:
    """Lease the oldest open hand-off to this publisher. ``{"data": None}`` when there is none."""
    now = time.time() if now is None else now
    try:
        ttl_s = int(ttl_s or LEASE_TTL_S)
    except (TypeError, ValueError):
        ttl_s = LEASE_TTL_S
    ttl_s = max(LEASE_TTL_MIN_S, min(ttl_s, LEASE_TTL_MAX_S))
    with _lock(hub_dir):
        recs = records(hub_dir, now)
        # Lapsed past the budget: close it as failed rather than offering it forever.
        for rec in recs.values():
            if (rec["status"] in OPEN and not rec["claim_live"]
                    and int(rec.get("claims") or 0) >= MAX_CLAIMS):
                _emit(hub_dir, {"ev": "failed", "id": rec["id"], "agent": "hub", "machine": "",
                                "fence": rec.get("fence"), "at": _iso(now), "ts": now,
                                "reason": "%d publishers claimed it and none reported a result"
                                          % MAX_CLAIMS})
                rec["status"] = "failed"
        candidates = sorted((r for r in recs.values()
                             if r["status"] in OPEN and not r["claim_live"]
                             and (not want or r["id"] == want)),
                            key=lambda r: float(r.get("ts") or 0))
        if not candidates:
            return {"data": None}
        rec = candidates[0]
        token = secrets.token_hex(16)
        fence = int(rec.get("fence") or 0) + 1
        _emit(hub_dir, {"ev": "claimed", "id": rec["id"], "agent": agent, "machine": machine,
                        "fence": fence, "token_sha": hashlib.sha256(token.encode()).hexdigest(),
                        "at": _iso(now), "ts": now, "expires": now + ttl_s})
        rec = records(hub_dir, now)[rec["id"]]
    out = public(rec, now)
    out.update(token=token, fence=fence, ttl_s=ttl_s)
    return {"data": out}


def _fence(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _holder(rec: dict, token, fence) -> str:
    """'' when (token, fence) is the NEWEST claim, else why not. A lapsed lease nobody re-claimed
    still counts: the push may well have landed, and its report is the truth."""
    claim = rec.get("claim")
    if not claim:
        return "no claim is open on %s" % rec["id"]
    if _fence(fence) is None or _fence(fence) != _fence(claim.get("fence")):
        return "fence %s is stale; %s holds fence %s" % (
            fence, claim.get("machine") or "another publisher", claim.get("fence"))
    got = hashlib.sha256(str(token or "").encode()).hexdigest()
    if not hmac.compare_digest(got, str(claim.get("token_sha") or "")):
        return "the token does not match the claim"
    return ""


def bundle_bytes(hub_dir, hid: str, token, fence, now: float | None = None) -> bytes:
    """The bundle, for the holder of the newest claim only; raises Refusal otherwise."""
    path = _bundle_path(hub_dir, hid)
    rec = records(hub_dir, now).get(hid)
    if not rec:
        raise Refusal("not_found", "no such hand-off", 404)
    why = _holder(rec, token, fence)
    if why:
        raise Refusal("not_holder", why, 409)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Refusal("bundle_missing", "the bundle file is missing (%s)" % type(exc).__name__,
                      410)


def record_result(hub_dir, hid: str, agent: str, machine: str, token, fence, outcome: str,
                  pushed_sha: str = "", reason: str = "", conflicts=None, note: str = "",
                  *, resolver=None, now: float | None = None) -> dict:
    """published | failed | released, accepted from the newest claim only. Returns
    ``{"data": record, "terminal": bool}``; raises Refusal."""
    now = time.time() if now is None else now
    if not HANDOFF_RE.match(str(hid or "")):
        raise Refusal("bad_id", "id is h-<12 hex>")
    fence = _fence(fence)
    outcome = str(outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise Refusal("bad_outcome", "outcome is published, failed or released")
    pushed_sha = str(pushed_sha or "").strip().lower()
    if outcome == "published" and not SHA_RE.match(pushed_sha):
        raise Refusal("need_pushed_sha", "published needs pushed_sha, the full sha now on the "
                                         "branch")
    if outcome in ("failed", "released") and not str(reason or "").strip():
        raise Refusal("need_reason", "say why")
    with _lock(hub_dir):
        rec = records(hub_dir, now).get(hid)
        if not rec:
            raise Refusal("not_found", "no such hand-off", 404)
        if rec["status"] in TERMINAL:
            res = rec.get("result") or {}
            if res.get("fence") == fence and rec["status"] == outcome:
                return {"data": dict(public(rec, now), replayed=True), "terminal": False}
            raise Refusal("already_" + rec["status"], "%s is already %s" % (hid, rec["status"]),
                          409)
        why = _holder(rec, token, fence)
        if why:
            raise Refusal("not_holder", why, 409)
        verified = None
        if outcome == "published" and resolver is not None:
            verified = resolver(rec["project"], pushed_sha)
            if verified is False:
                raise Refusal("pushed_sha_not_on_server",
                              "%s is not on a server for %s -- the push did not land"
                              % (pushed_sha[:12], rec["project"]))
            verified = verified if verified in (True, False) else None
        ev = {"ev": outcome, "id": hid, "agent": agent, "machine": machine, "fence": fence,
              "at": _iso(now), "ts": now, "reason": str(reason or "")[:1000],
              "note": str(note or "")[:600]}
        if outcome == "published":
            ev.update(pushed_sha=pushed_sha, verified=verified)
        if conflicts:
            ev["conflicts"] = [str(c)[:300] for c in list(conflicts)[:50]]
        _emit(hub_dir, ev)
        rec = records(hub_dir, now)[hid]
    return {"data": public(rec, now), "terminal": outcome in TERMINAL, "record": rec}


# ---------------------------------------------------------------------------- board side-effects

def checkpoint_step(rec: dict, agent: str, machine: str, now: float | None = None) -> dict:
    """The plan row a terminal hand-off puts on its task. A publish is a ``pushed`` checkpoint
    with the sha as a FIELD, which is what closes the task once a verified deploy carries it."""
    now = time.time() if now is None else now
    res = rec.get("result") or {}
    by = agent + (" on " + machine if machine else "")
    if rec["status"] == "published":
        sha = res.get("pushed_sha") or ""
        note = ("Published hand-off %s: %s is on %s %s, pushed by %s for %s (%s). %s"
                % (rec["id"], sha[:12], rec["project"], rec.get("branch") or "main", by,
                   rec.get("agent") or "?", rec.get("machine") or "?",
                   res.get("note") or "")).strip()
        return {"step": ("Published via hand-off %s: %s" % (rec["id"], sha[:12]))[:80],
                "done": True, "kind": "pushed", "sha": sha, "note": note[:600],
                "note_at": _iso(now)}
    conflicts = res.get("conflicts") or []
    note = ("Hand-off %s could NOT be published by %s: %s%s. The commits are still in the "
            "author's checkout (%s on %s)."
            % (rec["id"], by, res.get("reason") or "",
               (" -- conflicting paths: " + ", ".join(conflicts[:10])) if conflicts else "",
               str(rec.get("head") or "")[:12], rec.get("machine") or "?"))
    return {"step": ("Hand-off %s failed to publish" % rec["id"])[:80], "done": True,
            "kind": "checkpoint", "note": note[:600], "note_at": _iso(now)}


def author_message(rec: dict, agent: str, machine: str, link: str = "") -> dict:
    """{title, body} telling the author what became of the hand-off."""
    res = rec.get("result") or {}
    by = agent + (" on " + machine if machine else "")
    branch = rec.get("branch") or "main"
    if rec["status"] == "published":
        title = "%s published: %s on %s %s" % (rec["task"], (res.get("pushed_sha") or "")[:12],
                                                rec["project"].rsplit("/", 1)[-1], branch)
        body = ("Your hand-off %s for %s is on %s: %s pushed %s to %s (rebased onto the current "
                "%s; the original head was %s). The task carries a `pushed` checkpoint with the "
                "sha, so a verified deploy of that commit closes it."
                % (rec["id"], rec["task"], branch, by, res.get("pushed_sha") or "",
                   rec["project"], branch, str(rec.get("head") or "")[:12]))
    else:
        conflicts = res.get("conflicts") or []
        title = "%s hand-off %s could not be published" % (rec["task"], rec["id"])
        body = ("%s could not publish your hand-off %s for %s: %s.%s Nothing was forced and "
                "nothing was pushed. Rebase onto origin/%s, resolve it, and hand it off again "
                "(python -m hub_core.client handoff %s)."
                % (by, rec["id"], rec["task"], res.get("reason") or "",
                   (" Conflicting paths: " + ", ".join(conflicts[:10]) + ".") if conflicts else "",
                   branch, rec["task"]))
    if link:
        body += "\n\n" + link
    return {"title": title[:200], "body": body}
