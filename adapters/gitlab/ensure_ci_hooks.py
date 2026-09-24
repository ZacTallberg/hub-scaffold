"""ensure_ci_hooks.py — every GitLab project reports its CI results to the hub, without anybody
remembering to wire it.

The hub learns that a pipeline failed from ONE channel: a webhook posted to
``POST <hub>/api/ci-event`` (see hub_core/ci_events.py). A project nobody registered is dark
by default, and nothing on the board says so — an empty CI section reads as "all green". This
reconciler gives every project in scope the project hook, and keeps giving it to the projects
that appear later (run it on a schedule).

Two things learned on the origin instance shape it:

* GROUP HOOKS CAN LOOK HEALTHY AND NEVER FIRE. On an instance without the licence tier that fans
  group hooks out, the records exist and the manual "test" button still delivers — which is
  exactly why they look wired — while no pipeline event ever arrives. Project hooks work on
  every tier, so this registers project hooks.
* A CHECK THAT CANNOT SEE IS NOT A CHECK THAT PASSED. GitLab answers /hooks with a LIST and a
  refusal with an OBJECT; code that iterated the answer without checking its shape crashed on
  the refusal, in the one command whose job was to say why a project was dark. A project whose
  hooks cannot be listed is reported ``unknown``, never ``ok``, and the run exits non-zero.

SAFETY. It only ever CREATES a missing hook, or turns the pipeline/job flags ON for a hook that
already points at this hub. It never deletes a hook, never edits a hook pointing anywhere else,
and never touches a project it could not read. Without ``--apply`` it only reports. A detector
that is wrong here does nothing.

Usage:
  GITLAB_API=https://gitlab.example.com/api/v4 GITLAB_TOKEN=... \\
  HUB_CI_EVENT_URL=https://hub.example.com/hub/api/ci-event HUB_CI_WEBHOOK_SECRET=... \\
  python ensure_ci_hooks.py [--group team] [--project team/budget-app] [--apply]

Exit status: 0 every project in scope is registered (or was just registered), 1 something was
missing and not applied, 3 at least one project could not be read (unknown). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


class Refused(Exception):
    pass


def _ctx(insecure: bool):
    if not insecure:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def call(api, token, method, path, body=None, *, insecure=False):
    req = urllib.request.Request(api.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("PRIVATE-TOKEN", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ctx(insecure)) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(text) if text.strip() else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:300]
        try:
            return e.code, json.loads(text), dict(e.headers)
        except ValueError:
            return e.code, text, dict(e.headers)
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)[:200], {}


def paged(api, token, path, *, insecure=False) -> list:
    """Every page, or raise: a reconciler that silently reads page 1 and reports 'every
    project has a hook' is the failure this file exists to end."""
    page, out = 1, []
    while True:
        sep = "&" if "?" in path else "?"
        status, rows, headers = call(api, token, "GET", f"{path}{sep}per_page=100&page={page}",
                                     insecure=insecure)
        if status != 200 or not isinstance(rows, list):
            raise Refused(f"GET {path} page {page} -> {status} {str(rows)[:160]}")
        out.extend(r for r in rows if isinstance(r, dict))
        nxt = str(headers.get("X-Next-Page") or headers.get("x-next-page") or "").strip()
        if not nxt:
            return out
        page = int(nxt)


def hook_state(hooks, url) -> str:
    """ok | disabled | missing | unknown — shape-tolerant: anything but a list is 'unknown'."""
    if not isinstance(hooks, list):
        return "unknown"
    mine = [h for h in hooks if isinstance(h, dict)
            and str(h.get("url") or "").rstrip("/") == url.rstrip("/")]
    if not mine:
        return "missing"
    return "ok" if any(h.get("pipeline_events") and h.get("job_events") for h in mine) else "disabled"


