#!/usr/bin/env bash
# Move live trading from this laptop to the server, with no moment where two bots run.
#   deploy/cutover.sh HOST
# Steps: refuse if a local bot is still running -> dry cycle on the server -> cancel-all and verify an empty book
#        -> push code + DB snapshot -> start the systemd service -> show the first cycle.
set -euo pipefail
HOST=${1:?usage: deploy/cutover.sh HOST}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if pgrep -f "sigbot.run --live" >/dev/null; then
  echo "A local live bot is still running (pid $(pgrep -f 'sigbot.run --live' | tr '\n' ' ')). Stop it first (Ctrl-C in its terminal tab), then rerun."
  exit 1
fi
if ssh "$HOST" 'systemctl is-active --quiet sigbot'; then
  echo "The server bot is already running; nothing to cut over."
  exit 1
fi

echo "== 1/5 push code (no DB yet) and dry-run on the server"
deploy/push.sh "$HOST"
deploy/remote.sh "$HOST" dryrun

echo "== 2/5 cancel all resting orders and verify the book is empty"
python3 deploy/cancel_all.py

echo "== 3/5 push the database snapshot"
deploy/push.sh "$HOST" --with-db

echo "== 4/5 start the service"
# marker: from now on the bot lives on the server; the laptop watchdog checks the server and never starts a local bot
echo "$HOST" > deploy/SERVER_HOST
deploy/remote.sh "$HOST" start

echo "== 5/5 first cycle (waiting up to 120 s)"
for i in $(seq 1 24); do
  if ssh "$HOST" 'sudo journalctl -u sigbot --since "-3 min" --no-pager | grep -q "cycle .* done"'; then break; fi
  sleep 5
done
deploy/remote.sh "$HOST" status
echo "Cutover done. The laptop no longer trades; manage the bot with deploy/remote.sh $HOST status|logs|stop|start."
