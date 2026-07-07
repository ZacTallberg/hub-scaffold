#!/usr/bin/env bash
# selftest.sh — end-to-end proof the scaffold works on a fresh machine. Four steps:
#   1. agnosticism scrub          (tools/scrub_check.sh)
#   2. engine unit tests          (python -m unittest over hub_core — framework-free)
#   3. bootstrap doc integrity    (tools/build_bootstrap.py --check)
#   4. example site boots + the write API refuses correctly (405/403/400 ladder), then the full
#      server-granted-done hardening ladder (example/selftest.py: 403/409/422/422/200)
# Prints PASS/FAIL per step and exits nonzero if any step failed. Requires: bash, git, a
# python3 on PATH (override with PYTHON=/path/to/python); step 4 additionally needs Django
# importable by that python.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python}"

PASSED=()
FAILED=()

run_step() {
  local name="$1"; shift
  echo ""
  echo "==== STEP: $name ===="
  if ( "$@" ); then
    echo "PASS: $name"
    PASSED+=("$name")
  else
    echo "FAIL: $name"
    FAILED+=("$name")
  fi
}

step_scrub() {
  bash "$ROOT/tools/scrub_check.sh"
}

step_unittests() {
  cd "$ROOT"
  "$PY" -m unittest discover -s hub_core -t "$ROOT" -v
}

step_bootstrap_check() {
  cd "$ROOT"
  "$PY" tools/build_bootstrap.py --check
}

# The refusal ladder, exercised in-process with the Django test client. The token below must
# match the HUB_WRITE_TOKEN exported in step_example.
API_SNIPPET='
from django.test import Client
c = Client()
def post(path, body, token=None):
    kw = {"content_type": "application/json"}
    if token:
        kw["HTTP_X_WRITE_TOKEN"] = token
    return c.post(path, data=body, **kw).status_code
checks = [
    ("read is fine (hub page 200)", c.get("/hub/").status_code, 200),
    ("wrong method refused (GET on write API -> 405)", c.get("/hub/api/task").status_code, 405),
    ("missing token refused (403)", post("/hub/api/task", "{}"), 403),
    ("wrong token refused (403)", post("/hub/api/task", "{}", "not-the-token"), 403),
    ("malformed JSON refused after auth (400)", post("/hub/api/task", "{not json", "selftest-token"), 400),
    ("invalid payload refused (400)", post("/hub/api/complete", "{}", "selftest-token"), 400),
]
bad = False
for name, got, want in checks:
    ok = got == want
    bad = bad or not ok
    print(("  ok  " if ok else "  BAD ") + name + "  got=%s want=%s" % (got, want))
raise SystemExit(1 if bad else 0)
'

step_example() {
  if [ ! -f "$ROOT/example/manage.py" ]; then
    echo "example/manage.py not found — the example site is missing"
    return 1
  fi
  cd "$ROOT/example"
  export DEBUG=1
  export HUB_WRITE_TOKEN="selftest-token"
  "$PY" manage.py migrate --no-input || return 1
  "$PY" manage.py seedhub || return 1
  "$PY" manage.py hubaudit || return 1
  "$PY" manage.py shell -c "$API_SNIPPET" || return 1
  # The full server-granted-done hardening ladder: direct done -> 409, missing
  # verification_command -> 422, unresolvable evidence -> 422, real completion -> 200.
  "$PY" selftest.py || return 1
}

run_step "scrub (agnosticism gate)"        step_scrub
run_step "hub_core unit tests"             step_unittests
run_step "bootstrap doc --check"           step_bootstrap_check
run_step "example site + write-API ladder" step_example

echo ""
echo "==== SELFTEST SUMMARY ===="
for s in "${PASSED[@]:-}"; do [ -n "$s" ] && echo "  PASS  $s"; done
for s in "${FAILED[@]:-}"; do [ -n "$s" ] && echo "  FAIL  $s"; done
if [ "${#FAILED[@]}" -ne 0 ]; then
  echo "SELFTEST: FAIL (${#FAILED[@]} step(s) failed)"
  exit 1
fi
echo "SELFTEST: PASS (all ${#PASSED[@]} steps)"
exit 0
