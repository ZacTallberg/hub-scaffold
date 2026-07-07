"""Refusal-ladder self-test for the mounted hub write API (run from example/):

    DEBUG=1 HUB_WRITE_TOKEN=selftest-token python selftest.py

Proves the server-granted-done hardening end-to-end via the Django test client:
  no-token -> 403 · direct done -> 409 · complete without verification_command -> 422 ·
  unresolvable evidence -> 422 · claimed + evidenced + server-verified complete -> 200.
Exit 0 only if every rung behaves. Assumes migrate + seedhub have run (selftest.sh does both)."""
import json
import os
import sys
from pathlib import Path

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

TOKEN = os.environ["HUB_WRITE_TOKEN"]
client = Client()
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
    return json.loads(body) if body else {}


print("== hub write-API refusal ladder ==")

# rung 0: writes are fail-closed without the token
rung("no-token-403", post("/hub/api/task", {"title": "x", "agent": "ladder"}, token=None), 403)

# rung 1: the generic upsert can never mint a 'done'
rung("direct-done-409", post("/hub/api/task", {"title": "sneaky done", "status": "done", "agent": "ladder"}), 409)

# rung 2: a task WITHOUT verification_command cannot be completed even with real evidence
r = rung("create-no-vc-200", post("/hub/api/task", {"title": "ladder: no verification_command", "agent": "ladder"}), 200)
tid1 = r["data"]["id"]
lease1 = rung("claim1-200", post("/hub/api/claim", {"id": tid1, "agent": "ladder"}), 200)["token"]
rung("no-vc-422", post("/hub/api/complete", {"id": tid1, "token": lease1, "agent": "ladder",
                                             "accept_note": "trying without vc", "evidence_uri": ["manage.py"]}), 422)

# rung 3: evidence must dereference (URL <400 / repo commit / existing repo path)
r = rung("create-real-200", post("/hub/api/task", {"title": "ladder: real completion", "agent": "ladder",
                                                   "verification_command": "python -c \"print('verified')\""}), 200)
tid2 = r["data"]["id"]
lease2 = rung("claim2-200", post("/hub/api/claim", {"id": tid2, "agent": "ladder"}), 200)["token"]
rung("fake-evidence-422", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder",
                                                     "accept_note": "fake", "evidence_uri": ["no-such-file-xyz.txt"]}), 422)

# rung 4: claimed + dereferencing evidence + server-run verification_command + sound audit -> done
rung("real-complete-200", post("/hub/api/complete", {"id": tid2, "token": lease2, "agent": "ladder",
                                                     "accept_note": "server ran the verification_command",
                                                     "evidence_uri": ["manage.py"]}), 200)

# confirm the transition landed
state = json.loads(client.get("/hub/task.json").content)
done = [t["id"] for t in state["data"] if t.get("status") == "done"]
print("done tasks in snapshot:", done)
if tid2 not in done:
    failures.append("snapshot-missing-done")

print("LADDER:", "ALL RUNGS PASS" if not failures else ("FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
