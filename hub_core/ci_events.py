"""CI results reach the board: a failure becomes a row on the operational stream, and a later
green on the same job and ref retires it.

Framework-free; every function takes the hub dir explicitly. The adapter authenticates the
sender and hands this module a parsed body. Two sender shapes are understood:

* a GitLab pipeline or job webhook (``object_kind`` in the body), and
* a GENERIC event any CI system can post from one script step (documented in HUB-API.md):
  ``{"kind": "pipeline"|"job"|"deploy", "project", "ref", "sha", "status", "pipeline", "job",
  "jobs": [{"name", "status"}], "trigger", "url", "finished_at", "allow_failure",
  "restored_sha"}``.

Both are normalized to one event shape first, so every rule below holds for every sender.

Rules this module holds, each paid for on the origin system:

* A DEPLOY THAT ONLY RECORDS SUCCESS REPORTS SUCCESSES ONLY. A release record written by the
  job that runs after verification passes can never say that a deploy failed, so a board fed
  only by release records reads "every deploy ok" while hosts roll back. CI failures need a
  path of their own, and this is it.
* THE SENDER IS NOT AN AGENT. A CI system cannot present an agent credential; it presents a
  shared webhook secret. Without a configured secret the ingest refuses everything, because an
  open ingest anyone can post rows to is worse than no ingest.
* DEPLOY AND VERIFY ARE NOT ORDINARY JOBS. A red test is a developer's problem; a red deploy
  or verify means a host may be running something nobody chose, so it is critical.
* A PIPELINE THAT FAILED WITH NO JOBS IS A CONFIGURATION REJECTION, AND THE WORST ONE. Nothing
  ran -- no deploy, no verify, no test -- and every later push does the same until a person
  edits the file. It is recorded critical with a sentence that says exactly that, instead of
  the least informative row there is ("pipeline () failing").
* A LATER GREEN CLOSES AN EARLIER RED, on POSITIVE evidence only: the same job on the same ref
  passed, in an event that happened after the failure (webhook delivery is not ordered). A
  pipeline success closes the roll-up row and the rows of the jobs IT RAN -- never a job it did
  not run, or a scheduled job that is still failing reads as fixed. Retiring is acking, never
  deleting, and the note says it was the ingest, not a person.
* A RECURRENCE REOPENS. A failure whose signature was acked is un-acked when it happens again,
  so a job that broke, went green and broke again is on the queue, not hidden under an old note.
* THE TRIGGER IS A HINT, NEVER A VERDICT. An api-, schedule- or web-triggered run has no
  deploy stage, so a verify job there may be reporting on the previously deployed commit. The
  row says so, so nobody goes looking at a healthy host.
* DUPLICATE DELIVERIES ARE ONE EVENT. A project hook and a group hook can both fire for one
  pipeline; keyed on the event's own finish time, so a retried pipeline is still a new event.
* THE RAW DELIVERY IS KEPT, bounded: two rotated files, and the rotation happens BEFORE the
  write, because a prune that only runs after a successful write never runs once the disk is
  full. Keeping it is last and fail-soft, but never silent: a store that cannot be written
  records a warning row instead of reading back "count: 0" forever.
* AN INGEST THAT ERRORS TEACHES THE SENDER TO DISABLE THE HOOK. A body that cannot be parsed
  is answered 200 and recorded as the hub's own warning.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import errorlog
from .process_lock import ProcessFileLock

EVENT_LOG_MAX_BYTES = 4_000_000
DELIVERED_KEEP_S = 86400
DELIVERED_MAX = 4000
CRITICAL_WORDS = ("deploy", "verify", "release", "rollback", "bootstrap")
DEPLOYLESS_TRIGGERS = {"api", "schedule", "web", "trigger", "pipeline", "chat", "external",
                       "workflow_dispatch", "manual"}
SUCCESS = {"success", "passed", "succeeded", "ok"}
FAILED = {"failed", "failure", "error", "errored"}


def slug(value, limit=60) -> str:
    return re.sub(r"[^a-z0-9._-]", "", str(value or "").strip().lower())[:limit]


def _status(value) -> str:
    value = str(value or "").strip().lower()
    if value in SUCCESS:
        return "success"
    if value in FAILED:
        return "failed"
    return value


def _epoch(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    from datetime import datetime
    text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S%z"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return parsed.timestamp()
        except ValueError:
            continue
    return 0.0


# ── Normalization: every sender becomes one event shape ──

def from_gitlab(body: dict) -> dict:
    kind = str(body.get("object_kind") or "").lower()
    project = body.get("project") or {}
    full = str(project.get("path_with_namespace") or project.get("name")
               or (body.get("repository") or {}).get("name") or "unknown")
    # The LEAF before slugging: slugging first glues the group onto every project name.
    name = slug(full.rstrip("/").rsplit("/", 1)[-1]) or "unknown"
    web = str(project.get("web_url") or "").rstrip("/")
    actor = slug((body.get("user") or {}).get("username") or "", 40)
    if kind == "pipeline":
        attrs = body.get("object_attributes") or {}
        pid = attrs.get("id")
        return {
            "kind": "pipeline", "project": name, "pipeline": str(pid or ""),
            "ref": slug(attrs.get("ref") or "", 40), "sha": str(attrs.get("sha") or "")[:40],
            "status": _status(attrs.get("status")), "trigger": slug(attrs.get("source") or "", 24),
            "finished_at": attrs.get("finished_at"),
            "jobs": [{"name": slug(b.get("name")), "status": _status(b.get("status")),
                      "allow_failure": bool(b.get("allow_failure"))}
                     for b in (body.get("builds") or []) if slug(b.get("name"))],
            "url": "%s/-/pipelines/%s" % (web, pid) if web and pid else "",
            "actor": actor,
        }
    if kind in ("build", "job"):
        pipeline = body.get("pipeline") if isinstance(body.get("pipeline"), dict) else {}
        allow = body.get("build_allow_failure")
        return {
            "kind": "job", "project": name, "pipeline": str(body.get("pipeline_id") or ""),
            "job": slug(body.get("build_name")), "job_id": str(body.get("build_id") or ""),
            "ref": slug(body.get("ref") or "", 40), "sha": str(body.get("sha") or "")[:40],
            "status": _status(body.get("build_status")),
            "trigger": slug(pipeline.get("source") or "", 24),
            "finished_at": body.get("build_finished_at"),
            "allow_failure": allow is True or str(allow) == "True",
            "failure_reason": slug(body.get("build_failure_reason") or "", 40),
            "url": "%s/-/jobs/%s" % (web, body.get("build_id")) if web and body.get("build_id") else "",
            "actor": actor,
        }
    return {"kind": kind or "none", "project": name}


def from_generic(body: dict) -> dict:
    kind = str(body.get("kind") or "").strip().lower()
    return {
        "kind": kind or "none",
        "project": slug(body.get("project")) or "unknown",
        "pipeline": str(body.get("pipeline") or "")[:40],
        "job": slug(body.get("job")),
        "ref": slug(body.get("ref") or "", 40),
        "sha": str(body.get("sha") or "")[:40],
        "status": _status(body.get("status")),
        "trigger": slug(body.get("trigger") or "", 24),
        "finished_at": body.get("finished_at"),
        "allow_failure": bool(body.get("allow_failure")),
        "failure_reason": slug(body.get("failure_reason") or "", 40),
        "restored_sha": slug(body.get("restored_sha") or "", 40),
        "jobs": [{"name": slug(j.get("name")), "status": _status(j.get("status")),
                  "allow_failure": bool(j.get("allow_failure"))}
                 for j in (body.get("jobs") or []) if isinstance(j, dict) and slug(j.get("name"))],
        "url": str(body.get("url") or "")[:300],
        "actor": slug(body.get("actor") or "", 40),
    }


def normalize(body: dict) -> dict:
    """The provider is read from the body itself: GitLab always sends object_kind."""
    return from_gitlab(body) if body.get("object_kind") else from_generic(body)


# ── Bookkeeping sidecars (observed state beside the error stream, never the ledger) ──

def _duplicate(hub_dir, key: str) -> bool:
    """True when this exact event was delivered within a day. An event without a finish time
    is never deduped. Fail-open: a sidecar that cannot be read dedupes nothing."""
    if not key:
        return False
    path = Path(hub_dir) / "ci-delivered.json"
    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".ci-delivered.lock", timeout=3):
            try:
                seen = json.loads(path.read_text(encoding="utf-8"))
                seen = seen if isinstance(seen, dict) else {}
            except (OSError, ValueError):
                seen = {}
            now = time.time()
            seen = {k: v for k, v in seen.items() if now - float(v or 0) < DELIVERED_KEEP_S}
            if key in seen:
                return True
            seen[key] = now
            if len(seen) > DELIVERED_MAX:
                seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-DELIVERED_MAX:])
            tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
            tmp.write_text(json.dumps(seen), encoding="utf-8")
            os.replace(tmp, path)
    except Exception:                                        # noqa: BLE001
        return False
    return False


def _critical(names) -> bool:
    return any(w in (n or "") for n in names for w in CRITICAL_WORDS)


def _trigger_note(trigger: str, deployish: bool) -> str:
    if deployish and trigger in DEPLOYLESS_TRIGGERS:
        return (" [%s-triggered: this run has no deploy stage, so a deploy/verify job here may be "
                "reporting on the previously deployed commit -- read its log before trusting "
                "that]" % trigger)
    return ""


def _open_ci_rows(hub_dir, project: str) -> list:
    """Every retained row of this project's CI family that is still open. An ack older than the
    row's own time does not close it: that is a recurrence after a resolve."""
    path = Path(hub_dir) / "errors.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    acked = errorlog.read_acked(hub_dir)
    newest = {}
    prefix = "ci.%s." % project
    for line in lines[-errorlog.KEEP_ROWS:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("source") or "").startswith(prefix):
            newest[row.get("fingerprint")] = row           # the newest occurrence wins
    out = []
    for fp, row in newest.items():
        mark = acked.get(fp)
        if mark and _epoch(mark.get("at")) >= float(row.get("epoch") or 0):
            continue
        out.append(row)
    return out


