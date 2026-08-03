#!/usr/bin/env bash
# scrub_check.sh — the agnosticism gate. The scaffold ships to a different machine/company, so
# it must contain ZERO origin-specific residue: no source-org names, hosts, IPs, or absolute
# source paths. Greps the whole scaffold tree for the forbidden list below.
#   bash tools/scrub_check.sh          exit 0 = clean, exit 2 = hits (each one listed)
# This script is the single place the forbidden strings may legally appear, so it excludes
# itself (by filename) from the scan.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Forbidden patterns (grep -E, case-insensitive). Word-boundaries on short/ambiguous tokens so
# ordinary English does not false-positive; bare substrings for distinctive ones.
PATTERNS=(
  'zacoberg'
  'zcobe'
  'everyopenmic'
  'openmic'
  'fairy'
  'aether'
  '\bloom\b'
  '\bblobs\b'
  'homebase'
  'greenhouse'
  '\blens\b'
  'dokku'
  '\bnas\b'
  'celeron'
  'creds\.local'
  'ntfy'
  '192\.168\.'
  '143\.198\.'
  '147\.182\.'
  '\b64\.23\.'
  '/c/code'
  'C:\\code'
  'C:/code'
  '\bplots?\b'
)

combined="$(IFS='|'; echo "${PATTERNS[*]}")"

if [ "${1:-}" = "--selftest" ]; then
  printf '%s\n' 'loom' '192.168.10.20' | grep -Eiq -e "$combined" || {
    echo "SCRUB SELFTEST: FAIL — seeded forbidden text was not detected" >&2
    exit 1
  }
  if printf '%s\n' 'bloom' 'ordinary portable text' | grep -Eiq -e "$combined"; then
    echo "SCRUB SELFTEST: FAIL — boundary-safe text was rejected" >&2
    exit 1
  fi
  echo "SCRUB SELFTEST: PASS — seeded violation detected; boundary-safe text accepted"
  exit 0
fi

if [ -n "${1:-}" ]; then
  echo "usage: bash tools/scrub_check.sh [--selftest]" >&2
  exit 2
fi

out="$(git grep --untracked --exclude-standard -nIEi \
        -e "$combined" -- . \
        ':(exclude)tools/scrub_check.sh' \
        ':(exclude)**/node_modules/**' \
        ':(exclude)**/staticfiles/**' 2>/dev/null || true)"

if [ -n "$out" ]; then
  printf '%s\n' "$out" | sed 's/^/  /'
  echo "SCRUB: FAIL — origin-specific residue found (see above). Rewrite generically; keep the lesson, lose the specifics."
  exit 2
fi
echo "SCRUB: CLEAN — no forbidden terms, IPs, or absolute source paths found."
exit 0
