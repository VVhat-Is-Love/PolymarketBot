#!/usr/bin/env bash
# Install and enable the daily SQLite backup systemd timer.
# Run as root on the production server.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
DEPLOY_DIR="$(dirname "$0")"

cp "$DEPLOY_DIR/polymarket-backup.service" "$UNIT_DIR/"
cp "$DEPLOY_DIR/polymarket-backup.timer"   "$UNIT_DIR/"

# Ensure backup directory exists and is owned by the bot user
mkdir -p /opt/polymarket-bot/backups
chown bot:bot /opt/polymarket-bot/backups

systemctl daemon-reload
systemctl enable --now polymarket-backup.timer

echo "Timer status:"
systemctl status polymarket-backup.timer --no-pager

echo ""
echo "Test run (runs backup immediately):"
echo "  systemctl start polymarket-backup.service"
echo ""
echo "View logs:"
echo "  journalctl -u polymarket-backup.service -n 50"
