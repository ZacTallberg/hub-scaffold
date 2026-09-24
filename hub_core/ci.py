"""CI pipeline events, in one neutral shape, onto the operational error stream.

Any CI system (a hosted runner's webhook, a self-hosted pipeline's final step, a cron job
that checks a build) reduces to the same few facts: which PROJECT, which REF and commit, the
overall STATUS, and each JOB's name and status. An adopter's adapter translates its CI's
webhook into that shape and posts it to ``/hub/api/ci-event``; this module does the rest:

  * a failed JOB becomes a row under ``ci.<project>.<job>`` with ref/sha/url/actor in its
    context — a problem the fold keys on (project, job), so a job failing every run is ONE
    line with a count;
  * a failed pipeline whose failed jobs are unknown lands as one ``ci.<project>.pipeline``
    roll-up naming the jobs it could see;
  * a branch run is recorded like any other, and the read-time bar (errorlog.passes_bar)
    keeps it off the queue unless the ref is one a push DEPLOYS from (HUB_DEPLOY_REFS) — the
    branch's author owns it, and it stays countable under ``?include=all``;
  * a PASS is stamped per (project, job). A job's failure rows are retired only by that job
    itself passing LATER on the same ref — positive evidence, never absence: a job that
    stopped running (moved to manual, deleted from the pipeline) never fails again, and a
    later green deploy must not read as a green streak for it.

Framework-free; every function takes the hub dir explicitly. Sidecar state only.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import errorlog
from .process_lock import ProcessFileLock

_PASSES_MAX = 2000
_SLUG = re.compile(r"[^a-z0-9_.-]+")
FAILED = ("failed", "failure", "error", "errored", "broken", "timed_out", "timeout")
PASSED = ("success", "succeeded", "passed", "ok", "green")
PIPELINE = "(pipeline)"          # the pass-stamp name for a whole green pipeline


def slug(value, limit=80) -> str:
    return _SLUG.sub("-", str(value or "").strip().lower()).strip("-.")[:limit]


def _passes_path(hub_dir) -> Path:
    return Path(hub_dir) / "ci-job-passes.json"


def read_passes(hub_dir) -> dict:
    """{"<project>.<job>@<ref>": epoch-of-last-pass}."""
    try:
        value = json.loads(_passes_path(hub_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def last_pass(passes: dict, project: str, job: str, ref: str) -> float:
    try:
        return float((passes or {}).get("%s.%s@%s" % (project, job, ref)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _mark_passes(hub_dir, project: str, ref: str, jobs, when: float) -> int:
    names = sorted({(j if j == PIPELINE else slug(j)) for j in (jobs or ())
                    if j == PIPELINE or slug(j)})
    if not project or not names:
        return 0
    try:
        Path(hub_dir).mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(Path(hub_dir), name=".ci-job-passes.lock", timeout=3):
            data = read_passes(hub_dir)
            for name in names:
                key = "%s.%s@%s" % (project, name, ref)
                data[key] = max(float(data.get(key) or 0), float(when))
            if len(data) > _PASSES_MAX:
                data = dict(sorted(data.items(), key=lambda kv: float(kv[1] or 0))[-_PASSES_MAX:])
            path = _passes_path(hub_dir)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, path)
    except Exception:                                        # noqa: BLE001
        return 0                 # a missed stamp only keeps a row OPEN; it never hides one
    return len(names)


def normalize(event: dict) -> tuple:
    """(clean_event, problem) — the shape every adapter posts, validated. `jobs` is a list of
    {name, status, url?}; a bare list of names is accepted as the FAILED jobs of a failed
    pipeline."""
    if not isinstance(event, dict):
        return None, "object_required"
    project = slug(event.get("project"))
    if not project:
        return None, "need_project"
    status = str(event.get("status") or "").strip().lower()
    if status not in FAILED + PASSED:
        return None, "status must be one of: failed, success (got %r)" % status[:40]
    jobs = []
    for item in (event.get("jobs") or [])[:64]:
        if isinstance(item, str):
            item = {"name": item, "status": "failed" if status in FAILED else "success"}
        if not isinstance(item, dict):
            continue
        name = slug(item.get("name"))
        if not name:
            continue
        jstatus = str(item.get("status") or "").strip().lower()
        jobs.append({"name": name, "status": jstatus,
                     "url": str(item.get("url") or "")[:600]})
    return {"project": project,
            "ref": slug(event.get("ref") or "main", 120) or "main",
            "sha": re.sub(r"[^0-9a-f]", "", str(event.get("sha") or "").lower())[:40],
            "status": "failed" if status in FAILED else "success",
            "jobs": jobs,
            "url": str(event.get("url") or "")[:600],
            "actor": str(event.get("actor") or "")[:80],
            "source": str(event.get("source") or "")[:40],
            "details": str(event.get("details") or "")}, None


def retire_passed(hub_dir, *, now: float | None = None) -> int:
    """Acknowledge every open ``ci.<project>.<job>`` row whose job PASSED on the same ref
    after the row was written. Idempotent (acks are). Returns how many rows it retired."""
    now = now or time.time()
    passes = read_passes(hub_dir)
    if not passes:
        return 0
    rows, _meta = errorlog.read(hub_dir, limit=errorlog.KEEP_ROWS)
    acked = errorlog.read_acked(hub_dir)
    retired = 0
    for row in rows:
        src = str(row.get("source") or "").lower()
        parts = src.split(".")
        if len(parts) < 3 or parts[0] != "ci":
            continue
        if errorlog.is_acked(row, acked):
            continue
        project, job = parts[1], ".".join(parts[2:])
        ctx = row.get("context") or {}
        ref = str(ctx.get("ref") or "main").lower()
        occurred = max(float(row.get("epoch") or 0), float(row.get("last_occurrence") or 0))
        if job == "pipeline":
            # A roll-up is retired by EACH job it names passing later — or, when it could
            # name none, by a whole pipeline on the same ref passing later.
            named = ctx.get("jobs") if isinstance(ctx.get("jobs"), list) else []
            named = [slug(n) for n in named if slug(n)]
            if named:
                stamps = [last_pass(passes, project, n, ref) for n in named]
            else:
                stamps = [last_pass(passes, project, PIPELINE, ref)]
            passed = min(stamps) if stamps and all(stamps) else 0.0
            what = ", ".join(named) or "the pipeline"
        else:
            passed = last_pass(passes, project, job, ref)
            what = job
        if passed and passed > occurred:
            entry = errorlog.ack(hub_dir, str(row.get("fingerprint") or ""), actor="hub", note=(
                "%s passed on %s after this failure (%s)" % (
                    what, ref, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(passed)))))
            if entry:
                acked[str(row.get("fingerprint") or "")] = entry
                retired += 1
    return retired


def ingest(hub_dir, event: dict, *, record=None, now: float | None = None) -> dict:
    """Apply one CI event. `record` is the writer for rows (the adapter passes its
    record-and-wake wrapper); defaults to errorlog.record. Returns what it did."""
    now = now or time.time()
    record = record or (lambda *a, **k: errorlog.record(hub_dir, *a, **k))
    clean, problem = normalize(event)
    if problem:
        return {"ok": False, "error": problem}
    project, ref = clean["project"], clean["ref"]
    passed = [j["name"] for j in clean["jobs"] if j["status"] in PASSED]
    failed = [j for j in clean["jobs"] if j["status"] in FAILED]
    # A job that passed in a red pipeline passed all the same.
    marked = _mark_passes(hub_dir, project, ref, passed, now)
    out = {"ok": True, "project": project, "ref": ref, "status": clean["status"],
           "passes_marked": marked, "recorded": []}
    base_ctx = {"project": project, "ref": ref, "sha": clean["sha"], "url": clean["url"],
                "actor": clean["actor"], "source": clean["source"], "component": "ci"}
    if clean["status"] == "failed":
        if failed:
            for job in failed:
                row = record("ci.%s.%s" % (project, job["name"]),
                             "job %s failed on %s" % (job["name"], ref),
                             severity="error", code="ci_job_failed",
                             details=clean["details"],
                             context={**base_ctx, "job": job["name"],
                                      "url": job["url"] or clean["url"]})
                out["recorded"].append({"source": "ci.%s.%s" % (project, job["name"]),
                                        "fingerprint": row.get("fingerprint")})
        else:
            names = [j["name"] for j in clean["jobs"] if j["status"] not in PASSED]
            row = record("ci.%s.pipeline" % project,
                         "pipeline failed on %s: %s" % (ref, ", ".join(names) or "no job ran"),
                         severity="error", code="ci_pipeline_failed",
                         details=clean["details"], context={**base_ctx, "jobs": names})
            out["recorded"].append({"source": "ci.%s.pipeline" % project,
                                    "fingerprint": row.get("fingerprint")})
    else:
        # A WHOLE green pipeline is its own positive observation (it retires a roll-up that
        # could name no job), and it stamps the channel as seen: this project's CI lane
        # reaches the hub, which is what the app-health verdict asks.
        out["passes_marked"] += _mark_passes(hub_dir, project, ref, [PIPELINE], now)
        errorlog.touch_seen(hub_dir, "ci.%s.pipeline" % project, now)
    out["retired"] = retire_passed(hub_dir, now=now)
    return out
