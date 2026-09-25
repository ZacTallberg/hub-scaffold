"""Every service this project runs, and whether its failures can REACH the board.

"A full view of every error in a service" has a precondition the error stream cannot state
about itself: that the service's failures can get here at all. A coverage strip says "app
lane: live" the moment ANY service forwards, which hides the one whose forwarder is not
wired; a deploy list says "serving" for a service whose CI has never posted an event; and
liveness, deploy state, forwarding and open problems live on four different surfaces.

This module folds them into one row per service with a VERDICT that names what cannot be
seen:

  observed  liveness probed recently, and the service's forwarder (or CI lane) has reported
            at least once — an empty problem list for it MEANS something
  partial   something is missing; the row names what, as an observation, never a guessed
            cause
  dark      neither the forwarder nor a CI lane has EVER reported: silence is not health
  unbuilt   declared, never deployed, never reported anything: nothing to observe yet

Beside the verdict, never inside it, rides the CHAT channel: whether a fault from inside the
service's in-app assistant has ever reached the board (reported / silent). A service with no
chat and one whose chat is not wired look identical until a forced failed turn tells them
apart, so silent is stated, never read as healthy.

Evidence only. "Forwarder armed" is the durable per-family first/last-seen stamp the error
store keeps (errorlog.read_sources_seen), plus the retained window — never a guess from a
config file on a machine the hub cannot read. Framework-free; the adapter supplies the
declared services (HUB_APPS), the hub's own deploy records, and the liveness cache.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import errorlog, problems as _problems

LIVENESS_STALE_S = 300          # an older probe is history, not an observation
SWEEP_EVERY_S = 60
PROBE_TIMEOUT_S = 4.0
_SWEEPING: dict = {}


def _liveness_path(hub_dir) -> Path:
    return Path(hub_dir) / "app-liveness.json"


def read_liveness(hub_dir) -> dict:
    try:
        value = json.loads(_liveness_path(hub_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """One bounded GET. Any 2xx/3xx is up; a 5xx is erroring; no answer is unreachable. A
    probe NEVER raises and never takes longer than its timeout."""
    started = time.monotonic()
    out = {"at": time.time(), "url": url}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HubLivenessProbe/1.0",
                                                   "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:        # noqa: S310
            out["status"] = resp.status
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
    except Exception as exc:                                 # noqa: BLE001
        out["status"] = None
        out["error"] = type(exc).__name__
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    status = out.get("status")
    if status is None:
        out["state"] = "unreachable"
    elif status >= 500:
        out["state"] = "erroring"
    elif status in (401, 403):
        # An authenticated front door that refuses an anonymous probe is ANSWERING.
        out["state"] = "up"
    elif status >= 400:
        out["state"] = "down"
    else:
        out["state"] = "up"
    return out


def sweep(hub_dir, apps: dict, *, force: bool = False) -> dict:
    """Probe every declared health URL whose last probe is older than SWEEP_EVERY_S, and
    persist the results. Called from a background thread while somebody reads the surface —
    liveness is an observation with an expiry, and every row says how old its answer is."""
    now = time.time()
    state = read_liveness(hub_dir)
    changed = False
    for slug, cfg in (apps or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        url = str(cfg.get("health_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        prior = state.get(slug) if isinstance(state.get(slug), dict) else {}
        if not force and prior and (now - float(prior.get("at") or 0)) < SWEEP_EVERY_S:
            continue
        result = probe(url)
        if result["state"] == "up":
            result["last_ok_at"] = result["at"]
        elif prior.get("last_ok_at"):
            result["last_ok_at"] = prior["last_ok_at"]
        state[slug] = result
        changed = True
    if changed:
        try:
            path = _liveness_path(hub_dir)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            pass
    return state


def sweep_in_background(hub_dir, apps: dict) -> bool:
    """Start one sweep thread per hub dir unless one is already running. Never blocks the
    request that asked."""
    key = str(hub_dir)
    running = _SWEEPING.get(key)
    if running and running.is_alive():
        return False
    thread = threading.Thread(target=sweep, args=(hub_dir, apps), name="hub-liveness-sweep",
                              daemon=True)
    _SWEEPING[key] = thread
    thread.start()
    return True


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def host_liveness(host: str, tenants: list, live: dict, now: float) -> tuple:
    """(liveness row, how it was derived) for a HOST with no health URL of its own, from the
    probes of the services it serves (those declaring `hosted_in: <host>`).

    A host that only serves other services has no public URL to probe, so it read "liveness is
    not probed" forever while every service it serves was probed every sweep. Its liveness is
    theirs: up when any served service answered on a FRESH probe; down only when EVERY freshly
    probed one is down (that is the host, not one service); `({}, "")` when none has a fresh
    probe, so the honest "not probed" gap still stands. It never borrows one tenant's URL —
    that would charge the host with that tenant's own outage."""
    fresh = []
    for t in tenants:
        row = live.get(t)
        if not isinstance(row, dict) or row.get("at") is None:
            continue
        at = _float(row.get("at"))
        if at is None:
            continue
        if now - at <= LIVENESS_STALE_S:
            fresh.append((t, row, at))
    if not fresh:
        return {}, ""
    up = [(t, r, at) for t, r, at in fresh if r.get("state") == "up"]
    newest = max(at for _t, _r, at in fresh)
    how = ("liveness derived from the %d service(s) %s serves: %d of %d freshly probed answering"
           % (len(tenants), host, len(up), len(fresh)))
    if up:
        last_ok = max((_float(r.get("last_ok_at")) if r.get("last_ok_at") is not None else at)
                      for _t, r, at in up)
        return {"state": "up", "at": newest, "last_ok_at": last_ok}, how
    return {"state": "down", "at": newest}, how


