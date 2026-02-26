<p align="center">
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/Python-3.8%2B-blue" alt="Python Version">
  </a>
  <a href="https://opensource.org/licenses/MIT">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  </a>
</p>

# 🏘️ Homeassistant Bridge

Homeassistant Bridge is a lightweight Python [**MeshMonitor**](https://github.com/Yeraze/MeshMonitor) Bridge script that connects [**Home Assistant**](https://www.home-assistant.io/) to MeshMonitor ([**Meshtastic**](https://meshtastic.org/)) using MQTT.

Home Assistant handles:
- Devices
- Automations
- Service execution
- State management

MeshMonitor handles:
- Meshtastic
- Webhooks
- Routing
- Delivery

Homeassistant Bridge focuses purely on:
- State normalization
- Event forwarding
- Secure command execution

No scraping.  
No bypassing authentication.  
No direct radio transmission.

---

## What It Does

### Home Assistant → MeshMonitor

Subscribes to Home Assistant state_changed events over the official WebSocket API and publishes normalized MQTT messages.

Example topic:

meshmonitor/homeassistant/light/light-front-porch/state

---

### MeshMonitor → Home Assistant

Listens for MQTT command messages and safely executes allowed Home Assistant services via the official call_service API.

Example command payload:

{
  "request_id": "t1",
  "domain": "light",
  "service": "turn_on",
  "service_data": {
    "entity_id": "light.front_porch"
  }
}

Response topics:

meshmonitor/homeassistant/cmd/ack  
meshmonitor/homeassistant/cmd/error

All service calls are filtered through configurable allowlists.

---

## Architecture

Home Assistant  <--WebSocket-->  Homeassistant Bridge  <--MQTT-->  MeshMonitor

The bridge:
- Authenticates using a Home Assistant Long-Lived Access Token
- Subscribes to state changes
- Publishes MQTT updates
- Executes allowed commands
- Returns structured acknowledgments

---

## Quick Start

### 1) Clone

git clone https://github.com/maxhayim/homeassistant-bridge.git  
cd homeassistant-bridge

---

### 2) Install

sudo bash install.sh

This will:
- Install Python dependencies
- Create /etc/meshmonitor/homeassistant-bridge.env
- Install and enable a systemd service

---

### 3) Configure

Edit:

sudo nano /etc/meshmonitor/homeassistant-bridge.env

Minimum required:

HA_URL=http://10.0.0.206:8123  
HA_TOKEN=YOUR_LONG_LIVED_TOKEN  

MQTT_HOST=127.0.0.1  
MQTT_PORT=1883  

---

### 4) Restart

sudo systemctl restart homeassistant-bridge.service

Logs:

journalctl -u homeassistant-bridge.service -f

---

## Home Assistant Setup

In Home Assistant:

Profile → Long-Lived Access Tokens → Create Token  

Copy the token into:

HA_TOKEN=...

---

## MQTT Topics

### State Updates

meshmonitor/homeassistant/<domain>/<entity_slug>/state

Example:

meshmonitor/homeassistant/binary_sensor/binary-sensor-front-door/state

---

### Alerts (optional)

meshmonitor/alerts/homeassistant/motion  
meshmonitor/alerts/homeassistant/contact_open  
meshmonitor/alerts/homeassistant/leak  
meshmonitor/alerts/homeassistant/smoke  
meshmonitor/alerts/homeassistant/carbon_monoxide  
meshmonitor/alerts/homeassistant/low_battery  

---

### Command Channel (optional)

Enable in env:

ENABLE_COMMANDS=true

Command topic:

meshmonitor/homeassistant/cmd/service

Ack topic:

meshmonitor/homeassistant/cmd/ack

Error topic:

meshmonitor/homeassistant/cmd/error

---

## Command Safety

Commands are protected by allowlists:

ALLOW_DOMAIN_REGEX=^(light|switch|lock|cover|climate|fan|input_boolean|script)$  
ALLOW_SERVICE_REGEX=^(turn_on|turn_off|toggle|lock|unlock|open_cover|close_cover|set_temperature|set_hvac_mode|press|trigger)$  
ALLOW_ENTITY_REGEX=^(light\.|switch\.|lock\.|cover\.|climate\.|fan\.|input_boolean\.|script\.)  

If a command does not match the allowlist, it is rejected.

This prevents arbitrary MQTT clients from executing unintended services.

---

## Environment Variables

Core:

HA_URL=http://127.0.0.1:8123  
HA_WS_URL= (optional override)  
HA_TOKEN=your_token  

MQTT_HOST=127.0.0.1  
MQTT_PORT=1883  
MQTT_USERNAME=  
MQTT_PASSWORD=  
MQTT_TLS=false  

Topics:

TOPIC_ROOT=meshmonitor/homeassistant  
ALERT_ROOT=meshmonitor/alerts/homeassistant  

Behavior:

PUBLISH_RETAIN=false  
ENABLE_ALERT_TOPICS=true  
ENABLE_COMMANDS=false  
HEARTBEAT_SECONDS=30  

Security:

ALLOW_DOMAIN_REGEX=  
ALLOW_SERVICE_REGEX=  
ALLOW_ENTITY_REGEX=  

---

## Requirements

- Python 3.8+
- MQTT broker
- Home Assistant (WebSocket API enabled by default)

No Docker required.  
No Supervisor Add-on required.  
No scraping.  
No shared credentials.

---

## Design Philosophy

This bridge follows:

- KISS (Keep It Simple)
- Local-first architecture
- Official APIs only
- Clear separation of responsibilities

Home Assistant remains the authority for device control.  
MeshMonitor remains the transport layer.  
Homeassistant Bridge simply connects them.

---

## License

This project is licensed under the MIT License.

See the LICENSE file for details.  
Full license text: https://opensource.org/licenses/MIT

---

## Acknowledgments

* MeshMonitor built by [Yeraze](https://github.com/Yeraze)
* [Home Assistant](https://www.home-assistant.io/)  
* [Eclipse Mosquitto)](https://mosquitto.org/) 

Discover other community-contributed scripts for MeshMonitor: https://meshmonitor.org/user-scripts.html