def retire(hub_dir, project, ref, ran, sha, event_epoch, *, whole_pipeline=False, ack=None) -> int:
    """A LATER GREEN CLOSES AN EARLIER RED. Returns how many signatures it retired."""
    if not project:
        return 0
    ack = ack or (lambda fp, note: errorlog.ack(hub_dir, fp, actor="ci", note=note))
    prefix = "ci.%s." % project
    wanted = {j for j in ran if j}
    retired = 0
    for row in _open_ci_rows(hub_dir, project):
        ctx = row.get("context") or {}
        if ref and ctx.get("ref") and str(ctx.get("ref")) != ref:
            continue
        if event_epoch and float(row.get("epoch") or 0) > event_epoch:
            continue                                   # a failure AFTER this success still stands
        leaf = str(row.get("source") or "")[len(prefix):]
        if leaf == "rollback":
            continue                                   # a rollback is closed by a person
        if leaf == "pipeline":
            named = {j for j in str(ctx.get("jobs") or "").split(",") if j}
            if not whole_pipeline and not (named & wanted):
                continue
        elif leaf not in wanted:
            continue
        if ack(row.get("fingerprint"),
               "superseded: %s passed on %s (%s). Retired by the CI ingest, not by a person -- "
               "if it is still broken it will come straight back."
               % (leaf, (sha or "a later commit")[:12], ref or "?")):
            retired += 1
    return retired


