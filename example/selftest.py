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
os.environ.setdefault("PROJECT_IDENTITY_FILE", str(BASE_DIR / "PROJECT" / "project.json"))
os.environ.setdefault(
    "AGENT_CARD_KEY_FILE",
    str(Path(os.environ.get("HUB_DIR") or BASE_DIR / "PROJECT" / ".hub") / "agent-card-es256.pem"),
)
os.chdir(BASE_DIR)

import django

django.setup()

from django.test import Client
from hub import agent_card, hub_api, hub_app, hub_write
from hub_core import identity, upcast

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
    try:
        body = resp.content.decode("utf-8")
    except AttributeError:
        body = "<streaming response>"
    ok = resp.status_code == want
    print("%s [%s] want %s got %s  %s" % ("PASS" if ok else "FAIL", tag, want, resp.status_code, body[:220]))
    if not ok:
        failures.append(tag)
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


def check(tag, condition, detail=""):
    ok = bool(condition)
    print("%s [%s]%s" % ("PASS" if ok else "FAIL", tag,
                          ("  " + str(detail)[:220]) if detail else ""))
    if not ok:
        failures.append(tag)
    return ok


def rpc(method, params=None, rid=1):
    response = post("/hub/api/mcp", {
        "jsonrpc": "2.0", "id": rid, "method": method, "params": params or {},
    })
    return rung("mcp-%s-http-200" % method.replace("/", "-"), response, 200)


def rpc_tool(name, arguments, rid=1):
    outer = rpc("tools/call", {"name": name, "arguments": arguments}, rid=rid)
    result = outer.get("result") or {}
    try:
        inner = json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        inner = {}
        failures.append("mcp-%s-tool-envelope" % name)
    return result, inner


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

# rung 2: strict-mode discovery and direct claim agree that an unspecced task is not executable
r = rung("create-no-vc-200", post("/hub/api/task", {"title": "ladder: no verification_command", "agent": "ladder"}), 200)
tid1 = r["data"]["id"]
needs_spec = rung("claim-needs-spec-409", post(
    "/hub/api/claim", {"id": tid1, "agent": "ladder-needs-spec"}), 409)
if ((needs_spec.get("errors") or [{}])[0].get("code")) != "needs_spec":
    failures.append("claim-needs-spec-error-code")

# rung 3: evidence must dereference (URL <400 / repo commit / existing repo path)
r = rung("create-real-200", post("/hub/api/task", {"title": "ladder: real completion", "agent": "ladder",
                                                   "verification_command": "python -c \"print('verified')\""}), 200)
tid2 = r["data"]["id"]
lease2 = rung("claim2-200", post("/hub/api/claim", {"id": tid2, "agent": "ladder-real"}), 200)["token"]
empty = rung("empty-evidence-422", post("/hub/api/complete", {
    "id": tid2, "token": lease2, "agent": "ladder-real", "accept_note": "not enough",
    "evidence_uri": [""]
}), 422)
if ((empty.get("errors") or [{}])[0].get("code")) != "need_evidence":
    failures.append("empty-evidence-error-code")
rung("fake-evidence-422", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder-real",
                                                     "accept_note": "fake", "evidence_uri": ["no-such-file-xyz.txt"]}), 422)


def receipt(cmd, exit_code=0, ran_by="ladder-real"):
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
lease3 = rung("claim3-200", post("/hub/api/claim", {"id": tid3, "agent": "ladder-fail"}), 200)["token"]
failed = rung("failing-vc-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder-fail", "accept_note": "must fail",
    "evidence_uri": ["manage.py"],
    "verification_run": receipt("python -c \"import sys; sys.exit(7)\"", exit_code=7, ran_by="ladder-fail")
}), 422)
if ((failed.get("errors") or [{}])[0].get("code")) != "bad_verification_run":
    failures.append("failing-vc-error-code")

# rung 4b: NO receipt at all is refused — the hub will not run the command on your behalf, so a
# completion with nothing to validate is a claim, not a proof.
noproof = rung("no-receipt-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder-fail", "accept_note": "no proof",
    "evidence_uri": ["manage.py"]}), 422)
if ((noproof.get("errors") or [{}])[0].get("code")) != "need_verification_run":
    failures.append("no-receipt-error-code")

