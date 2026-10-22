#!/usr/bin/env bash
# Copy the bot to the server. Run LOCALLY from the project folder.
#   deploy/push.sh HOST            # code, config, params, keys and reference data (safe while the server bot runs)
#   deploy/push.sh HOST --with-db  # also the SQLite log (only when NO bot is running anywhere: used by cutover.sh)
# HOST is anything ssh accepts (an alias in ~/.ssh/config, or user@ip). The remote user needs sudo for rsync/chown.
set -euo pipefail
HOST=${1:?usage: deploy/push.sh HOST [--with-db]}
WITH_DB=${2:-}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

python3 -m py_compile sigbot/*.py deploy/cancel_all.py

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/data"
rsync -a --exclude '__pycache__' --exclude 'SERVER_HOST' sigbot deploy params config.json README.md "$STAGE/"
# reference data the bot reads at startup (seed bars, volume cache, universe, history, maps)
for f in markets.json candles_5m.json volume_cache.json volume_cache.json.vol race_map.json; do
  [ -f "data/$f" ] && cp "data/$f" "$STAGE/data/"
done
[ -d data/history ] && cp -R data/history "$STAGE/data/"
# keys (never printed)
for k in .sig_api_key .anthropic_api_key; do
  [ -f "$k" ] && cp "$k" "$STAGE/$k"
done
if [ "$WITH_DB" = "--with-db" ]; then
  # consistent snapshot even if something still has the file open
  sqlite3 data/sigbot.sqlite ".backup '$STAGE/data/sigbot.sqlite'"
  echo "including database snapshot ($(du -h "$STAGE/data/sigbot.sqlite" | cut -f1))"
fi

ssh "$HOST" 'mkdir -p /tmp/sigbot-push'
rsync -az --delete "$STAGE/" "$HOST:/tmp/sigbot-push/"
ssh "$HOST" 'sudo rsync -a /tmp/sigbot-push/ /opt/sigbot/ \
  && sudo chown -R sigbot:sigbot /opt/sigbot \
  && sudo chmod 600 /opt/sigbot/.sig_api_key /opt/sigbot/.anthropic_api_key 2>/dev/null; \
  sudo install -m 644 /opt/sigbot/deploy/sigbot.service /etc/systemd/system/sigbot.service \
  && sudo systemctl daemon-reload && rm -rf /tmp/sigbot-push && echo "pushed to /opt/sigbot"'