# ── Ingest ──

def ingest(hub_dir, body: dict, *, record=None, ack=None, unack=None,
           ignore_jobs=()) -> dict:
    """Decide what one delivery means, write it, and return what was done."""
    record = record or (lambda *a, **k: errorlog.record(hub_dir, *a, **k))
    unack = unack or (lambda fp: errorlog.unack(hub_dir, fp))
    ev = normalize(body)
    kind, project, status = ev.get("kind"), ev.get("project"), ev.get("status")
    now = time.time()
    finished = _epoch(ev.get("finished_at"))
    event_epoch = finished or now
    ident = ev.get("job_id") or ev.get("pipeline") or ""
    if status in ("success", "failed") and ev.get("finished_at") and ident:
        if _duplicate(hub_dir, "%s:%s:%s:%s:%s" % (kind, project, ident, status, ev.get("finished_at"))):
            return {"recorded": False, "reason": "duplicate delivery", "event": ev}
    ignored = {slug(j) for j in ignore_jobs or () if slug(j)}

    def write(source, message, *, severity, code, details, context):
        row = record(source, message, severity=severity, code=code, details=details,
                     context=dict(context, component="ci"))
        fp = row.get("fingerprint")
        if fp and fp in errorlog.read_acked(hub_dir):
            unack(fp)                                   # a recurrence REOPENS
            row["reopened"] = True
        return row

    details = " · ".join(x for x in ("sha %s" % ev.get("sha", "")[:12] if ev.get("sha") else "",
                                     ev.get("url") or "",
                                     "trigger %s" % ev["trigger"] if ev.get("trigger") else "") if x)
    base_ctx = {"project": project, "ref": ev.get("ref", ""), "sha": ev.get("sha", "")[:12],
                "url": ev.get("url", ""), "trigger": ev.get("trigger", "")}

    if kind == "pipeline":
        jobs = ev.get("jobs") or []
        if status == "success":
            ran = {j["name"] for j in jobs if j["status"] == "success"}
            n = retire(hub_dir, project, ev.get("ref"), ran, ev.get("sha"), event_epoch,
                       whole_pipeline=True, ack=ack)
            return {"recorded": False, "reason": "status=success", "superseded": n, "ran": sorted(ran)}
        if status != "failed":
            return {"recorded": False, "reason": "status=%s" % (status or "none")}
        passed = {j["name"] for j in jobs if j["status"] == "success"}
        superseded = retire(hub_dir, project, ev.get("ref"), passed, ev.get("sha"), event_epoch,
                            ack=ack) if passed else 0
        if not jobs:
            row = write("ci.%s.pipeline" % project,
                        "no job ran on %s -- nothing was deployed, verified or tested: the pipeline "
                        "was rejected before any job started (usually its CI configuration)"
                        % (ev.get("ref") or "?"),
                        severity="critical", code="ci_config_rejected", details=details,
                        context=dict(base_ctx, jobs=""))
            return {"recorded": True, "severity": "critical", "fingerprint": row.get("fingerprint"),
                    "reason": "no jobs"}
        failed = [j["name"] for j in jobs if j["status"] == "failed" and not j.get("allow_failure")]
        if not failed:
            return {"recorded": False, "reason": "only allowed failures", "superseded": superseded}
        if all(n in ignored for n in failed):
            return {"recorded": False, "reason": "all failed jobs ignored"}
        deployish = _critical(failed)
        row = write("ci.%s.pipeline" % project,
                    "pipeline failed on %s: %s%s" % (ev.get("ref") or "?", ", ".join(failed[:4]),
                                                    _trigger_note(ev.get("trigger", ""), deployish)),
                    severity="critical" if deployish else "error", code="ci_pipeline_failed",
                    details=details, context=dict(base_ctx, jobs=",".join(failed[:8])))
        return {"recorded": True, "severity": "critical" if deployish else "error",
                "fingerprint": row.get("fingerprint"), "reopened": bool(row.get("reopened")),
                "superseded": superseded}

    if kind == "job":
        name = ev.get("job") or "job"
        if status == "success":
            n = retire(hub_dir, project, ev.get("ref"), {name}, ev.get("sha"), event_epoch, ack=ack)
            return {"recorded": False, "reason": "status=success", "superseded": n}
        if status != "failed":
            return {"recorded": False, "reason": "status=%s" % (status or "none")}
        if name in ignored:
            return {"recorded": False, "reason": "job ignored"}
        if ev.get("allow_failure"):
            return {"recorded": False, "reason": "allow_failure"}
        deployish = _critical([name])
        reason = ev.get("failure_reason")
        row = write("ci.%s.%s" % (project, name),
                    "CI job %s failed on %s%s%s" % (name, ev.get("ref") or "?",
                                                   " (%s)" % reason if reason else "",
                                                   _trigger_note(ev.get("trigger", ""), deployish)),
                    severity="critical" if deployish else "error", code="ci_job_failed",
                    details=details, context=dict(base_ctx, job=name, reason=reason or ""))
        return {"recorded": True, "severity": "critical" if deployish else "error",
                "fingerprint": row.get("fingerprint"), "reopened": bool(row.get("reopened"))}

    if kind == "deploy":
        # A deploy step reporting its own outcome. Success is proven by the release record
        # (POST /hub/api/deploy); this path exists for the outcomes that record can never carry.
        if status == "success":
            return {"recorded": False, "reason": "a successful release is recorded by /hub/api/deploy"}
        restored = ev.get("restored_sha") or ""
        if status in ("rolled_back", "rolledback", "restored") or restored:
            row = write("ci.%s.rollback" % project,
                        "deploy rolled the host back to %s -- it is NOT running the commit that was "
                        "pushed" % (restored[:12] or "the previous commit"),
                        severity="critical", code="deploy_rolled_back",
                        details="rejected sha %s · restored %s" % (ev.get("sha", "")[:12] or "?",
                                                                  restored[:12] or "?"),
                        context=dict(base_ctx, restored_sha=restored[:12]))
            return {"recorded": True, "severity": "critical", "fingerprint": row.get("fingerprint")}
        if status == "failed":
            row = write("ci.%s.deploy" % project,
                        "deploy failed on %s%s" % (ev.get("ref") or "?",
                                                   " (%s)" % ev["failure_reason"] if ev.get("failure_reason") else ""),
                        severity="critical", code="deploy_failed", details=details, context=base_ctx)
            return {"recorded": True, "severity": "critical", "fingerprint": row.get("fingerprint")}
        return {"recorded": False, "reason": "status=%s" % (status or "none")}

    return {"recorded": False, "reason": "kind=%s not handled" % (kind or "none")}


