#!/bin/bash
# Run the dashboard (python3 -m sigbot.dashboard, http://127.0.0.1:8765) as a macOS launchd agent next to the bot.
#   deploy/mac/dashboard_ctl.sh install   write ~/Library/LaunchAgents/com.sizhe.sigbot.dashboard.plist and start it
#   deploy/mac/dashboard_ctl.sh start|stop|restart|status|logs|uninstall
# The dashboard only reads the database and makes at most 2 venue reads a minute; stopping it never touches orders.
# Runs at low priority (nice 10, Background process type), restarts if it crashes, starts at login once installed.
set -euo pipefail
LABEL=com.sizhe.sigbot.dashboard
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"
DOMAIN="gui/$(id -u)"
LOG="$ROOT/data/dashboard.log"
URL=http://127.0.0.1:8765

write_plist() {
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>sigbot.dashboard</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>Nice</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
PL
}
case "${1:-status}" in
  install)   write_plist; launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
             launchctl bootstrap "$DOMAIN" "$PLIST"; sleep 3; "$0" status ;;
  start)     [ -f "$PLIST" ] || { echo "not installed: run $0 install"; exit 1; }
             launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl kickstart "$DOMAIN/$LABEL"; sleep 3; "$0" status ;;
  stop)      launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; echo "stopped" ;;
  restart)   "$0" stop; "$0" start ;;
  status)    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit" || echo "not loaded"
             if curl -s -o /dev/null -m 3 "$URL/"; then echo "serving $URL"; else echo "nothing on $URL"; fi ;;
  logs)      tail -f "$LOG" ;;
  uninstall) "$0" stop; rm -f "$PLIST"; echo "removed $PLIST" ;;
  *) echo "usage: $0 install|start|stop|restart|status|logs|uninstall"; exit 2 ;;
esac
