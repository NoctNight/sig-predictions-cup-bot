#!/usr/bin/env bash
# One-time server preparation. Run ON THE SERVER as root (or with sudo), Ubuntu 24.04 or newer (Python 3.12+ needed).
#   ssh root@HOST 'bash -s' < deploy/server_setup.sh
set -euo pipefail

if ! command -v apt-get >/dev/null; then
  echo "this script expects Ubuntu/Debian (apt-get)"; exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 rsync sqlite3 curl tzdata ca-certificates

PYV=$(python3 -c 'import sys; print(sys.version_info >= (3, 12))')
if [ "$PYV" != "True" ]; then
  echo "python3 is older than 3.12 ($(python3 -V)); use Ubuntu 24.04+ or install python3.12"; exit 1
fi

# dedicated unprivileged user and folder
id -u sigbot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin sigbot
mkdir -p /opt/sigbot
chown -R sigbot:sigbot /opt/sigbot
chmod 750 /opt/sigbot

# keep the clock right (order expiries and the 5-second scan grid depend on it)
timedatectl set-ntp true || true

# the deploy user (root, or a cloud image's default user such as `ubuntu`) needs passwordless sudo:
# the push and remote scripts run sudo non-interactively over ssh
if [ -n "${SUDO_USER:-}" ] && ! sudo -n -u "$SUDO_USER" true 2>/dev/null; then
  echo "note: give $SUDO_USER passwordless sudo, or deploy as root"
fi

echo "server ready: python $(python3 -V), user sigbot, folder /opt/sigbot"