# ── The raw delivery, kept ──

def _log_paths(hub_dir):
    return Path(hub_dir) / "ci-events.jsonl", Path(hub_dir) / "ci-events.prev.jsonl"


def retain(hub_dir, body, outcome, *, record=None) -> None:
    """Keep the raw delivery so any row can be traced to the bytes that produced it. Rotates
    BEFORE writing. Never raises; a failure is recorded as a warning row, never swallowed."""
    cur = None
    try:
        cur, prev = _log_paths(hub_dir)
        cur.parent.mkdir(parents=True, exist_ok=True)
        try:
            if cur.exists() and cur.stat().st_size >= EVENT_LOG_MAX_BYTES:
                if prev.exists():
                    prev.unlink()
                cur.replace(prev)
        except OSError:
            pass
        ev = normalize(body) if isinstance(body, dict) else {}
        rec = {"at": time.time(), "kind": ev.get("kind", ""), "project": ev.get("project", ""),
               "pipeline": ev.get("pipeline", ""), "job": ev.get("job_id") or ev.get("job", ""),
               "ref": ev.get("ref", ""), "sha": ev.get("sha", ""), "status": ev.get("status", ""),
               "outcome": outcome, "body": body}
        with ProcessFileLock(cur.parent, name=".ci-events.lock", timeout=3):
            with cur.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    except Exception as exc:                                 # noqa: BLE001
        try:
            (record or (lambda *a, **k: errorlog.record(hub_dir, *a, **k)))(
                "hub.ci", "a CI delivery could not be retained: %s" % type(exc).__name__,
                severity="warning", code="ci_event_not_retained",
                details="%s -- %s" % (str(exc)[:200], cur or "(path not resolved)"),
                context={"component": "ci"})
        except Exception:                                    # noqa: BLE001
            pass


