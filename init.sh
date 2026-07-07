#!/usr/bin/env bash
# hub-scaffold init — stamp a new project with the event-sourced hub, the PROJECT/ management
# plane, governance files, and the enforcement patterns. The only sanctioned way to adopt the
# scaffold — never copy pieces by hand and pivot them.
#
#   bash init.sh <target-dir> <project-key> "<Brand Name>" [live-url]
#
#   target-dir    directory to create (must not exist, or must be an empty dir)
#   project-key   [a-z0-9-] machine key; becomes {{PROJECT_KEY}} everywhere
#   Brand Name    human-facing name; becomes {{BRAND}} everywhere
#   live-url      optional; becomes {{LIVE_URL}} (default: https://<project-key>.example.com)
#
# What it does: copies PROJECT/, hub_core/, adapters/, patterns/, OPERATING-AGREEMENT.md into
# the target, renames governance templates into place (CLAUDE.md, AGENTS.md), substitutes the
# three placeholders across all text files (fail-closed if any survive), then git init -b main
# with a genesis commit. Safe to run from any cwd.
set -euo pipefail

usage() {
  echo 'usage: bash init.sh <target-dir> <project-key> "<Brand Name>" [live-url]' >&2
  exit 2
}

TARGET_ARG="${1:-}"; KEY="${2:-}"; BRAND="${3:-}"; LIVE_URL="${4:-}"
[ -n "$TARGET_ARG" ] && [ -n "$KEY" ] && [ -n "$BRAND" ] || usage

case "$KEY" in
  *[!a-z0-9-]*|-*|*-)
    echo "ERROR: project-key must be [a-z0-9-] with no leading/trailing dash (got: $KEY)" >&2
    exit 1;;
esac
[ -n "$LIVE_URL" ] || LIVE_URL="https://$KEY.example.com"

# Scaffold root = the directory this script lives in (works from any cwd).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Refuse an existing non-empty target (a file, or a dir with anything in it).
if [ -e "$TARGET_ARG" ]; then
  if [ ! -d "$TARGET_ARG" ] || [ -n "$(ls -A "$TARGET_ARG" 2>/dev/null)" ]; then
    echo "ERROR: target exists and is not an empty directory: $TARGET_ARG" >&2
    exit 1
  fi
fi
mkdir -p "$TARGET_ARG"
TARGET="$(cd "$TARGET_ARG" && pwd)"

# --- copy the scaffold content -------------------------------------------------------------
missing=0
for t in PROJECT hub_core adapters patterns; do
  [ -d "$ROOT/$t" ] || { echo "ERROR: scaffold is incomplete, missing $t/" >&2; missing=1; }
done
for f in OPERATING-AGREEMENT.md governance/CLAUDE.md.template governance/AGENTS.md.template; do
  [ -f "$ROOT/$f" ] || { echo "ERROR: scaffold is incomplete, missing $f" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1

(cd "$ROOT" && tar \
  --exclude=.git --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.hub' --exclude='*.sqlite3*' \
  -cf - PROJECT hub_core adapters patterns) | (cd "$TARGET" && tar -xf -)

cp "$ROOT/OPERATING-AGREEMENT.md" "$TARGET/OPERATING-AGREEMENT.md"
# Governance templates land renamed into place at the project root.
cp "$ROOT/governance/CLAUDE.md.template" "$TARGET/CLAUDE.md"
cp "$ROOT/governance/AGENTS.md.template" "$TARGET/AGENTS.md"

# --- substitute placeholders across text files ----------------------------------------------
# sed-escape replacement text (we use | as the sed delimiter): escape \, &, and |.
esc() { printf '%s' "$1" | sed -e 's/[\\&]/\\&/g' -e 's/|/\\|/g'; }
KEY_R="$(esc "$KEY")"; BRAND_R="$(esc "$BRAND")"; URL_R="$(esc "$LIVE_URL")"

grep -rIl -e '{{PROJECT_KEY}}' -e '{{BRAND}}' -e '{{LIVE_URL}}' "$TARGET" 2>/dev/null \
  | while IFS= read -r f; do
      sed -i \
        -e "s|{{PROJECT_KEY}}|$KEY_R|g" \
        -e "s|{{BRAND}}|$BRAND_R|g" \
        -e "s|{{LIVE_URL}}|$URL_R|g" \
        "$f"
    done

# Placeholder gate: nothing leaves init half-templated (fail-closed).
if LEFT="$(grep -rIln -e '{{PROJECT_KEY}}' -e '{{BRAND}}' -e '{{LIVE_URL}}' "$TARGET" 2>/dev/null)" \
   && [ -n "$LEFT" ]; then
  echo "ERROR: placeholders survived templating in:" >&2
  printf '%s\n' "$LEFT" >&2
  exit 1
fi

# --- git genesis ------------------------------------------------------------------------------
GIT_ID=()
git -C "$TARGET" init -q -b main
# Identity check must run against the TARGET repo (a local-only user.email in the caller's cwd
# repo would otherwise pass the check here yet be absent when committing in $TARGET).
if ! git -C "$TARGET" config user.email >/dev/null 2>&1; then
  GIT_ID=(-c user.name="hub-scaffold-init" -c user.email="init@localhost.invalid")
fi
git -C "$TARGET" add -A
git -C "$TARGET" "${GIT_ID[@]}" commit -qm "genesis: $KEY project plane + hub from hub-scaffold"

# --- next steps -------------------------------------------------------------------------------
cat <<EOF

Initialized '$KEY' ($BRAND) at $TARGET — placeholders substituted, git genesis committed.

Next steps (the 10-minute runbook lives in the scaffold README):
  1. Mount the hub in your web project per adapters/django/MOUNTING.md
     (copy adapters/django/hub/ in as an app, add its urls under /hub, NEVER at the front door).
  2. Set a write token and seed the board:
       export HUB_WRITE_TOKEN=<random-secret>
       python manage.py migrate && python manage.py seedhub
  3. Prove the gate works, then wire it into CI/pre-deploy:
       python manage.py hubaudit
  4. Adopt the deploy contract: read patterns/deploy-contract.md, start from
     patterns/deploy.sh.example, and install patterns/pre-receive-gate.sh on your git host.
     patterns/standing-canary.sh needs your org's alerting hooked in before it is real.
  5. Read OPERATING-AGREEMENT.md with your team; it is the human half of the system.

Live URL recorded as: $LIVE_URL
EOF
