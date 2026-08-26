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
# The guard is about the CHROME: no token-entry UI, no unlock affordance. The #hub-data
# JSON island is data, and it legitimately carries the operational error stream — which
# includes recorded auth refusals that NAME the token header. Searching the whole page
# would make the board unable to report an auth refusal without failing its own selftest,
# so the island is excised before the check.
import re as _re
page_text = _re.sub(r'<script id="hub-data".*?</script>', "",
                    page.content.decode("utf-8"), flags=_re.S).lower()
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


def receipt(cmd, exit_code=0, ran_by="ladder"):
    """A typed verification_run receipt. The WORKER runs the command and reports what happened;
    the hub validates the receipt and never executes the string itself (RCE ruling)."""
    import hashlib
    return {"command": cmd, "exit_code": exit_code,
            "output_sha256": hashlib.sha256(b"ladder").hexdigest(), "ran_by": ran_by}

# rung 4: a present command must actually pass (it is not a presence-only checkbox)
r = rung("create-failing-vc-200", post("/hub/api/task", {
    "title": "ladder: failing verification command", "agent": "ladder",
    "verification_command": "python -c \"import sys; sys.exit(7)\""
}), 200)
tid3 = r["data"]["id"]
lease3 = rung("claim3-200", post("/hub/api/claim", {"id": tid3, "agent": "ladder"}), 200)["token"]
failed = rung("failing-vc-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder", "accept_note": "must fail",
    "evidence_uri": ["manage.py"],
    "verification_run": receipt("python -c \"import sys; sys.exit(7)\"", exit_code=7)
}), 422)
if ((failed.get("errors") or [{}])[0].get("code")) != "bad_verification_run":
    failures.append("failing-vc-error-code")

# rung 4b: NO receipt at all is refused — the hub will not run the command on your behalf, so a
# completion with nothing to validate is a claim, not a proof.
noproof = rung("no-receipt-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder", "accept_note": "no proof",
    "evidence_uri": ["manage.py"]}), 422)
if ((noproof.get("errors") or [{}])[0].get("code")) != "need_verification_run":
    failures.append("no-receipt-error-code")

# rung 4c: a receipt for a DIFFERENT command cannot be borrowed into place.
borrowed = rung("borrowed-receipt-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder", "accept_note": "borrowed",
    "evidence_uri": ["manage.py"],
    "verification_run": receipt("python -c \"print('something else entirely')\"")}), 422)
if ((borrowed.get("errors") or [{}])[0].get("code")) != "bad_verification_run":
    failures.append("borrowed-receipt-error-code")

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
        "evidence_uri": ["manage.py"],
        # A VALID receipt, so this rung reaches the AUDIT gate rather than stopping at the
        # receipt gate: an unsound hub must refuse even a completion whose proof is perfect.
        "verification_run": receipt("python -c \"print('verified')\"")
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
        "verification_run": receipt("python -c \"print('verified')\""),
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
        "verification_run": receipt("python -c \"print('verified')\""),
        "id": tid6, "token": lease6, "agent": "ladder", "accept_note": "must lose ownership",
        "evidence_uri": ["manage.py"]
    }), 409)
if ((expired_complete.get("errors") or [{}])[0].get("code")) != "lease":
    failures.append("completion-lease-race-code")

# rung 7: claimed + dereferencing evidence + server-run verification_command + sound audit -> done
rung("real-complete-200", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder",
                                                     "accept_note": "worker ran it out-of-band and submitted the receipt",
                                                     "evidence_uri": ["manage.py"],
                                                     "verification_run": receipt("python -c \"print('verified')\"")}), 200)
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

print("== the secret-shape refusal at the write choke point ==")
# A pasted credential must be refused at the door: the ledger is append-only, so a secret
# that lands can never be removed. A redaction PLACEHOLDER must pass — refusing what a
# member types after being told to redact makes the error message a dead end.
rung("secret-shaped-422", post("/hub/api/note", {
    "title": "ladder: failing command output",
    "body_md": "it dies with DB_PASSWORD=hunter2secret99 in the log", "agent": "ladder"}), 422)
rung("redacted-placeholder-200", post("/hub/api/note", {
    "title": "ladder: redacted command output",
    "body_md": "it dies with DB_PASSWORD=[REDACTED] in the log", "agent": "ladder"}), 200)

print("== ask -> deliver -> answer -> ack: the loop closes ==")
OPERATOR = hub_app.operator_agent()
askr = rung("ask-200", post("/hub/api/ask", {
    "agent": "asker-one",
    "question": "how do I reach the staging database endpoint from the worker seat",
    "context": "the connection refuses from my machine; works from the host"}), 200)
