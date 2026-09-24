"""From a task to the artifact actually serving it, hop by hop, with every unknown named.

"Is this task's work live?" is usually answered yes or no -- and a bare yes/no cannot show the
chain behind it: which commit, recorded how, carried by which verified deploy, first shipped in
which release, and whether the build serving RIGHT NOW still contains it. The ladder shows it:

    recorded      the commits the TASK ITSELF recorded as fields (``step --sha``) -- never
                  inferred from prose or borrowed evidence links, which can describe another
                  task's work
    verified      a verified deploy record (``audit_ok``) that carries one of them
    first_release the EARLIEST verified deploy carrying it, not merely the newest
    serving_now   whether the NEWEST verified deploy still contains it (``superseded`` if not)

Each hop carries its own state -- ``known`` / ``no`` / ``superseded`` / ``unknown`` -- and a hop
that cannot be established says ``unknown`` and WHY, never "no".

THE ANCESTRY CACHE lives in front of the containment question, keyed by (project, candidate,
target) with FULL 40-hex shas, because a board can span several repositories and a prefix is not
an identity across them. Two rules it lives by:

* NEVER CACHE AN UNAVAILABLE ANSWER. "The repository could not be asked" and "it is not an
  ancestor" are different facts; storing the first as the second writes a permanent "no" on the
  first day a checkout is missing.
* EVERY STORED VERDICT CARRIES ITS DATE AND WHAT PROVED IT. Ancestry of two FIXED shas is
  immutable -- that is why it is cacheable at all. Anything derived from a moving target ("what is
  serving now") is recomputed on every read and never stored.

Standard library only; the adapter supplies the state, the commit resolver and the cache path.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

from . import plan as _plan

FULL = 40
CACHE_MAX = 5000


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ancestry:
    """A persistent cache of DEFINITE containment answers, in front of a commit resolver."""

    def __init__(self, path, resolver):
        self.path = Path(path) if path else None
        self.resolver = resolver
        self._data = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except Exception:                                # noqa: BLE001 - a cache is never a dependency
                self._data = {}
        return self._data

    def _save(self) -> None:
        if not self.path or self._data is None:
            return
        try:
            data = self._data
            if len(data) > CACHE_MAX:                        # oldest answers go first
                data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", ""))[-CACHE_MAX:])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp%d" % os.getpid())
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:                                    # noqa: BLE001
            pass

    def contains(self, project: str, candidate: str, target: str) -> dict:
        """``{state: yes|no|unknown, why, cached}`` -- is `candidate` inside `target`?"""
        cand = str(candidate or "").lower()
        tgt = str(target or "").lower()
        if not cand or not tgt:
            return {"state": "unknown", "why": "missing sha", "cached": False}
        if tgt.startswith(cand) or cand.startswith(tgt):
            return {"state": "yes", "why": "same commit", "cached": False}
        full_c = cand if len(cand) == FULL else self.resolver.full(cand, project)
        full_t = tgt if len(tgt) == FULL else self.resolver.full(tgt, project)
        key = "%s|%s|%s" % (project or "", full_c, full_t) if full_c and full_t else ""
        if key:
            hit = self._load().get(key)
            if hit is not None:
                return {"state": "yes" if hit.get("contained") else "no",
                        "why": hit.get("evidence", ""), "at": hit.get("at"), "cached": True}
        verdict = self.resolver.contains(full_c or cand, full_t or tgt, project)
        if verdict is None:
            return {"state": "unknown", "cached": False,
                    "why": "no repository that holds both commits could be asked"}
        if key:
            self._load()[key] = {"contained": bool(verdict), "at": _now(),
                                 "evidence": "merge-base --is-ancestor %s %s"
                                             % (full_c[:12], full_t[:12])}
            self._save()
        return {"state": "yes" if verdict else "no", "cached": False,
                "why": "merge-base --is-ancestor answered %s" % ("yes" if verdict else "no")}


def _hop(name: str, state: str, detail: str, **extra) -> dict:
    return {"hop": name, "state": state, "detail": detail, **extra}


def ladder(task: dict, state: dict, resolver, cache_path=None) -> dict:
    """The evidence chain for one task, hop by hop, with every unknown named."""
    project = resolver.project_of(task) if resolver is not None else ""
    tid = (task or {}).get("id")
    hops = []
    out = {"task": tid, "project": project or None, "hops": hops, "complete": False}
    recorded = sorted(_plan.recorded_shas(task))
    deploys = [e for e in ((state or {}).get("entities") or {}).values()
               if isinstance(e, dict) and e.get("type") == "deploy"]
    closing = [d for d in deploys if tid and tid in (d.get("tasks_closed") or [])]
    if not recorded:
        hops.append(_hop("recorded", "unknown",
                         "this task recorded no commit of its own (step --sha), so nothing "
                         "downstream can be attributed to it"
                         + ("; %d deploy record(s) list it in tasks_closed" % len(closing)
                            if closing else "")))
        return out
    hops.append(_hop("recorded", "known", "%d commit(s) recorded by the task" % len(recorded),
                     shas=[s[:12] for s in recorded]))
    if not deploys:
        hops.append(_hop("verified", "unknown",
                         "no deploy record exists, so the Hub cannot say whether these commits "
                         "were ever verified live"))
        return out
    verified = sorted((d for d in deploys if d.get("audit_ok") is True and d.get("sha")),
                      key=lambda d: str(d.get("at") or ""))
    if not verified:
        hops.append(_hop("verified", "no", "%d deploy record(s), none verified (audit_ok)"
                         % len(deploys)))
        return out
    anc = Ancestry(cache_path, resolver)
    carrying, unknown = [], 0
    for d in verified:                                       # OLDEST first
        target = str(d.get("sha") or "")
        hit = ""
        for c in recorded:
            verdict = anc.contains(project, c, target)
            if verdict["state"] == "yes":
                hit = c
                break
            if verdict["state"] == "unknown":
                unknown += 1
        if hit:
            carrying.append((d, hit))
    if not carrying:
        hops.append(_hop("verified", "unknown" if unknown else "no",
                         ("the containing repository could not be asked for %d question(s); "
                          "configure HUB_PROJECT_REPOS for this task's project" % unknown)
                         if unknown else
                         "no verified deploy carries any commit this task recorded"))
        return out
    first, shipped = carrying[0]
    hops.append(_hop("verified", "known",
                     "verified deploy %s carries %s" % (str(first.get("sha"))[:12], shipped[:12]),
                     deploy=first.get("id"), at=first.get("at") or ""))
    hops.append(_hop("first_release", "known",
                     "first carried by the release that built %s" % str(first.get("sha"))[:12],
                     deploy=first.get("id"), sha=str(first.get("sha"))[:12],
                     at=first.get("at") or "", build=first.get("build") or ""))
    latest = verified[-1]
    still = anc.contains(project, shipped, str(latest.get("sha") or ""))
    state_name = {"yes": "known", "no": "superseded", "unknown": "unknown"}[still["state"]]
    phrase = {"yes": "the newest verified deploy %s still contains %s",
              "no": "the newest verified deploy %s NO LONGER contains %s",
              "unknown": "cannot say whether the newest verified deploy %s contains %s"}
    hops.append(_hop("serving_now", state_name,
                     phrase[still["state"]] % (str(latest.get("sha"))[:12], shipped[:12]),
                     deploy=latest.get("id"), sha=str(latest.get("sha"))[:12],
                     at=latest.get("at") or "", evidence=still.get("why", ""),
                     cached=still.get("cached", False)))
    out.update({"complete": still["state"] == "yes", "shipped": shipped[:12]})
    return out
