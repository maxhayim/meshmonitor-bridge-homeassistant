#!/usr/bin/env bash
set -e

APP_NAME="homeassistant-bridge"
INSTALL_PATH="/usr/local/sbin/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="/etc/meshmonitor/${APP_NAME}.env"

echo "Installing ${APP_NAME}..."

# Ensure directories
sudo mkdir -p /etc/meshmonitor

# Install Python deps
sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install -r requirements.txt

# Copy main script
sudo cp meshmonitor_bridge_homeassistant.py "${INSTALL_PATH}"
sudo chmod +x "${INSTALL_PATH}"

# Create default env file if missing
if [ ! -f "${ENV_FILE}" ]; then
sudo tee "${ENV_FILE}" > /dev/null <<EOF
HA_URL=http://127.0.0.1:8123
HA_TOKEN=REPLACE_ME_LONG_LIVED_TOKEN

MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=
MQTT_TLS=false

TOPIC_ROOT=meshmonitor/homeassistant
ALERT_ROOT=meshmonitor/alerts/homeassistant

ENABLE_COMMANDS=false
PUBLISH_RETAIN=false

ALLOW_DOMAIN_REGEX=^(light|switch|lock|cover|climate|fan|input_boolean|script)$
ALLOW_SERVICE_REGEX=^(turn_on|turn_off|toggle|lock|unlock|open_cover|close_cover|set_temperature|set_hvac_mode|press|trigger)$
ALLOW_ENTITY_REGEX=
EOF
fi

# Install systemd service
sudo cp homeassistant-bridge.service "${SERVICE_FILE}"

sudo systemctl daemon-reload
sudo systemctl enable ${APP_NAME}.service

echo "Installation complete."
echo "Edit config: sudo nano ${ENV_FILE}"
echo "Start service: sudo systemctl start ${APP_NAME}.service"