#: Pipeline states in which the pipeline has not finished: its result is still to come.
ACTIVE = frozenset({"created", "waiting_for_resource", "preparing", "pending", "running",
                    "scheduled"})


def sha_status(hub_dir, sha: str) -> dict:
    """What CI last said about one commit, from the retained deliveries -- never their bodies.

    ``{"sha", "found", "status", "active", "project", "pipeline", "at"}``: ``found`` is False
    when no pipeline delivery for the sha has arrived (yet); ``active`` is True while the newest
    pipeline for it is not terminal. A reader that must not act while a pipeline runs (an
    unattended launcher re-offering a handed-back task) asks here instead of the CI provider, so
    it needs no provider credential and works for any sender this module understands."""
    sha = re.sub(r"[^0-9a-f]", "", str(sha or "").strip().lower())[:40]
    out = {"sha": sha, "found": False, "status": "", "active": False, "project": "",
           "pipeline": "", "at": None}
    if len(sha) < 7:
        return out
    newest = None
    cur, prev = _log_paths(hub_dir)
    for path in (cur, prev):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if sha not in line:
                continue                       # cheap pre-filter before parsing
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "pipeline":
                continue
            got = str(rec.get("sha") or "").lower()
            if not got or not (got.startswith(sha) or sha.startswith(got)):
                continue
            if newest is None or float(rec.get("at") or 0) >= float(newest.get("at") or 0):
                newest = rec
    if newest is None:
        return out
    status = str(newest.get("status") or "")
    out.update(found=True, status=status, active=status in ACTIVE,
               project=str(newest.get("project") or ""), pipeline=str(newest.get("pipeline") or ""),
               at=newest.get("at"))
    return out


def retained(hub_dir, *, pipeline="", job="", project="", limit=20) -> tuple[list, dict]:
    """(deliveries newest first, a description of the store). The store names its own path,
    size and writability, so an empty answer says WHY it is empty."""
    want_project = slug(project)
    out = []
    cur, prev = _log_paths(hub_dir)
    for path in (cur, prev):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if pipeline and str(rec.get("pipeline") or "") != str(pipeline):
                continue
            if job and str(rec.get("job") or "") != str(job):
                continue
            if want_project and str(rec.get("project") or "") != want_project:
                continue
            out.append(rec)
    out.sort(key=lambda r: r.get("at") or 0, reverse=True)
    store = {"path": str(cur), "exists": cur.exists(),
             "bytes": cur.stat().st_size if cur.exists() else 0,
             "rotated_bytes": prev.stat().st_size if prev.exists() else 0,
             "max_bytes_per_file": EVENT_LOG_MAX_BYTES,
             "writable": os.access(str(cur.parent if cur.parent.exists() else cur.parent.parent), os.W_OK)}
    return out[:max(1, min(int(limit or 20), 200))], store