ask_id = askr["data"]["id"]
rung("ask-needs-agent-422", post("/hub/api/ask", {"question": "who am I"}), 422)
dup = rung("ask-duplicate-409", post("/hub/api/ask", {
    "agent": "asker-two",
    "question": "how can I reach the staging database endpoint from a worker seat"}), 409)
if not (dup.get("errors") or [{}])[0].get("matches"):
    failures.append("ask-duplicate-missing-matches")
rung("ask-anyway-200", post("/hub/api/ask", {
    "agent": "asker-two", "anyway": True,
    "question": "how can I reach the staging database endpoint from a worker seat"}), 200)
inbox_op = json.loads(client.get("/hub/inbox.json?agent=" + OPERATOR).content)["data"]
if not any(i.get("id") == ask_id for i in inbox_op.get("items", [])):
    failures.append("ask-not-addressed-to-operator")
# The long-poll answers immediately when the fingerprint differs, and reports an honest
# wait when it matches (wait=0 keeps the ladder fast).
w = json.loads(client.get("/hub/inbox/wait?agent=" + OPERATOR + "&fp=stale&wait=0").content)["data"]
if w.get("fingerprint") == "stale" or w.get("degraded"):
    failures.append("inbox-wait-immediate-change")
rung("answer-200", post("/hub/api/answer", {
    "question": ask_id,
    "text": "use the service name, not the raw address — the worker seat resolves it"}), 200)
qs = json.loads(client.get("/hub/questions.json").content)
qrow = next((r for r in qs["data"] if r["id"] == ask_id), None)
if not qrow or qrow.get("open") or not qrow.get("answered"):
    failures.append("answer-left-question-open")
inbox_asker = json.loads(client.get("/hub/inbox.json?agent=asker-one").content)["data"]
answer_item = next((i for i in inbox_asker.get("items", []) if i.get("kind") == "answer"), None)
if not answer_item:
    failures.append("answer-not-addressed-to-asker")
else:
    rung("ack-200", post("/hub/api/ack", {
        "agent": "asker-one", "directive": answer_item["id"]}), 200)
    dirs = json.loads(client.get("/hub/directive.json").content)["data"]
    drow = next((d for d in dirs if d["id"] == answer_item["id"]), None)
    # Every named target acked -> the directive retires itself; a delivered queue that
    # never leaves "active" is a queue people learn to ignore.
    if not drow or drow.get("status") != "fulfilled":
        failures.append("full-ack-did-not-retire-directive")
    inbox_after = json.loads(client.get("/hub/inbox.json?agent=asker-one").content)["data"]
    if any(i.get("id") == answer_item["id"] for i in inbox_after.get("items", [])):
        failures.append("acked-item-still-addressed")
rung("directive-all-200", post("/hub/api/directive", {
    "title": "ladder: standing instruction", "body_md": "hold the line", "targets": ["all"]}), 200)

print("== identity: minted tokens bind, mismatches refuse, whoami tells the truth ==")
mint = rung("mint-token-200", post("/hub/api/mint-token", {"target": "worker-seven"}), 200)
minted_token = (mint.get("data") or {}).get("token") or ""
rung("minted-cannot-mint-403", post("/hub/api/mint-token", {"target": "worker-eight"},
                                    token=minted_token), 403)
rung("minted-cannot-answer-403", post("/hub/api/answer", {"question": ask_id, "text": "no"},
                                      token=minted_token), 403)
rung("minted-mismatch-422", post("/hub/api/note", {
    "title": "ladder: impersonation attempt", "agent": "somebody-else"}, token=minted_token), 422)
attributed = rung("minted-attributed-200", post("/hub/api/note", {
    "title": "ladder: attributed by the credential"}, token=minted_token), 200)
ent = json.loads(client.get("/hub/note/" + attributed["data"]["id"].rsplit(":", 1)[-1] + ".json").content)["data"]
if (ent.get("provenance") or {}).get("agent") != "worker-seven":
    failures.append("minted-write-not-attributed")
who = json.loads(client.get("/hub/whoami.json", HTTP_X_WRITE_TOKEN=minted_token).content)["data"]
if who.get("token_class") != "minted" or who.get("bound_agent") != "worker-seven" \
        or not who.get("identity_is_proven"):
    failures.append("whoami-minted-class")
who_g = json.loads(client.get("/hub/whoami.json", HTTP_X_WRITE_TOKEN=TOKEN).content)["data"]
if who_g.get("token_class") != "general" or who_g.get("identity_is_proven"):
    failures.append("whoami-general-class")
