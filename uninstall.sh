#!/usr/bin/env bash
set -e

APP_NAME="homeassistant-bridge"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
INSTALL_PATH="/usr/local/sbin/${APP_NAME}"

echo "Stopping service..."
sudo systemctl stop ${APP_NAME}.service || true
sudo systemctl disable ${APP_NAME}.service || true

echo "Removing files..."
sudo rm -f "${SERVICE_FILE}"
sudo rm -f "${INSTALL_PATH}"

sudo systemctl daemon-reload

echo "Uninstall complete."