def rows(hub_dir, state=None, *, apps: dict | None = None, native: str = "hub",
         native_deploy: dict | None = None, now: float | None = None) -> tuple:
    """(rows, metadata): one row per declared or reporting service, dark first."""
    now = now or time.time()
    apps = apps or {}
    live = read_liveness(hub_dir)
    families = errorlog.read_sources_seen(hub_dir)
    err_rows, _meta = errorlog.read(hub_dir, limit=errorlog.KEEP_ROWS)
    probs, _pmeta = _problems.read(hub_dir, state, include="all", now=now)
    # THE WINDOW IS EVIDENCE TOO: a row still retained from app.<slug> proves the channel
    # even if the durable stamp began after it.
    for r in err_rows:
        for fam in (errorlog.family_of(r.get("source")), errorlog.chat_family_of(r.get("source"))):
            if not fam:
                continue
            ep = float(r.get("epoch") or 0)
            prior = families.get(fam) if isinstance(families.get(fam), dict) else None
            if prior is None or float(prior.get("last") or 0) < ep:
                families[fam] = {"first": (prior or {}).get("first") or ep, "last": ep,
                                 "count": int((prior or {}).get("count") or 0) or 1}
    slugs = {str(s).lower() for s in apps} | {native}
    for fam in families:
        if fam.startswith("app."):
            slugs.add(fam[len("app."):])
    # A RETIRED service is not a service to observe, whichever channel still names it. Its
    # process may keep running (and its forwarder keep posting) until somebody stops it, which
    # put a retired service back in this table as "partial: liveness not probed" — gaps that ARE
    # the retirement, not something anyone can arm. Its errors still fold into problems; only the
    # observe-me verdict is withheld. A service hosted inside another stays: the host still
    # speaks for it.
    retired = {str(k).lower() for k, v in apps.items()
               if isinstance(v, dict) and str(v.get("status") or "") == "retired"
               and not v.get("hosted_in")}
    slugs -= retired - {native}
    tenants_of: dict = {}
    for t, v in apps.items():
        host = str((v or {}).get("hosted_in") or "").lower() if isinstance(v, dict) else ""
        if host and str(t).lower() not in retired:
            tenants_of.setdefault(host, []).append(str(t).lower())
    out = []
    for slug in sorted(slugs):
        cfg = apps.get(slug) if isinstance(apps.get(slug), dict) else {}
        is_native = slug == native
        hosted_in = str(cfg.get("hosted_in") or "").lower()
        project = str(cfg.get("project") or hosted_in or slug).lower()
        lv = live.get(slug) if isinstance(live.get(slug), dict) else {}
        fwd = families.get("app." + slug) if isinstance(families.get("app." + slug), dict) else {}
        chat = families.get("chat." + slug) if isinstance(families.get("chat." + slug), dict) else {}
        ci = families.get("ci." + project) if isinstance(families.get("ci." + project), dict) else {}
        window = {"total": 0, "deferred": 0, "by_kind": {}}
        for r in err_rows:
            src = str(r.get("source") or "").lower()
            if not src.startswith("app." + slug + "."):
                continue
            window["total"] += 1
            kind = src.split(".")[2] if len(src.split(".")) > 2 else "server"
            window["by_kind"][kind] = window["by_kind"].get(kind, 0) + 1
            if not errorlog.passes_bar(r)[0]:
                window["deferred"] += 1
        mine = [p for p in probs if p["on_board"] and p["state"] != "resolved"
                and (p["where"] == slug or (p["kind"] == "ci" and p["where"] == project))]
        unclaimed = [p for p in mine if p["state"] == "unclaimed"]
        dep = (native_deploy or {}) if is_native else {}
        derived = ""
        if not lv and not is_native and not cfg.get("health_url") and tenants_of.get(slug):
            lv, derived = host_liveness(slug, sorted(tenants_of[slug]), live, now)
        gaps = []
        # NOTES are observations that are not defects: a stage nobody can clear from here (it
        # ends with a host step) held as a GAP keeps the row `partial` forever, and every
        # responder offered it comes back not-cleared on a condition no agent can change.
        notes = [derived] if derived else []
        if is_native:
            # The hub records its own server errors directly; it has no forwarder to wire.
            fwd = fwd or {"first": 0, "last": now, "count": window["total"]}
        elif not fwd:
            gaps.append("its forwarder has never reported: wire patterns/error-visibility.md "
                        "(POST /hub/api/app-error with app=%s), or its failures stay in its "
                        "own logs" % slug)
        if hosted_in and ci:
            notes.append("ships inside %s: its CI events are the host's" % hosted_in)
        lv_age = (now - float(lv["at"])) if lv.get("at") else None
        if not cfg.get("health_url") and not is_native and not derived:
            gaps.append("liveness is not probed: no health_url declared for it in HUB_APPS")
        elif lv and (lv_age is None or lv_age > LIVENESS_STALE_S):
            gaps.append("liveness was last probed %d min ago, so \"%s\" is what the probe saw "
                        "then, not now" % (int((lv_age or 0) // 60), lv.get("state") or "unknown"))
        elif lv and lv.get("state") in ("down", "erroring", "unreachable"):
            gaps.append("not answering: %s" % lv.get("state"))
        elif cfg.get("health_url") and not lv:
            gaps.append("liveness has not been probed yet (a sweep starts when this is read)")
        never_anything = not fwd and not ci and window["total"] == 0 and not mine
        if never_anything and not is_native and cfg.get("status") == "planned":
            verdict = "unbuilt"
            gaps = ["declared but never deployed: nothing to observe yet"]
        else:
            verdict = "dark" if (not fwd and not ci) else ("partial" if gaps else "observed")
        out.append({
            "slug": slug,
            "native": is_native,
            "owners": [str(o) for o in (cfg.get("owners") or [])],
            "url": str(cfg.get("url") or ""),
            "project": project,
            "hosted_in": hosted_in,
            "deploy": ({"at": dep.get("at") or "", "sha": str(dep.get("sha") or "")[:12]}
                       if dep else None),
            "live": {"state": lv.get("state") or ("self" if is_native else "unknown"),
                     "status": lv.get("status"), "latency_ms": lv.get("latency_ms"),
                     "checked_age_s": int(lv_age) if lv_age is not None else None,
                     "last_ok_age_s": (int(now - float(lv["last_ok_at"])) if lv.get("last_ok_at") else None)},
            "forwarder": {"state": "native" if is_native else ("armed" if fwd else "never"),
                          "last_age_s": int(now - float(fwd["last"])) if fwd.get("last") else None,
                          "count": int(fwd.get("count") or 0)},
            "chat": {"state": "reported" if chat else "silent",
                     "last_age_s": int(now - float(chat["last"])) if chat.get("last") else None,
                     "count": int(chat.get("count") or 0),
                     "window": int(window["by_kind"].get("agent", 0))},
            "ci": {"state": "seen" if ci else "never",
                   "last_age_s": int(now - float(ci["last"])) if ci.get("last") else None,
                   "count": int(ci.get("count") or 0)},
            "window": window,
            "problems": {"open": len(mine), "unclaimed": len(unclaimed),
                         "in_flight": sum(1 for p in mine if p["state"] == "in_flight"),
                         "escalated": sum(1 for p in mine if p["state"] == "escalated"),
                         "critical": sum(1 for p in mine if p["severity"] == "critical"),
                         "oldest_s": max((int(p.get("age_s") or 0) for p in unclaimed), default=0),
                         "ids": [p["id"] for p in mine[:4]]},
            "gaps": gaps,
            "notes": notes,
            "verdict": verdict,
        })
    rank = {"dark": 0, "partial": 1, "observed": 2, "unbuilt": 3}
    out.sort(key=lambda r: (rank.get(r["verdict"], 3), -r["problems"]["unclaimed"], r["slug"]))
    counts = {k: sum(1 for r in out if r["verdict"] == k) for k in rank}
    meta = {"counts": counts, "total": len(out), "retired_withheld": sorted(retired - {native}),
            "sources_seen": errorlog.sources_seen_store(hub_dir),
            "liveness_stale_s": LIVENESS_STALE_S,
            "note": ("a verdict of 'never' means nothing when the stamp store is not writable; "
                     "sources_seen.writable says which")}
    return out, meta


def doctor(hub_dir, slug: str, state=None, *, apps: dict | None = None, native: str = "hub",
           native_deploy: dict | None = None, now: float | None = None) -> dict:
    """One service, diagnosed from evidence: its health row (synthesized even for a service
    nobody declared, if it has ever reported), its OPEN problems — resolved ones are history,
    listed apart — and a plain WAITING vs BLOCKED reading: a held or escalated problem is
    somebody's work in flight, not a blocker."""
    now = now or time.time()
    slug = str(slug or "").strip().lower()
    health, meta = rows(hub_dir, state, apps=apps, native=native, native_deploy=native_deploy,
                        now=now)
    row = next((r for r in health if r["slug"] == slug), None)
    probs, _m = _problems.read(hub_dir, state, include="resolved", app="", now=now)
    project = (row or {}).get("project") or slug
    mine = [p for p in probs if p["where"] == slug or (p["kind"] == "ci" and p["where"] == project)]
    open_ = [p for p in mine if p["state"] != "resolved"]
    history = [p for p in mine if p["state"] == "resolved"]
    blockers = [p for p in open_ if p["state"] == "unclaimed"]
    waiting = [p for p in open_ if p["state"] in ("in_flight", "escalated")]
    if row is None and slug in (meta.get("retired_withheld") or []):
        verdict = "retired"
        lines = ["%s is declared retired in HUB_APPS: it is not observed, and its remaining "
                 "errors still fold into problems" % slug]
    elif row is None:
        verdict = "unknown"
        lines = ["nothing named %r is declared in HUB_APPS or has ever reported to this hub" % slug]
    else:
        verdict = "blocked" if blockers else ("waiting" if waiting else row["verdict"])
        lines = list(row["gaps"]) + ["note: " + n for n in (row.get("notes") or [])]
        for p in blockers:
            lines.append("BLOCKED on %s (unclaimed %s): %s" % (
                p["id"], _problems.age_phrase(p.get("age_s")), p["title"][:160]))
        for p in waiting:
            what = (("escalated, waiting on %s" % (p.get("escalation") or {}).get("blocked_on"))
                    if p["state"] == "escalated" else _problems.holder_phrase(p))
            lines.append("WAITING on %s (%s): %s" % (p["id"], what, p["title"][:160]))
    return {"slug": slug, "verdict": verdict, "health": row, "lines": lines,
            "open": [{k: p.get(k) for k in ("id", "state", "severity", "title", "age_s",
                                             "since_last_s", "holder", "escalation", "cause")}
                     for p in open_],
            "history": [{k: p.get(k) for k in ("id", "title", "resolved")} for p in history[:10]],
            "sources_seen": meta.get("sources_seen")}
