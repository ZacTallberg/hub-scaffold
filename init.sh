#!/usr/bin/env bash
# hub-scaffold init — stamp a new project with the event-sourced hub, the PROJECT/ management
# plane includes the canonical PROJECT/HUB-QUALITY.md excellence contract.
# plane, governance/security files, architecture contract, and enforcement patterns. The only sanctioned way to adopt the
# scaffold — never copy pieces by hand and pivot them.
#
#   bash init.sh <target-dir> <project-key> "<Brand Name>" [live-url]
#
#   target-dir    directory to create (must not exist, or must be an empty dir)
#   project-key   [a-z0-9-] machine key; becomes {{PROJECT_KEY}} everywhere
#   Brand Name    human-facing name; becomes {{BRAND}} everywhere
#   live-url      optional; becomes {{LIVE_URL}} (default: https://<project-key>.example.com)
#
# Optional authored visual inputs are environment variables: HUB_VISUAL_MARK, HUB_ACCENT_H,
# HUB_ACCENT_PAIR_H, HUB_DISPLAY_VOICE, HUB_SURFACE_CHARACTER, and HUB_AMBIENT_MOTIF. When omitted,
# the key deterministically selects a coherent starter so two generated Hubs are not identical.
#
# What it does: copies .gitignore, PROJECT/ (including its explicit project.json identity), hub_core/,
# adapters/, patterns/, campaigns/, OPERATING-AGREEMENT.md,
# SECURITY.md, and docs/ARCHITECTURE.md into
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

# --- authored starter identity ---------------------------------------------------------------
# Bounded vocabularies keep this art direction portable and safe to inject as data/CSS tokens.
KEY_SUM="$(printf '%s' "$KEY" | cksum | awk '{print $1}')"
MARKS=(cube bolt pulse route target rocket branch stack package gauge)
VOICES=(precision editorial kinetic humanist monumental)
SURFACES=(glass paper luminous technical soft)
MOTIFS=(grid constellation orbit waves embers threads petals monolith rings stage)
VISUAL_MARK="${HUB_VISUAL_MARK:-${MARKS[$((KEY_SUM % ${#MARKS[@]}))]}}"
ACCENT_H="${HUB_ACCENT_H:-$((KEY_SUM % 360))}"
ACCENT_PAIR_H="${HUB_ACCENT_PAIR_H:-$(((ACCENT_H + 52 + KEY_SUM % 64) % 360))}"
DISPLAY_VOICE="${HUB_DISPLAY_VOICE:-${VOICES[$((KEY_SUM % ${#VOICES[@]}))]}}"
SURFACE_CHARACTER="${HUB_SURFACE_CHARACTER:-${SURFACES[$((KEY_SUM % ${#SURFACES[@]}))]}}"
AMBIENT_MOTIF="${HUB_AMBIENT_MOTIF:-${MOTIFS[$((KEY_SUM % ${#MOTIFS[@]}))]}}"
[[ " ${MARKS[*]} " == *" $VISUAL_MARK "* ]] || { echo "ERROR: unsupported HUB_VISUAL_MARK: $VISUAL_MARK" >&2; exit 1; }
[[ " ${VOICES[*]} " == *" $DISPLAY_VOICE "* ]] || { echo "ERROR: unsupported HUB_DISPLAY_VOICE: $DISPLAY_VOICE" >&2; exit 1; }
[[ " ${SURFACES[*]} " == *" $SURFACE_CHARACTER "* ]] || { echo "ERROR: unsupported HUB_SURFACE_CHARACTER: $SURFACE_CHARACTER" >&2; exit 1; }
[[ " ${MOTIFS[*]} " == *" $AMBIENT_MOTIF "* ]] || { echo "ERROR: unsupported HUB_AMBIENT_MOTIF: $AMBIENT_MOTIF" >&2; exit 1; }
for HUE_VALUE in "$ACCENT_H" "$ACCENT_PAIR_H"; do
  [[ "$HUE_VALUE" =~ ^[0-9]+$ ]] && [ "$HUE_VALUE" -le 360 ] || {
    echo "ERROR: accent hues must be integers from 0 through 360" >&2; exit 1;
  }
done

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
for t in PROJECT hub_core adapters patterns campaigns; do
  [ -d "$ROOT/$t" ] || { echo "ERROR: scaffold is incomplete, missing $t/" >&2; missing=1; }