rung("revoked-token-403", post("/hub/api/note", {"title": "after revoke"}, token="x"), 403)
rung("mint-revoke-200", post("/hub/api/mint-token", {"target": "worker-seven", "revoke": True}), 200)
rung("minted-after-revoke-403", post("/hub/api/note", {
    "title": "ladder: revoked credential"}, token=minted_token), 403)

print("== presence: observed seats, live consoles, abandoned claims ==")
rung("presence-ping-200", post("/hub/api/presence", {"agent": "seat-one"},
                               ), 200)
resp = client.post("/hub/api/presence", data=json.dumps({"agent": "seat-two"}),
                   content_type="application/json", HTTP_X_WRITE_TOKEN=TOKEN,
                   HTTP_X_HUB_MACHINE="box-a", HTTP_X_HUB_SESSION="sess1234",
                   HTTP_X_HUB_CWD="services/api", HTTP_X_HUB_FOCUS="wiring the export job")
rung("presence-ping-headers-200", resp, 200)
snap = json.loads(client.get("/hub/hub.json").content)
sessions = (snap.get("live") or {}).get("sessions_live") or []
if not any(s.get("agent") == "seat-two" and s.get("focus") == "wiring the export job"
           for s in sessions):
    failures.append("live-console-not-on-board")
fleet_cards = (snap.get("live") or {}).get("fleet") or []
if not any(c.get("agent") == "seat-two" and c.get("status") == "active" for c in fleet_cards):
    failures.append("focused-console-reads-idle")

print("== the operational error stream: ingest, bar, ack truthfulness, bounded clear ==")
e1 = rung("app-error-201", post("/hub/api/app-error", {
    "app": "billing", "kind": "server", "message": "nightly export crashed",
    "severity": "critical", "details": "Traceback ... ValueError"}), 201)
fp = e1["data"]["fingerprint"]
rung("agent-error-201", post("/hub/api/agent-error", {
    "agent": "seat-one", "source": "launcher", "message": "worker wrapper exited 1"}), 201)
errs = json.loads(client.get("/hub/errors.json").content)
if not any(r.get("fingerprint") == fp for r in errs["data"]):
    failures.append("app-error-not-on-bar")
if errs["metadata"].get("coverage") is None:
    failures.append("errors-missing-coverage")
# a browser transport blip is recorded but held BELOW the bar
csrf2 = csrf_client.cookies["csrftoken"].value
blip = csrf_client.post("/hub/api/client-error",
                        data=json.dumps({"source": "sync", "message": "Failed to fetch"}),
                        content_type="application/json", HTTP_X_CSRFTOKEN=csrf2)
rung("client-error-201", blip, 201)
deferred = json.loads(client.get("/hub/errors.json?include=deferred").content)["data"]
blip_row = next((r for r in deferred if str(r.get("source", "")).startswith("browser.sync")), None)
if not blip_row or blip_row.get("bar") != "deferred":
    failures.append("browser-blip-cleared-the-bar")
rung("reopen-unacked-409", post("/hub/api/ack-error", {"fingerprint": fp, "reopen": True}), 409)
rung("ack-error-201", post("/hub/api/ack-error", {"fingerprint": fp, "agent": "ladder",
                                                  "note": "fixed in the export job"}), 201)
rung("reopen-acked-200", post("/hub/api/ack-error", {"fingerprint": fp, "reopen": True}), 200)
rung("clear-unbounded-400", post("/hub/api/clear-errors", {}), 400)
rung("clear-only-acked-200", post("/hub/api/clear-errors", {"only_acked": True}), 200)

print("== board search, plan checkpoint notes, attention rail ==")
hits = json.loads(client.get("/hub/search.json?q=queue lease truth").content)["data"]
if not any(h.get("id") == qid for h in hits):
    failures.append("search-missed-known-task")
cur = hub_app.current_state()["entities"][qid]
rung("plan-note-200", post("/hub/api/task", {
    "id": qid, "agent": "queue-rescuer", "expected_version": cur["version"],
    "plan": [{"step": "diagnose", "done": True, "note": "root cause found in the export path"},
             {"step": "fix", "done": False}]}), 200)
snap2 = json.loads(client.get("/hub/hub.json").content)
attn_kinds = {a.get("kind") for a in (snap2.get("live") or {}).get("attention") or []}
if "open-question" not in attn_kinds:
    failures.append("open-question-not-on-attention-rail")
inflight_rows = (snap2.get("live") or {}).get("inflight") or []
if not any(r.get("last_note") for r in inflight_rows):
    failures.append("checkpoint-note-not-on-live-row")

print("LADDER:", "ALL RUNGS PASS" if not failures else ("FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
