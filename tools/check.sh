#!/usr/bin/env bash
# Fast, impact-aware sanity checks for ordinary development. This is deliberately not a release
# proof. Use --all-fast in ordinary CI; use selftest.sh only at a meaningful verification boundary.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python}"
MODE="${1:-}"

if [ -n "$MODE" ] && [ "$MODE" != "--all-fast" ]; then
  echo "usage: bash tools/check.sh [--all-fast]" >&2
  exit 2
fi

cd "$ROOT"

mapfile -d '' STATUS_ROWS < <(git status --porcelain=v1 -z --untracked-files=all)
CHANGED=()
for row in "${STATUS_ROWS[@]:-}"; do
  # Porcelain -z rename/copy records carry a second path-only record; accept only status records.
  if [ "${row:2:1}" = " " ]; then
    CHANGED+=("${row:3}")
  fi
done

has_match() {
  local pattern="$1" file
  [ "$MODE" = "--all-fast" ] && return 0
  for file in "${CHANGED[@]:-}"; do
    [[ "$file" =~ $pattern ]] && return 0
  done
  return 1
}

echo "FAST CHECK: agnosticism scrub"
bash tools/scrub_check.sh

if has_match '\.py$'; then
  echo "FAST CHECK: Python syntax"
  "$PY" -m compileall -q hub_core adapters/django/hub example
fi

if [ "$MODE" = "--all-fast" ]; then
  echo "FAST CHECK: framework-free unit tests"
  "$PY" -m unittest discover -s hub_core -t "$ROOT"
fi

if has_match '(\.md$|\.template$|^PROJECT/schema/|^example/PROJECT/schema/|^tools/docs_check\.py$)'; then
  echo "FAST CHECK: documentation links and schema mirror"
  "$PY" tools/docs_check.py
fi

if has_match '(^PROJECT/|^PROJECT-PLANE-BOOTSTRAP\.md$|^tools/build_bootstrap\.py$)'; then
  echo "FAST CHECK: generated bootstrap parity"
  "$PY" tools/build_bootstrap.py --check
fi

if has_match '\.sh$'; then
  echo "FAST CHECK: changed shell syntax"
  SHELL_FILES=("${CHANGED[@]:-}")
  if [ "$MODE" = "--all-fast" ]; then
    mapfile -d '' SHELL_FILES < <(git ls-files -z '*.sh')
  fi
  for file in "${SHELL_FILES[@]:-}"; do
    if [[ "$file" =~ \.sh$ ]] && [ -f "$file" ]; then
      bash -n "$file"
    fi
  done
fi

if [ "$MODE" != "--all-fast" ] && [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "FAST CHECK: no pending changes; nothing impact-selected"
fi

echo "FAST CHECK: PASS (sanity only; no release/integration claim)"
