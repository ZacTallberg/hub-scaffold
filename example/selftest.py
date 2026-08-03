"""Refusal-ladder self-test for the mounted hub write API (run from example/):

    DEBUG=1 HUB_WRITE_TOKEN=selftest-token python selftest.py

Proves the queue and server-granted-done hardening end-to-end via the Django test client:
  auth + launch boundary · queue claim/renew/expiry/reclaim · completion evidence/command/audit
  refusals · version/lease race fencing · successful server-verified completion.
Exit 0 only if every rung behaves. Assumes migrate + seedhub have run (selftest.sh does both)."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent
SCAFFOLD = BASE_DIR.parent
for _p in (str(BASE_DIR), str(SCAFFOLD), str(SCAFFOLD / "adapters" / "django")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example_site.settings")
os.environ.setdefault("DEBUG", "1")
os.environ.setdefault("HUB_WRITE_TOKEN", "selftest-token")
os.chdir(BASE_DIR)

import django

django.setup()

from django.test import Client
from hub import hub_app, hub_write

TOKEN = os.environ["HUB_WRITE_TOKEN"]
client = Client()
csrf_client = Client(enforce_csrf_checks=True)
failures = []


def post(path, body, token=TOKEN):
    kwargs = {"content_type": "application/json"}
    if token:
        kwargs["HTTP_X_WRITE_TOKEN"] = token
    return client.post(path, data=json.dumps(body), **kwargs)


def rung(tag, resp, want):
    body = resp.content.decode("utf-8")
    ok = resp.status_code == want
    print("%s [%s] want %s got %s  %s" % ("PASS" if ok else "FAIL", tag, want, resp.status_code, body[:220]))
    if not ok:
        failures.append(tag)
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


print("== hub write-API refusal ladder ==")

# rung 0: writes are fail-closed without the token
rung("no-token-403", post("/hub/api/task", {"title": "x", "agent": "ladder"}, token=None), 403)

# The browser gets exactly one narrow, CSRF-gated capability. It never needs the general write
# token; the separate authoritative consume endpoint still does.
page = csrf_client.get("/hub/")
rung("hub-page-csrf-200", page, 200)
page_text = page.content.decode("utf-8").lower()
if "x-write-token" in page_text or "unlock" in page_text:
    failures.append("browser-write-token-ui-present")
csrf = csrf_client.cookies["csrftoken"].value
rung("launch-mint-no-csrf-403", csrf_client.post(
    "/hub/api/launch-grant", data=json.dumps({"action": "start", "count": 1}),
    content_type="application/json"), 403)
minted = rung("launch-mint-csrf-no-write-token-200", csrf_client.post(
    "/hub/api/launch-grant", data=json.dumps({"action": "start", "count": 1}),
    content_type="application/json", HTTP_X_CSRFTOKEN=csrf), 200)
grant = minted["data"]["grant"]
rung("launch-consume-no-write-token-403", post(
    "/hub/api/launch-grant/consume", {"consume": grant, "action": "start", "count": 1}, token=None), 403)
rung("launch-consume-write-token-200", post(
    "/hub/api/launch-grant/consume", {"consume": grant, "action": "start", "count": 1}), 200)
rung("launch-replay-refused-403", post(
    "/hub/api/launch-grant/consume", {"consume": grant, "action": "start", "count": 1}), 403)

# rung 1: the generic upsert can never mint a 'done'
rung("direct-done-409", post("/hub/api/task", {"title": "sneaky done", "status": "done", "agent": "ladder"}), 409)

# Queue integrity: only real, available tasks can be claimed. A claim transitions projected state,
# hides live work from discovery, is idempotent for its owner, and is reclaimable after expiry.
rung("claim-missing-404", post("/hub/api/claim", {"id": "example:task:not-real", "agent": "ladder"}), 404)
rung("claim-bad-agent-400", post("/hub/api/claim", {"id": "example:task:not-real", "agent": {}}), 400)
rung("claim-bad-ttl-422", post(
    "/hub/api/claim", {"id": "example:task:not-real", "agent": "ladder", "ttl_s": 0}), 422)
q = rung("create-queue-task-200", post("/hub/api/task", {
    "title": "ladder: queue lease truth", "agent": "ladder",
    "verification_command": "python -c \"print('verified')\""
}), 200)
qid = q["data"]["id"]
before = json.loads(client.get("/hub/next.json?n=50").content)["data"]
if qid not in [row["id"] for row in before]:
    failures.append("queue-missing-before-claim")
qclaim = rung("queue-claim-200", post(
    "/hub/api/claim", {"id": qid, "agent": "queue-owner", "ttl_s": 60}), 200)
qtoken = qclaim["token"]
if qclaim.get("version") != 2:
    failures.append("claim-missing-transition-version")
tasks = json.loads(client.get("/hub/task.json").content)["data"]
if next(t for t in tasks if t["id"] == qid).get("status") != "in_progress":
    failures.append("claim-missing-in-progress-transition")
retry = rung("queue-same-owner-retry-200", post(
    "/hub/api/claim", {"id": qid, "agent": "queue-owner", "ttl_s": 60}), 200)
if retry.get("token") != qtoken:
    failures.append("claim-retry-rotated-token")
rung("queue-other-owner-held-409", post(
    "/hub/api/claim", {"id": qid, "agent": "queue-contender", "ttl_s": 60}), 409)
rung("heartbeat-bad-ttl-422", post(
    "/hub/api/heartbeat", {"id": qid, "token": qtoken, "ttl_s": 0}), 422)
active = json.loads(client.get("/hub/next.json?n=50").content)["data"]
if qid in [row["id"] for row in active]:
    failures.append("live-lease-still-offered")
expired = hub_app._read_lease(qid)
expired["expires"] = 0
hub_app._write_lease(qid, expired)
rung("expired-heartbeat-409", post(
    "/hub/api/heartbeat", {"id": qid, "token": qtoken, "ttl_s": 60}), 409)
stale = json.loads(client.get("/hub/next.json?n=50").content)["data"]
stale_row = next((row for row in stale if row["id"] == qid), None)
if not stale_row or not stale_row.get("stale_reclaim"):
    failures.append("expired-in-progress-not-reoffered")
reclaim = rung("expired-reclaim-200", post(
    "/hub/api/claim", {"id": qid, "agent": "queue-rescuer", "ttl_s": 60}), 200)
if reclaim.get("version") != 2 or reclaim.get("token") == qtoken:
    failures.append("expired-reclaim-fencing")

# rung 2: a task WITHOUT verification_command cannot be completed even with real evidence
r = rung("create-no-vc-200", post("/hub/api/task", {"title": "ladder: no verification_command", "agent": "ladder"}), 200)
tid1 = r["data"]["id"]
lease1 = rung("claim1-200", post("/hub/api/claim", {"id": tid1, "agent": "ladder"}), 200)["token"]
empty = rung("empty-evidence-422", post("/hub/api/complete", {
    "id": tid1, "token": lease1, "agent": "ladder", "accept_note": "not enough",
    "evidence_uri": [""]
}), 422)
if ((empty.get("errors") or [{}])[0].get("code")) != "need_evidence":
    failures.append("empty-evidence-error-code")
rung("no-vc-422", post("/hub/api/complete", {"id": tid1, "token": lease1, "agent": "ladder",
                                             "accept_note": "trying without vc", "evidence_uri": ["manage.py"]}), 422)

# rung 3: evidence must dereference (URL <400 / repo commit / existing repo path)
r = rung("create-real-200", post("/hub/api/task", {"title": "ladder: real completion", "agent": "ladder",
                                                   "verification_command": "python -c \"print('verified')\""}), 200)
tid2 = r["data"]["id"]
lease2 = rung("claim2-200", post("/hub/api/claim", {"id": tid2, "agent": "ladder"}), 200)["token"]
rung("fake-evidence-422", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder",
                                                     "accept_note": "fake", "evidence_uri": ["no-such-file-xyz.txt"]}), 422)

# rung 4: a present command must actually pass (it is not a presence-only checkbox)
r = rung("create-failing-vc-200", post("/hub/api/task", {
    "title": "ladder: failing verification command", "agent": "ladder",
    "verification_command": "python -c \"import sys; sys.exit(7)\""
}), 200)
tid3 = r["data"]["id"]
lease3 = rung("claim3-200", post("/hub/api/claim", {"id": tid3, "agent": "ladder"}), 200)["token"]
failed = rung("failing-vc-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder", "accept_note": "must fail",
    "evidence_uri": ["manage.py"]
}), 422)
if ((failed.get("errors") or [{}])[0].get("code")) != "verify_failed":
    failures.append("failing-vc-error-code")

# rung 5: a critical computed audit result blocks completion even when every other input is valid
r = rung("create-audit-blocked-200", post("/hub/api/task", {
    "title": "ladder: critical audit refusal", "agent": "ladder",
    "verification_command": "python -c \"print('verified')\""
}), 200)
tid4 = r["data"]["id"]
lease4 = rung("claim4-200", post("/hub/api/claim", {"id": tid4, "agent": "ladder"}), 200)["token"]
critical = {"violations": [{"id": "synthetic:critical", "severity": "critical",
                             "observed": "seeded self-test violation"}]}
with mock.patch("hub.hub_app.run_audit", return_value=critical):
    blocked = rung("critical-audit-422", post("/hub/api/complete", {
        "id": tid4, "token": lease4, "agent": "ladder", "accept_note": "must block",
        "evidence_uri": ["manage.py"]
    }), 422)
if ((blocked.get("errors") or [{}])[0].get("code")) != "audit_unsound":
    failures.append("critical-audit-error-code")

# rung 6: completion is fenced against a task edit or lease expiry that occurs after verification
r = rung("create-version-race-200", post("/hub/api/task", {
    "title": "ladder: completion version fence", "agent": "ladder",
    "verification_command": "python -c \"print('verified')\""
}), 200)
tid5 = r["data"]["id"]
lease5 = rung("claim-version-race-200", post(
    "/hub/api/claim", {"id": tid5, "agent": "ladder"}), 200)["token"]


def mutate_during_completion():
    current = hub_app.current_state()["entities"][tid5]
    _changed, changed_status = hub_write._append(
        "task", tid5, {"type": "task", "title": "ladder: changed during completion"},
        expected_version=current["version"], agent="racer", idem=None, etype="task.updated",
    )
    if changed_status != 200:
        failures.append("version-race-fixture-update")
    return {"violations": []}


with mock.patch("hub.hub_app.run_audit", side_effect=mutate_during_completion):
    raced = rung("completion-version-race-409", post("/hub/api/complete", {
        "id": tid5, "token": lease5, "agent": "ladder", "accept_note": "must conflict",
        "evidence_uri": ["manage.py"]
    }), 409)
if ((raced.get("errors") or [{}])[0].get("code")) != "conflict":
    failures.append("completion-version-race-code")

r = rung("create-lease-race-200", post("/hub/api/task", {
    "title": "ladder: completion lease fence", "agent": "ladder",
    "verification_command": "python -c \"print('verified')\""
}), 200)
tid6 = r["data"]["id"]
lease6 = rung("claim-lease-race-200", post(
    "/hub/api/claim", {"id": tid6, "agent": "ladder"}), 200)["token"]


def expire_during_completion():
    lease = hub_app._read_lease(tid6)
    lease["expires"] = 0
    hub_app._write_lease(tid6, lease)
    return {"violations": []}


with mock.patch("hub.hub_app.run_audit", side_effect=expire_during_completion):
    expired_complete = rung("completion-lease-race-409", post("/hub/api/complete", {
        "id": tid6, "token": lease6, "agent": "ladder", "accept_note": "must lose ownership",
        "evidence_uri": ["manage.py"]
    }), 409)
if ((expired_complete.get("errors") or [{}])[0].get("code")) != "lease":
    failures.append("completion-lease-race-code")

# rung 7: claimed + dereferencing evidence + server-run verification_command + sound audit -> done
rung("real-complete-200", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder",
                                                     "accept_note": "server ran the verification_command",
                                                     "evidence_uri": ["manage.py"]}), 200)
if hub_app._read_lease(tid2) is not None:
    failures.append("successful-completion-left-lease")
rung("done-task-not-claimable-409", post(
    "/hub/api/claim", {"id": tid2, "agent": "late-worker"}), 409)

# confirm the transition landed
state = json.loads(client.get("/hub/task.json").content)
done = [t["id"] for t in state["data"] if t.get("status") == "done"]
print("done tasks in snapshot:", done)
if tid2 not in done:
    failures.append("snapshot-missing-done")

print("LADDER:", "ALL RUNGS PASS" if not failures else ("FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
