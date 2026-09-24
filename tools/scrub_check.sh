#!/usr/bin/env bash
# scrub_check.sh — the agnosticism gate. The scaffold ships to a different machine/company, so
# it must contain ZERO origin-specific residue: no source-org names, hosts, IPs, or absolute
# source paths -- and no stray control bytes (a mangled escape). Greps the whole scaffold tree for the forbidden list below.
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
  '\bember\b'
  '\bpinnacle\b'
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
  'C:(\\+|/)code'
  '\bplots?\b'
)

combined="$(IFS='|'; echo "${PATTERNS[*]}")"

if [ -n "${1:-}" ]; then
  echo "usage: bash tools/scrub_check.sh" >&2
  exit 2
fi

out="$(git grep --untracked --exclude-standard -nIEi \
        -e "$combined" -- . \
        ':(exclude)tools/scrub_check.sh' \
        ':(exclude)**/node_modules/**' \
        ':(exclude)**/staticfiles/**' 2>/dev/null || true)"

# Control bytes in a TEXT file are almost never intended: they are what a heredoc or an escaped
# string leaves behind when a backslash escape is interpreted instead of written (backspace 0x08,
# vertical tab 0x0b, form feed 0x0c, SUB 0x1a, ESC 0x1b). They survive review -- most viewers
# render them as nothing -- and silently change a regex, a path or a literal.
ctl="$(git grep --untracked --exclude-standard -nIP '[\x08\x0B\x0C\x1A\x1B]' -- . \
        ':(exclude)**/node_modules/**' ':(exclude)**/staticfiles/**' 2>/dev/null \
        | cut -c1-160 || true)"

status=0
if [ -n "$out" ]; then
  printf '%s\n' "$out" | sed 's/^/  /'
  echo "SCRUB: FAIL — origin-specific residue found (see above). Rewrite generically; keep the lesson, lose the specifics."
  status=2
fi
if [ -n "$ctl" ]; then
  printf '%s\n' "$ctl" | cat -v | sed 's/^/  /'
  echo "SCRUB: FAIL — control bytes in text files (shown as ^H ^K ^L ^Z ^[). Usually a mangled escape: write the escape, not the byte."
  status=2
fi
[ "$status" -ne 0 ] && exit "$status"
echo "SCRUB: CLEAN — no forbidden terms, IPs, absolute source paths, or stray control bytes found."
exit 0