done
for f in .gitignore OPERATING-AGREEMENT.md SECURITY.md docs/ARCHITECTURE.md governance/CLAUDE.md.template governance/AGENTS.md.template; do
  [ -f "$ROOT/$f" ] || { echo "ERROR: scaffold is incomplete, missing $f" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1

(cd "$ROOT" && tar \
  --exclude=.git --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.hub' --exclude='*.sqlite3*' \
  -cf - PROJECT hub_core adapters patterns campaigns) | (cd "$TARGET" && tar -xf -)

cp "$ROOT/OPERATING-AGREEMENT.md" "$TARGET/OPERATING-AGREEMENT.md"
cp "$ROOT/SECURITY.md" "$TARGET/SECURITY.md"
cp "$ROOT/.gitignore" "$TARGET/.gitignore"
mkdir -p "$TARGET/docs"
cp "$ROOT/docs/ARCHITECTURE.md" "$TARGET/docs/ARCHITECTURE.md"
# Governance templates land renamed into place at the project root.
cp "$ROOT/governance/CLAUDE.md.template" "$TARGET/CLAUDE.md"
cp "$ROOT/governance/AGENTS.md.template" "$TARGET/AGENTS.md"

# --- substitute placeholders across text files ----------------------------------------------
# sed-escape replacement text (we use | as the sed delimiter): escape \, &, and |.
esc() { printf '%s' "$1" | sed -e 's/[\\&]/\\&/g' -e 's/|/\\|/g'; }
KEY_R="$(esc "$KEY")"; BRAND_R="$(esc "$BRAND")"; URL_R="$(esc "$LIVE_URL")"
MARK_R="$(esc "$VISUAL_MARK")"; ACCENT_R="$(esc "$ACCENT_H")"; PAIR_R="$(esc "$ACCENT_PAIR_H")"
VOICE_R="$(esc "$DISPLAY_VOICE")"; SURFACE_R="$(esc "$SURFACE_CHARACTER")"; MOTIF_R="$(esc "$AMBIENT_MOTIF")"

grep -rIl -e '{{PROJECT_KEY}}' -e '{{BRAND}}' -e '{{LIVE_URL}}' -e '{{VISUAL_MARK}}' \
  -e '{{ACCENT_H}}' -e '{{ACCENT_PAIR_H}}' -e '{{DISPLAY_VOICE}}' \
  -e '{{SURFACE_CHARACTER}}' -e '{{AMBIENT_MOTIF}}' "$TARGET" 2>/dev/null \
  | while IFS= read -r f; do
      sed -i \
        -e "s|{{PROJECT_KEY}}|$KEY_R|g" \
        -e "s|{{BRAND}}|$BRAND_R|g" \
        -e "s|{{LIVE_URL}}|$URL_R|g" \
        -e "s|{{VISUAL_MARK}}|$MARK_R|g" \
        -e "s|{{ACCENT_H}}|$ACCENT_R|g" \
        -e "s|{{ACCENT_PAIR_H}}|$PAIR_R|g" \
        -e "s|{{DISPLAY_VOICE}}|$VOICE_R|g" \
        -e "s|{{SURFACE_CHARACTER}}|$SURFACE_R|g" \
        -e "s|{{AMBIENT_MOTIF}}|$MOTIF_R|g" \
        "$f"
    done

# Placeholder gate: nothing leaves init half-templated (fail-closed).
if LEFT="$(grep -rIln -e '{{PROJECT_KEY}}' -e '{{BRAND}}' -e '{{LIVE_URL}}' -e '{{VISUAL_MARK}}' \
  -e '{{ACCENT_H}}' -e '{{ACCENT_PAIR_H}}' -e '{{DISPLAY_VOICE}}' \
  -e '{{SURFACE_CHARACTER}}' -e '{{AMBIENT_MOTIF}}' "$TARGET" 2>/dev/null)" \
   && [ -n "$LEFT" ]; then
  echo "ERROR: placeholders survived templating in:" >&2
  printf '%s\n' "$LEFT" >&2
  exit 1
fi

# --- git genesis ------------------------------------------------------------------------------
GIT_ID=(-c commit.gpgsign=false)
git -C "$TARGET" init -q -b main
# Identity check must run against the TARGET repo (a local-only user.email in the caller's cwd
# repo would otherwise pass the check here yet be absent when committing in $TARGET).
if ! git -C "$TARGET" config user.email >/dev/null 2>&1; then
  GIT_ID+=(-c user.name="hub-scaffold-init" -c user.email="init@localhost.invalid")
fi
git -C "$TARGET" add -A
git -C "$TARGET" "${GIT_ID[@]}" commit -qm "genesis: $KEY project plane + hub from hub-scaffold"

# --- next steps -------------------------------------------------------------------------------
cat <<EOF

Initialized '$KEY' ($BRAND) at $TARGET — placeholders substituted, git genesis committed.
Portable identity written to PROJECT/project.json (app host: $LIVE_URL; worker: hub-$KEY://).
Art direction: $VISUAL_MARK · hues $ACCENT_H/$ACCENT_PAIR_H · $DISPLAY_VOICE · $SURFACE_CHARACTER · $AMBIENT_MOTIF.

Next steps (the adoption runbook lives in the scaffold README):
  1. Mount the hub in your web project per adapters/django/MOUNTING.md
     (copy adapters/django/hub/ in as an app, add its urls under /hub, NEVER at the front door).
  2. Set a write token and seed the board:
       export HUB_WRITE_TOKEN=<random-secret>
       python manage.py migrate && python manage.py seedhub
     Treat the token as production credentials; read SECURITY.md before distributing it.
  3. Adopt the deploy contract: read patterns/deploy-contract.md for the four laws, then
     patterns/deploy-runbook.md for how to satisfy them WITHOUT a deploy script (an agent
     executes it and reads real output at each step; fill in the binding table once).
     There is deliberately no deploy script and no cron canary here: the runbook IS the deploy
     path, and patterns/standing-canary.md is the by-hand re-check that keeps a blessed record
     honest. Install patterns/pre-receive-gate.sh on your git host — it REFUSES rather than
     performs, which is why it alone stays as code.
  4. Read OPERATING-AGREEMENT.md with your team; it is the human half of the system.

Live URL recorded as: $LIVE_URL
EOF