# rung 4c: a receipt for a DIFFERENT command cannot be borrowed into place.
borrowed = rung("borrowed-receipt-422", post("/hub/api/complete", {
    "id": tid3, "token": lease3, "agent": "ladder-fail", "accept_note": "borrowed",
    "evidence_uri": ["manage.py"],
    "verification_run": receipt("python -c \"print('something else entirely')\"", ran_by="ladder-fail")}), 422)
if ((borrowed.get("errors") or [{}])[0].get("code")) != "bad_verification_run":
    failures.append("borrowed-receipt-error-code")

# rung 5: a critical computed audit result blocks completion even when every other input is valid
r = rung("create-audit-blocked-200", post("/hub/api/task", {
    "title": "ladder: critical audit refusal", "agent": "ladder",
    "verification_command": "python -c \"print('verified')\""
}), 200)
tid4 = r["data"]["id"]
lease4 = rung("claim4-200", post("/hub/api/claim", {"id": tid4, "agent": "ladder-audit"}), 200)["token"]
critical = {"violations": [{"id": "synthetic:critical", "severity": "critical",
                             "observed": "seeded self-test violation"}]}
with mock.patch("hub.hub_app.run_audit", return_value=critical):
    blocked = rung("critical-audit-422", post("/hub/api/complete", {
        "id": tid4, "token": lease4, "agent": "ladder-audit", "accept_note": "must block",
        "evidence_uri": ["manage.py"],
        # A VALID receipt, so this rung reaches the AUDIT gate rather than stopping at the
        # receipt gate: an unsound hub must refuse even a completion whose proof is perfect.
        "verification_run": receipt("python -c \"print('verified')\"", ran_by="ladder-audit")
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
    "/hub/api/claim", {"id": tid5, "agent": "ladder-version"}), 200)["token"]


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
        "verification_run": receipt("python -c \"print('verified')\"", ran_by="ladder-version"),
        "id": tid5, "token": lease5, "agent": "ladder-version", "accept_note": "must conflict",
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
    "/hub/api/claim", {"id": tid6, "agent": "ladder-lease"}), 200)["token"]


def expire_during_completion():
    lease = hub_app._read_lease(tid6)
    lease["expires"] = 0
    hub_app._write_lease(tid6, lease)
    return {"violations": []}


with mock.patch("hub.hub_app.run_audit", side_effect=expire_during_completion):
    expired_complete = rung("completion-lease-race-409", post("/hub/api/complete", {
        "verification_run": receipt("python -c \"print('verified')\"", ran_by="ladder-lease"),
        "id": tid6, "token": lease6, "agent": "ladder-lease", "accept_note": "must lose ownership",
        "evidence_uri": ["manage.py"]
    }), 409)
if ((expired_complete.get("errors") or [{}])[0].get("code")) != "lease":
    failures.append("completion-lease-race-code")

# rung 7: claimed + dereferencing evidence + worker-produced exit-0 receipt + sound audit -> done
rung("real-complete-200", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder-real",
                                                     "accept_note": "worker ran it out-of-band and submitted the receipt",
                                                     "evidence_uri": ["manage.py"],
                                                     "verification_run": receipt("python -c \"print('verified')\"", ran_by="ladder-real")}), 200)
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


print("== portable identity + truthful agent discovery ==")
ident = identity.load()
expected_ident = {
    "key": "example", "brand": "Example", "app_name": "example",
    "app_host": "https://example.invalid", "worker_scheme": "hub-example",
}
check("identity-five-fields", {k: ident.get(k) for k in expected_ident} == expected_ident, ident)
check("identity-drives-hub", hub_app.PROJECT_KEY == identity.key() == "example")
check("identity-drives-brand", hub_app.BRAND == identity.brand() == "Example")
check("identity-drives-worker-scheme",
      hub_app.worker_protocol() == identity.worker_scheme() == "hub-example")
check("identity-drives-receipt-predicate",
      upcast.receipt_predicate_type() == "https://example.invalid/verification_run/v1")

card_response = client.get("/.well-known/agent-card.json")
card = rung("agent-discovery-200", card_response, 200)
card_raw = card_response.content.decode("utf-8")
check("agent-discovery-identity", card.get("name") == "example-hub-worker")
check("agent-discovery-no-a2a-transport",
      card.get("supportedInterfaces") == []
      and card.get("capabilities", {}).get("streaming") is False
      and "preferredTransport" not in card and "url" not in card)
