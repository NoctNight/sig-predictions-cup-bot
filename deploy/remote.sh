#!/usr/bin/env bash
# Operate the server bot from the laptop.
#   deploy/remote.sh HOST status     # service state + last HEALTH/cycle lines + DB status
#   deploy/remote.sh HOST logs       # follow the journal (Ctrl-C to stop following; the bot keeps running)
#   deploy/remote.sh HOST dryrun     # one dry cycle on the server (places nothing)
#   deploy/remote.sh HOST start|stop|restart
#   deploy/remote.sh HOST latency    # round-trip time from the server to the venue
#   deploy/remote.sh HOST pull-db    # copy the server's SQLite log back to data/sigbot.server.sqlite for analysis
set -euo pipefail
HOST=${1:?usage: deploy/remote.sh HOST status|logs|dryrun|start|stop|restart|latency|pull-db}
CMD=${2:-status}
case "$CMD" in
  status)
    ssh "$HOST" 'sudo systemctl status sigbot --no-pager | head -6; echo ---;
      sudo journalctl -u sigbot --since "-15 min" --no-pager | grep -E "HEALTH|cycle .* done|WARNING|ERROR|KILL|Traceback" | tail -8 | cut -c1-260; echo ---;
      cd /opt/sigbot && sudo -u sigbot python3 -m sigbot.status --minutes 60' ;;
  logs)    ssh -t "$HOST" 'sudo journalctl -u sigbot -f -n 50' ;;
  dryrun)  ssh "$HOST" 'cd /opt/sigbot && sudo -u sigbot python3 -m sigbot.run --dry-run --once 2>&1 | grep -E "HEALTH|cycle .* done|WARNING|ERROR|Traceback" | cut -c1-260' ;;
  start)   ssh "$HOST" 'sudo systemctl enable sigbot && sudo systemctl start sigbot && sleep 3 && sudo systemctl status sigbot --no-pager | head -5' ;;
  stop)    ssh "$HOST" 'sudo systemctl stop sigbot && sudo systemctl status sigbot --no-pager | head -4' ;;
  restart) ssh "$HOST" 'sudo systemctl restart sigbot && sleep 3 && sudo systemctl status sigbot --no-pager | head -5' ;;
  latency) ssh "$HOST" 'for i in 1 2 3 4 5; do curl -s -o /dev/null -w "connect %{time_connect}s tls %{time_appconnect}s total %{time_total}s\n" https://sig.thesuper.market/api/v1/docs; done; curl -sI https://sig.thesuper.market/api/v1/docs | grep -i x-vercel-id' ;;
  pull-db) ssh "$HOST" 'sudo -u sigbot sqlite3 /opt/sigbot/data/sigbot.sqlite ".backup /tmp/sigbot.snapshot.sqlite" && sudo chmod 644 /tmp/sigbot.snapshot.sqlite'
           scp "$HOST:/tmp/sigbot.snapshot.sqlite" "$(cd "$(dirname "$0")/.." && pwd)/data/sigbot.server.sqlite" && echo "saved data/sigbot.server.sqlite" ;;
  *) echo "unknown command $CMD"; exit 1 ;;
esac
