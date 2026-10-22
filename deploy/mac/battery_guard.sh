#!/bin/bash
# Stop the bot cleanly (cancels every resting order) before the battery dies: a Mac that dies or hibernates mid-trading
# leaves its quotes resting on the venue. Checks every 60 s; exits after stopping the bot or once on AC power for 10 min.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIMIT=${1:-7}
ac=0
while true; do
  b=$(pmset -g batt)
  pct=$(echo "$b" | grep -oE '[0-9]+%' | head -1 | tr -d '%')
  if echo "$b" | grep -q "AC Power"; then ac=$((ac+1)); [ $ac -ge 10 ] && { echo "$(date '+%F %T') battery guard: on AC power, exiting"; exit 0; }; else ac=0; fi
  if [ -n "$pct" ] && [ "$pct" -le "$LIMIT" ] && ! echo "$b" | grep -q "AC Power"; then
    echo "$(date '+%F %T') battery guard: ${pct}% <= ${LIMIT}%, stopping the bot (orders cancelled)"
    "$ROOT/deploy/mac/botctl.sh" stop
    exit 0
  fi
  sleep 60
done
