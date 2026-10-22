#!/bin/bash
# Run the live bot as a macOS launchd agent, independent of the Claude app and of any terminal.
#   deploy/mac/botctl.sh install   write ~/Library/LaunchAgents/com.sizhe.sigbot.plist and start the bot
#   deploy/mac/botctl.sh start|stop|restart|status|logs|uninstall
# stop: launchd sends SIGTERM; the bot cancels all resting orders on the way out, then this script checks the book.
# The agent restarts the bot if it crashes (not after a clean stop), at most once every 30 s, and starts it at login.
set -euo pipefail
LABEL=com.sizhe.sigbot
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"
DOMAIN="gui/$(id -u)"

write_plist() {
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>-m</string><string>sigbot.run</string><string>--live</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StandardOutPath</key><string>$ROOT/data/live.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/live.log</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ExitTimeOut</key><integer>90</integer>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PL
}
running() { ps -eo command | grep "[s]igbot.run --live" | grep -vc caffeinate || true; }
case "${1:-status}" in
  install)
    if [ "$(running)" != "0" ]; then echo "a live bot is already running outside launchd: stop it first"; exit 1; fi
    write_plist; launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"; sleep 3; "$0" status ;;
  start)   launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl kickstart "$DOMAIN/$LABEL"; sleep 3; "$0" status ;;
  stop)    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
           for i in $(seq 1 30); do [ "$(running)" = "0" ] && break; sleep 3; done
           (cd "$ROOT" && python3 deploy/cancel_all.py | tail -1) ;;
  restart) "$0" stop; "$0" start ;;
  status)  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit" || echo "not loaded"
           echo "bot processes: $(running)"; grep -E "HEALTH|cycle [0-9]+ done" "$ROOT/data/live.log" | tail -1 | cut -c1-160 ;;
  logs)    tail -f "$ROOT/data/live.log" ;;
  uninstall) "$0" stop; rm -f "$PLIST"; echo "removed $PLIST" ;;
  *) echo "usage: $0 install|start|stop|restart|status|logs|uninstall"; exit 2 ;;
esac
