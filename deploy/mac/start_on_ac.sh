#!/bin/bash
# Wait until the Mac is on AC power (checked every 15 s, for up to 4 hours), then start the bot and arm the battery guard.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
for i in $(seq 1 960); do
  if pmset -g batt | grep -q "AC Power"; then
    echo "$(date '+%F %T') on AC power: starting the bot"
    "$ROOT/deploy/mac/botctl.sh" start
    nohup "$ROOT/deploy/mac/battery_guard.sh" 7 >> "$ROOT/data/battery_guard.log" 2>&1 &
    exit 0
  fi
  sleep 15
done
echo "$(date '+%F %T') gave up waiting for AC power"
