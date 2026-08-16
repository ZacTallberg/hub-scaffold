#!/usr/bin/env bash
# selftest.sh — isolated end-to-end verification for releases and high-risk boundaries. It is NOT
# the ordinary edit/commit gate; use tools/check.sh for fast development feedback. Five steps:
#   1. agnosticism scrub          (tools/scrub_check.sh — both directions)
#   2. python surfaces compile    (compileall — syntax/import floor, costs a second)
#   3. documentation integrity    (tools/docs_check.py)
#   4. bootstrap doc integrity    (tools/build_bootstrap.py --check)
#   5. init emits explicit identity; example site boots + the write API refuses correctly, proves
#      the CSRF-mint/token-consume launch boundary, then runs the mounted interop/realtime ladder
# There is deliberately NO unit battery: a suite passes whenever the repo is healthy, whether or
# not your change works. Prove a guard by watching it fire on a seeded positive; prove a feature
# against the real example app (step 5 is exactly that, for the write path).
# Prints PASS/FAIL per step and exits nonzero if any step failed. Requires: bash, git, a
# Python executable available as `python` (override with PYTHON=/path/to/python); step 5 additionally needs Django
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
  bash "$ROOT/tools/scrub_check.sh" && bash "$ROOT/tools/scrub_check.sh" --selftest
}

step_compile() {
  cd "$ROOT"
  # Every python surface compiles and imports. The engine's unit battery lived here until
  # 2026-08-08 (upstream ruling: a battery passes whenever the repo is healthy, whether or not
  # the change works - agents prove a guard by watching it fire and prove a feature against the
  # REAL example app in step 4). A syntax error or broken import is the class the battery caught
  # incidentally, and this keeps that class at a second's cost.
  "$PY" -m compileall -q hub_core adapters/django/hub example tools
}

step_docs_check() {
  cd "$ROOT"
  "$PY" tools/docs_check.py
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
  local runtime_root
  runtime_root="$(mktemp -d "$ROOT/.selftest-tmp.XXXXXX")" || return 1
  case "$runtime_root" in
    "$ROOT"/.selftest-tmp.*) ;;
    *) echo "refusing unexpected self-test temp path: $runtime_root" >&2; return 1 ;;
  esac
  SELFTEST_RUNTIME_ROOT="$runtime_root"
  cleanup_runtime() {
    case "${SELFTEST_RUNTIME_ROOT:-}" in
      "$ROOT"/.selftest-tmp.*) rm -rf -- "$SELFTEST_RUNTIME_ROOT" ;;
      *) echo "refusing to clean unexpected self-test temp path: ${SELFTEST_RUNTIME_ROOT:-unset}" >&2 ;;
    esac
  }
  trap cleanup_runtime EXIT
  local stamped="$runtime_root/stamped"
  bash "$ROOT/init.sh" "$stamped" selftest "Self Test" "https://selftest.example.invalid" \
    >/dev/null || return 1
  "$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p, encoding="utf-8")); expected={"key":"selftest","brand":"Self Test","app_name":"selftest","app_host":"https://selftest.example.invalid","worker_scheme":"hub-selftest"}; assert d == expected, d' \
    "$stamped/PROJECT/project.json" || return 1
  cd "$ROOT/example"
  export DEBUG=1
  export HUB_WRITE_TOKEN="selftest-token"
  export HUB_DIR="$runtime_root/hub"
  export HUB_TEST_DB="$runtime_root/example.sqlite3"
  "$PY" manage.py migrate --no-input || return 1
  "$PY" manage.py seedhub || return 1
  "$PY" manage.py hubaudit || return 1
  "$PY" manage.py shell -c "$API_SNIPPET" || return 1
  # The full isolated queue/completion ladder, including negative and race paths.
  "$PY" selftest.py || return 1
}

run_step "scrub (agnosticism gate)"        step_scrub
run_step "python surfaces compile"         step_compile
run_step "documentation integrity"         step_docs_check
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
