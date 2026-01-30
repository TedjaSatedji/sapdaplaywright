#!/bin/bash
set -e

APP_DIR="/root/sapdaplaywright"
SYSTEMD_DIR="/etc/systemd/system"

echo "➡️ Installing systemd service files..."
cp "$APP_DIR/deploy/systemd/spadabot-tele.service" "$SYSTEMD_DIR/"
cp "$APP_DIR/deploy/systemd/spadabot-discord.service" "$SYSTEMD_DIR/"

echo "➡️ Reloading systemd..."
systemctl daemon-reload

echo "➡️ Enabling services on boot..."
systemctl enable spadabot-tele.service
systemctl enable spadabot-discord.service

echo "➡️ Restarting services..."
systemctl restart spadabot-tele.service
systemctl restart spadabot-discord.service

echo "✅ Services installed and running."