protocols = ((card.get("x-hub") or {}).get("callableProtocols") or [])
check("agent-discovery-real-mcp-only",
      (card.get("x-hub") or {}).get("discoveryOnly") is True
      and len(protocols) == 1
      and protocols[0].get("name") == "MCP"
      and protocols[0].get("url") == "https://example.invalid/hub/api/mcp")
expected_kinds = json.loads((BASE_DIR / "PROJECT" / "schema" / "task.schema.json")
                            .read_text(encoding="utf-8"))["properties"]["work_kind"]["enum"]
card_kinds = [skill.get("id", "").removeprefix("work_kind.") for skill in card.get("skills", [])]
check("agent-discovery-skills-from-schema", card_kinds == expected_kinds)
check("agent-discovery-auth-name-not-value",
      "X-Write-Token" in card_raw and TOKEN not in card_raw)

# Verify the JWS when signing support exists; an unsigned host must state why it is unsigned.
if card.get("signatures"):
    try:
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        def b64ud(value):
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        signature = card["signatures"][0]
        protected = json.loads(b64ud(signature["protected"]))
        jwk = protected["jwk"]
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(b64ud(jwk["x"]), "big"),
            int.from_bytes(b64ud(jwk["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
        raw_sig = b64ud(signature["signature"])
        der_sig = utils.encode_dss_signature(
            int.from_bytes(raw_sig[:32], "big"), int.from_bytes(raw_sig[32:], "big"))
        unsigned = {k: v for k, v in card.items() if k != "signatures"}
        payload = base64.urlsafe_b64encode(agent_card.canonical(unsigned)).rstrip(b"=").decode("ascii")
        public.verify(der_sig, (signature["protected"] + "." + payload).encode("ascii"),
                      ec.ECDSA(hashes.SHA256()))
        check("agent-discovery-signature-valid", protected.get("alg") == "ES256")
    except Exception as exc:
        check("agent-discovery-signature-valid", False, type(exc).__name__)
else:
    check("agent-discovery-unsigned-is-explicit", bool(card.get("signatureStatus")))


print("== MCP lifecycle + realtime read contract ==")
initialized = rpc("initialize", rid=100).get("result") or {}
check("mcp-initialize-identity",
      initialized.get("protocolVersion") == "2026-07-28"
      and (initialized.get("serverInfo") or {}).get("name") == "example-hub-board")
check("mcp-initialize-tasks-extension",
      "io.modelcontextprotocol/tasks" in (initialized.get("capabilities", {}).get("extensions") or {}))
tool_list = (rpc("tools/list", rid=101).get("result") or {}).get("tools") or []
check("mcp-tool-list",
      [tool.get("name") for tool in tool_list]
      == ["board_next", "spec_task", "start_task", "finish_task"])
check("mcp-unknown-method-32601", (rpc("no/such/method", rid=102).get("error") or {}).get("code") == -32601)
check("mcp-unknown-tool-32602",
      (rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, rid=103)
       .get("error") or {}).get("code") == -32602)
check("mcp-missing-args-32602",
      (rpc("tools/call", {"name": "start_task", "arguments": {}}, rid=104)
       .get("error") or {}).get("code") == -32602)

cursor_response = client.get("/hub/cursor.json")
cursor0 = rung("cursor-200", cursor_response, 200)
check("cursor-shape", set(cursor0) == {"seq", "hash", "ts"})
check("cursor-no-content-canary", TOKEN not in cursor_response.content.decode("utf-8"))
snapshot0_response = client.get("/hub/hub.json")
rung("hub-etag-initial-200", snapshot0_response, 200)
etag0 = snapshot0_response.get("ETag")
check("hub-etag-present", bool(etag0))
unchanged0 = client.get("/hub/hub.json", HTTP_IF_NONE_MATCH=etag0)
rung("hub-etag-unchanged-304", unchanged0, 304)
check("hub-etag-304-empty", unchanged0.content == b"")

mcp_command = "python -c \"print('mcp verified')\""
created_mcp = rung("create-mcp-task-200", post("/hub/api/task", {
    "title": "realtime-payload-canary-never-stream-this", "agent": "mcp-agent",
    "work_kind": "product", "acceptance": "MCP starts and finishes this task exactly once.",
    "verification_command": mcp_command,
}), 200)
mcp_task = created_mcp["data"]["id"]
note_event = rung("create-sse-note-200", post("/hub/api/note", {
    "title": "sse framing companion", "body_md": "payload must never ride the event stream",
    "status": "standing", "agent": "realtime-test",
}), 200)
note_id = note_event["data"]["id"]

changed_response = client.get("/hub/hub.json", HTTP_IF_NONE_MATCH=etag0)
rung("hub-etag-ledger-change-200", changed_response, 200)
etag1 = changed_response.get("ETag")
check("hub-etag-ledger-change-new", bool(etag1 and etag1 != etag0))
delta_response = client.get("/hub/delta.json?since=%s" % cursor0["seq"])
delta = rung("delta-after-create-200", delta_response, 200)
changed_ids = [row.get("id") for row in delta.get("changed", [])]
check("delta-dedupes-newest-aggregate",
      changed_ids.count(mcp_task) == 1 and changed_ids.count(note_id) == 1)
check("delta-cockpit-present",
      all(key in (delta.get("live") or {}) for key in
          ("cursor", "progress", "fleet", "inflight", "attention", "readiness", "activity", "dag")))
delta_empty = rung("delta-at-head-200", client.get(
    "/hub/delta.json?since=%s" % delta["cursor"]["seq"]), 200)
check("delta-at-head-empty", delta_empty.get("changed") == [])

# Last-Event-ID wins over the query cursor. A patched monotonic clock lets the bounded stream emit
# ready + both pending events + reconnect without an actual 52-second wait.
with mock.patch("hub.hub_api.time.monotonic", side_effect=[0, 0, 1, 1, 53]):
    sse_response = client.get(
        "/hub/live/events?since=%s" % delta["cursor"]["seq"],
        HTTP_LAST_EVENT_ID=str(cursor0["seq"]),
    )
    sse_raw = b"".join(sse_response.streaming_content).decode("utf-8")
    sse_response.close()
rung("sse-http-200", sse_response, 200)
check("sse-headers",
      sse_response.get("Content-Type", "").startswith("text/event-stream")
      and sse_response.get("Cache-Control") == "no-cache, no-store, must-revalidate"
      and sse_response.get("X-Accel-Buffering") == "no")
sse_frames = [frame for frame in sse_raw.split("\n\n") if frame]
hub_frames = [frame for frame in sse_frames if "event: hub\n" in frame]
check("sse-frame-order",
      sse_frames[0] == "retry: 1500"
      and ("id: %s\nevent: ready" % cursor0["seq"]) in sse_frames[1]
      and len(hub_frames) == 2
      and "event: reconnect" in sse_frames[-1])
for index, frame in enumerate(hub_frames):
    payload_line = next((line for line in frame.splitlines() if line.startswith("data: ")), "data: {}")
    envelope = json.loads(payload_line[6:])
    check("sse-envelope-%s" % index,
          set(envelope) == {"seq", "ts", "event", "aggregate", "version", "agent"})
check("sse-no-payload-content",
      "realtime-payload-canary-never-stream-this" not in sse_raw
      and "payload must never ride" not in sse_raw and TOKEN not in sse_raw)

start_result, start_inner = rpc_tool("start_task", {"id": mcp_task, "agent": "mcp-agent"}, rid=110)
lease_token = (start_inner.get("body") or {}).get("token")
check("mcp-start-success",
      start_result.get("isError") is False and start_inner.get("status") == 200
      and bool(lease_token) and (start_inner.get("body") or {}).get("version") == 2)
mcp_ent = hub_app.current_state()["entities"].get(mcp_task) or {}
check("mcp-start-sole-transition",
      mcp_ent.get("status") == "in_progress" and mcp_ent.get("version") == 2
      and "planning_state" not in mcp_ent)
working = rpc("tasks/get", {"taskId": mcp_task}, rid=111).get("result") or {}
check("mcp-tasks-get-working", (working.get("task") or {}).get("status") == "working")

retry_cursor = client.get("/hub/cursor.json").json()
_retry_result, retry_inner = rpc_tool(
    "start_task", {"id": mcp_task, "agent": "mcp-agent"}, rid=112)
check("mcp-start-idempotent",
      (retry_inner.get("body") or {}).get("token") == lease_token
      and client.get("/hub/cursor.json").json()["seq"] == retry_cursor["seq"])
other_result, other_inner = rpc_tool(
    "start_task", {"id": mcp_task, "agent": "other-agent"}, rid=113)
check("mcp-start-fenced",
      other_result.get("isError") is True and other_inner.get("status") == 409)

# Heartbeat changes only the lease sidecar. The representation validator must still change.
lease_snapshot = client.get("/hub/hub.json")
rung("hub-etag-before-heartbeat-200", lease_snapshot, 200)
lease_etag = lease_snapshot.get("ETag")
rung("mcp-lease-heartbeat-200", post(
    "/hub/api/heartbeat", {"id": mcp_task, "token": lease_token, "ttl_s": 4321}), 200)
after_heartbeat = client.get("/hub/hub.json", HTTP_IF_NONE_MATCH=lease_etag)
rung("hub-etag-heartbeat-change-200", after_heartbeat, 200)
heartbeat_etag = after_heartbeat.get("ETag")
check("hub-etag-heartbeat-change-new", bool(heartbeat_etag and heartbeat_etag != lease_etag))
rung("hub-etag-after-heartbeat-304",
     client.get("/hub/hub.json", HTTP_IF_NONE_MATCH=heartbeat_etag), 304)

_bad_result, bad_inner = rpc_tool("finish_task", {
    "id": mcp_task, "agent": "mcp-agent", "lease_token": lease_token,
    "note": "bad receipt must be refused", "evidence": ["manage.py"],
    "verification_run": receipt(mcp_command, exit_code=7, ran_by="mcp-agent"),
}, rid=114)
check("mcp-finish-bad-receipt",
      bad_inner.get("status") == 422
      and ((bad_inner.get("body") or {}).get("errors") or [{}])[0].get("code")
      == "bad_verification_run"
      and hub_app.lease_valid(mcp_task, lease_token))
finish_result, finish_inner = rpc_tool("finish_task", {
    "id": mcp_task, "agent": "mcp-agent", "lease_token": lease_token,
    "note": "MCP lifecycle completed with the worker-produced receipt",
    "evidence": ["manage.py"],
    "verification_run": receipt(mcp_command, ran_by="mcp-agent"),
}, rid=115)
check("mcp-finish-success",
      finish_result.get("isError") is False and finish_inner.get("status") == 200)
finished_ent = hub_app.current_state()["entities"].get(mcp_task) or {}
check("mcp-finish-projected",
      finished_ent.get("status") == "done" and finished_ent.get("version") == 3
      and finished_ent.get("verification_run") and hub_app._read_lease(mcp_task) is None)
completed = rpc("tasks/get", {"taskId": mcp_task}, rid=116).get("result") or {}
check("mcp-tasks-get-completed", (completed.get("task") or {}).get("status") == "completed")

# EventStore caps each events_after query at 500. The delta endpoint must page rather than return a
# head cursor while permanently omitting aggregate 501.
burst_cursor = client.get("/hub/cursor.json").json()
burst_store = hub_app.store()
try:
    for index in range(501):
        burst_store.append(
            aggregate="example:note:delta-burst-%03d" % index,
            type="note.created",
            payload={"type": "note", "title": "delta burst %03d" % index,
                     "status": "standing"},
            expected_version=None,
            agent_id="delta-burst",
        )
finally:
    burst_store.close()
burst_delta = rung("delta-501-page-200", client.get(
    "/hub/delta.json?since=%s" % burst_cursor["seq"]), 200)
burst_rows = [row for row in burst_delta.get("changed", [])
              if str(row.get("id", "")).startswith("example:note:delta-burst-")]
check("delta-501-complete",
      len(burst_rows) == 501
      and any(row.get("id") == "example:note:delta-burst-500" for row in burst_rows)
      and burst_delta.get("cursor") == {
          k: client.get("/hub/cursor.json").json()[k] for k in ("seq", "hash")})

print("LADDER:", "ALL RUNGS PASS" if not failures else ("FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