def reconcile(api, token, url, secret, *, groups=(), projects=(), apply=False, insecure=False):
    scope = []
    if projects:
        for full in projects:
            status, row, _ = call(api, token, "GET", "/projects/" + urllib.parse.quote(full, safe=""),
                                  insecure=insecure)
            scope.append(row if status == 200 and isinstance(row, dict)
                         else {"path_with_namespace": full, "_unreadable": status})
    else:
        group_rows = ([{"id": urllib.parse.quote(g, safe=""), "full_path": g} for g in groups]
                      or paged(api, token, "/groups", insecure=insecure))
        for g in group_rows:
            try:
                scope.extend(paged(api, token, f"/groups/{g['id']}/projects?include_subgroups=true"
                                               f"&archived=false", insecure=insecure))
            except Refused as exc:
                scope.append({"path_with_namespace": g.get("full_path", "?") + "/*",
                              "_unreadable": str(exc)})
    seen, results = set(), []
    for p in scope:
        name = p.get("path_with_namespace") or "?"
        if name in seen:
            continue
        seen.add(name)
        if "_unreadable" in p or not p.get("id"):
            results.append((name, "unknown", "project could not be read: %s" % p.get("_unreadable")))
            continue
        status, hooks, _ = call(api, token, "GET", f"/projects/{p['id']}/hooks", insecure=insecure)
        state = hook_state(hooks if status == 200 else None, url)
        if state == "unknown":
            results.append((name, "unknown", "hooks could not be listed (%s): %s"
                            % (status, str(hooks)[:120])))
            continue
        if state == "ok":
            results.append((name, "ok", "registered"))
            continue
        if not apply:
            results.append((name, state, "WOULD %s" % ("CREATE" if state == "missing" else
                                                        "ENABLE pipeline/job events")))
            continue
        body = {"url": url, "token": secret, "pipeline_events": True, "job_events": True,
                "push_events": False, "enable_ssl_verification": not insecure}
        if state == "missing":
            st, out, _ = call(api, token, "POST", f"/projects/{p['id']}/hooks", body, insecure=insecure)
        else:
            hook = next(h for h in hooks if isinstance(h, dict)
                        and str(h.get("url") or "").rstrip("/") == url.rstrip("/"))
            st, out, _ = call(api, token, "PUT", f"/projects/{p['id']}/hooks/{hook['id']}", body,
                              insecure=insecure)
        results.append((name, "ok" if st in (200, 201) else state,
                        ("created" if state == "missing" else "enabled") if st in (200, 201)
                        else "FAILED %s: %s" % (st, str(out)[:120])))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--group", action="append", default=[], help="limit to a group (repeatable)")
    ap.add_argument("--project", action="append", default=[], help="limit to group/project (repeatable)")
    ap.add_argument("--apply", action="store_true", help="create/enable; without it, report only")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed CA)")
    args = ap.parse_args(argv)
    api, token = os.environ.get("GITLAB_API", ""), os.environ.get("GITLAB_TOKEN", "")
    url, secret = os.environ.get("HUB_CI_EVENT_URL", ""), os.environ.get("HUB_CI_WEBHOOK_SECRET", "")
    missing = [n for n, v in (("GITLAB_API", api), ("GITLAB_TOKEN", token),
                              ("HUB_CI_EVENT_URL", url), ("HUB_CI_WEBHOOK_SECRET", secret)) if not v]
    if missing:
        print("set " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        results = reconcile(api, token, url, secret, groups=args.group, projects=args.project,
                            apply=args.apply, insecure=args.insecure)
    except Refused as exc:
        print(f"UNKNOWN: the project list could not be read: {exc}", file=sys.stderr)
        return 3
    for name, state, note in results:
        print(f"{state:9} {name}  {note}")
    counts = {s: sum(1 for _, st, _ in results if st == s) for s in ("ok", "missing", "disabled", "unknown")}
    print("in scope %d: %s" % (len(results), ", ".join(f"{k} {v}" for k, v in counts.items())))
    if counts["unknown"]:
        return 3
    return 1 if counts["missing"] or counts["disabled"] else 0


if __name__ == "__main__":
    sys.exit(main())
