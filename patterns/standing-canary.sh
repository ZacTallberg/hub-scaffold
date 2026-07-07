#!/usr/bin/env bash
# standing-canary.sh — out-of-band deploy re-checker (Law 4 of deploy-contract.md).
#
# Runs from cron OUTSIDE any deploying agent's process — ideally on a machine that never deploys.
# For every blessed record ($BLESSED_DIR/<project>, one line: "<sha> <url>") it asserts the LIVE
# page still serves <meta name="build" content="build-<sha>">. Mismatch or unreachable fires
# $ALERT_CMD, with a per-project cooldown so a real outage is one alert, not a storm. An agent can
# claim anything; this script only believes the live page.
#
# Install (typical):
#   cp standing-canary.sh /usr/local/bin/standing-canary.sh && chmod +x /usr/local/bin/standing-canary.sh
#   echo '*/15 * * * * root BLESSED_DIR=/var/lib/deploy-blessed ALERT_CMD=/usr/local/bin/deploy-alert /usr/local/bin/standing-canary.sh' > /etc/cron.d/standing-canary
# Blessed records are written by deploy.sh on every VERIFIED deploy, so coverage grows for free.
#
# Config (environment):
#   BLESSED_DIR     dir of "<sha> <url>" records          (default: /var/lib/deploy-blessed)
#   STATE_DIR       last-result + cooldown state          (default: /var/lib/standing-canary)
#   ALERT_CMD       command invoked as: $ALERT_CMD "<title>" "<message>"; wrap your org's channel
#                   (chat webhook, pager, mail) in a tiny script. Unset = log-only.
#   COOLDOWN_SECS   min seconds between repeat alerts per project (default: 21600 = 6h)
#   CURL_UA         probe user-agent (default below; some edges filter obvious bot UAs)
set -u
BLESSED_DIR="${BLESSED_DIR:-/var/lib/deploy-blessed}"
STATE_DIR="${STATE_DIR:-/var/lib/standing-canary}"
ALERT_CMD="${ALERT_CMD:-}"
COOLDOWN_SECS="${COOLDOWN_SECS:-21600}"
CURL_UA="${CURL_UA:-Mozilla/5.0 (standing-canary)}"

mkdir -p "$STATE_DIR"
now=$(date +%s)

for f in "$BLESSED_DIR"/*; do
  [ -f "$f" ] || continue
  project=$(basename "$f")
  read -r sha url < "$f" || continue
  { [ -n "$sha" ] && [ -n "$url" ]; } || continue

  body=$(curl -sL -A "$CURL_UA" --max-time 45 "$url" 2>/dev/null)

  if printf '%s' "$body" | grep -qi "name=\"build\" content=\"build-$sha\""; then
    rm -f "$STATE_DIR/$project.alerted"
    echo "$now OK $project $sha" > "$STATE_DIR/$project.last"
    continue
  fi

  served=$(printf '%s' "$body" | grep -oi 'name="build" content="build-[^"]*"' | head -1)
  msg="STANDING CANARY: $project live page != blessed build-$sha (served: ${served:-none-or-unreachable}) url=$url"
  echo "$now FAIL $project $msg" > "$STATE_DIR/$project.last"

  last=0
  [ -f "$STATE_DIR/$project.alerted" ] && last=$(cat "$STATE_DIR/$project.alerted" 2>/dev/null || echo 0)
  if [ $((now - last)) -ge "$COOLDOWN_SECS" ]; then
    echo "$now" > "$STATE_DIR/$project.alerted"
    if [ -n "$ALERT_CMD" ]; then
      "$ALERT_CMD" "Standing canary FAIL: $project" "$msg" || echo "WARN: ALERT_CMD failed for $project" >&2
    else
      echo "$msg" >&2
    fi
  fi
done
exit 0
