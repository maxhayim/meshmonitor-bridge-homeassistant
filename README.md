<p align="center">
  <img src="docs/assets/logo.png" alt="Homeassistant Bridge Logo" width="200"/>
</p>
<p align="center">
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/Python-3.8%2B-blue" alt="Python Version">
  </a>
  <a href="https://opensource.org/licenses/MIT">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  </a>
  <a href="https://github.com/maxhayim/meshmonitor-bridge-homeassistant/releases">
    <img src="https://img.shields.io/badge/Release-v3.0.1-orange" alt="Release">
  </a>
  <a href="https://github.com/maxhayim/meshmonitor-bridge-homeassistant/actions/workflows/tests.yml">
    <img src="https://github.com/maxhayim/meshmonitor-bridge-homeassistant/actions/workflows/tests.yml/badge.svg" alt="Tests">
  </a>
</p>

# 🏘️ Homeassistant Bridge

Homeassistant Bridge is a lightweight Python [**MeshMonitor**](https://github.com/Yeraze/MeshMonitor) bridge script that connects [**Home Assistant**](https://www.home-assistant.io/) to MeshMonitor via MQTT, enabling Home Assistant state and service commands to be relayed to [**Meshtastic**](https://meshtastic.org/), [**MeshCore**](https://meshcore.co.uk/), or any other mesh network MeshMonitor supports.
 
Home Assistant handles:
- Devices
- Automations
- Service execution
- State management
MeshMonitor handles:
- Meshtastic, MeshCore, and any other mesh network it supports
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
 
On connect, publishes a one-time snapshot of every current entity state, then
subscribes to Home Assistant state_changed events over the official
WebSocket API and publishes normalized MQTT messages as they happen. The
subscription is established before the snapshot is requested, so no event in
between is ever missed. Use `STATE_DOMAIN_FILTER_REGEX` / `STATE_ENTITY_FILTER_REGEX`
to limit both the snapshot and the live stream to a subset of entities.
 
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
 
## Repository layout
 
    .
    ├── meshmonitor_bridge_homeassistant.py   # Runtime bridge script
    ├── requirements.txt                      # Python dependencies (bare-metal installs)
    ├── install.sh                            # systemd install (bare-metal)
    ├── uninstall.sh                           # systemd removal (bare-metal)
    ├── homeassistant-bridge.service          # systemd unit file
    ├── homeassistant-bridge.env.example      # Environment variable template
    ├── Dockerfile                            # Container build
    ├── docker-compose.yml                    # Container deployment
    ├── .dockerignore
    ├── tests/                                # pytest suite (logic + mocked MQTT/WS)
    ├── requirements-dev.txt                  # Test-only dependencies
    ├── .github/workflows/
    │   ├── docker-publish.yml                # Multi-arch GHCR build + publish
    │   └── tests.yml                         # pytest on push/PR
    ├── docs/assets/                          # Logo and documentation images
    └── README.md
 
---
 
## Quick Start
 
### 1) Clone
 
    git clone https://github.com/maxhayim/meshmonitor-bridge-homeassistant.git
    cd meshmonitor-bridge-homeassistant
 
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
 
## Docker
 
### 1) Pull
 
    docker pull ghcr.io/maxhayim/meshmonitor-bridge-homeassistant:latest
 
---
 
### 2) Configure
 
    cp homeassistant-bridge.env.example homeassistant-bridge.env
 
Edit `homeassistant-bridge.env` with your `HA_TOKEN`, `HA_URL`, and MQTT settings (same variables as the [Environment Variables](#environment-variables) section below).
 
---
 
### 3) Run
 
    docker run -d \
      --name homeassistant-bridge \
      --restart unless-stopped \
      --env-file homeassistant-bridge.env \
      ghcr.io/maxhayim/meshmonitor-bridge-homeassistant:latest
 
Or with Docker Compose:
 
    docker compose up -d
 
Logs:
 
    docker logs -f homeassistant-bridge
 
The bridge is stateless — no volumes are required beyond the env file.
 
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
 
Each of these three topics can be overridden independently via `COMMAND_TOPIC`, `ACK_TOPIC`, and `ERROR_TOPIC`.
 
---
 
### Health Heartbeat
 
Published on a timer (`HEARTBEAT_SECONDS`, default 30s), retained:
 
    meshmonitor/homeassistant/status
    meshmonitor/homeassistant/version
 
Override with `STATUS_TOPIC` and `VERSION_TOPIC`.

### Availability

Retained, set via MQTT Last Will and Testament so a crash or unplugged
bridge is reflected immediately (not just after a heartbeat gap):

    meshmonitor/homeassistant/available

Payload is the plain string `online` or `offline` (not JSON), matching Home
Assistant's MQTT availability-topic convention. Override with `AVAILABILITY_TOPIC`.
 
---
 
## Command Safety

**As of v3.0.0, command execution is default-deny.** `ENABLE_COMMANDS=true` is
not enough by itself — `ALLOW_DOMAIN_REGEX`, `ALLOW_SERVICE_REGEX`, **and**
`ALLOW_ENTITY_REGEX` must *all three* be set, or every command is rejected.
In prior versions, leaving `ALLOW_ENTITY_REGEX` empty let any entity through
as long as domain/service matched — that was a real gap, not a supported
"no restriction" mode, and it's closed now. See
[Migration Notes](#migration-notes-v2x--v300) if you're upgrading.

`ALLOW_DOMAIN_REGEX` and `ALLOW_SERVICE_REGEX` must match the *entire* value
(`re.fullmatch`), so an unanchored pattern like `light` can no longer match a
substring such as domain `lighting_rig`. `ALLOW_ENTITY_REGEX` stays a prefix
match (`re.search`), so one pattern below still covers every entity under a domain.

**Every target entity is validated, not just the first.** A command with
`"entity_id": ["light.porch", "climate.furnace"]` is rejected outright if
*any* entry fails the allowlist — it does not silently act on the ones that pass.

**Area, device, and label targeting is rejected by default.** Home
Assistant's `target` object can address a whole area, device, or label
instead of (or alongside) `entity_id`; none of those are checked against
`ALLOW_ENTITY_REGEX`, so they're refused unless you set
`ALLOW_TARGET_AREA_DEVICE_LABEL=true` (advanced/trusted use — it only stops
blocking the target type, it does not add an allowlist for it).

**Additional protections**, all configurable, 0 disables:

    MAX_COMMAND_PAYLOAD_BYTES=8192
    COMMAND_RATE_LIMIT_PER_MINUTE=60
    COMMAND_DEDUP_WINDOW_SECONDS=60   # rejects a repeated request_id seen within this window

Allowed and denied commands are logged (domain/service/entity targets and
`request_id`) for audit purposes — never the raw payload or any Home
Assistant credential.
 
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
    MQTT_CLIENT_ID= (default: mm-ha-<slugified HA_URL>, stable across restarts)
    MQTT_CMD_QUEUE_MAXSIZE=500

Topics:
 
    TOPIC_ROOT=meshmonitor/homeassistant
    ALERT_ROOT=meshmonitor/alerts/homeassistant
    COMMAND_TOPIC= (default: {TOPIC_ROOT}/cmd/service)
    ACK_TOPIC= (default: {TOPIC_ROOT}/cmd/ack)
    ERROR_TOPIC= (default: {TOPIC_ROOT}/cmd/error)
    STATUS_TOPIC= (default: {TOPIC_ROOT}/status)
    VERSION_TOPIC= (default: {TOPIC_ROOT}/version)
    AVAILABILITY_TOPIC= (default: {TOPIC_ROOT}/available)
 
Behavior:
 
    PUBLISH_RETAIN=false
    ENABLE_ALERT_TOPICS=true
    ENABLE_COMMANDS=false
    HEARTBEAT_SECONDS=30
    LOW_BATTERY_THRESHOLD=20          # numeric sensor.* battery %, crossing-only; set negative to disable
    STATE_DOMAIN_FILTER_REGEX=        # optional, only publish matching domains
    STATE_ENTITY_FILTER_REGEX=        # optional, only publish matching entity_ids

Security:
 
    ALLOW_DOMAIN_REGEX=
    ALLOW_SERVICE_REGEX=
    ALLOW_ENTITY_REGEX=
    ALLOW_TARGET_AREA_DEVICE_LABEL=false
    MAX_COMMAND_PAYLOAD_BYTES=8192
    COMMAND_RATE_LIMIT_PER_MINUTE=60
    COMMAND_DEDUP_WINDOW_SECONDS=60
 
---
 
## Migration Notes (v2.x → v3.0.0)

This is a security-hardening release with one breaking behavior change:

**`ENABLE_COMMANDS=true` now requires `ALLOW_ENTITY_REGEX` to be set.** If you
were running with `ALLOW_DOMAIN_REGEX`/`ALLOW_SERVICE_REGEX` set but
`ALLOW_ENTITY_REGEX` empty, every command will start being rejected (logged,
published to the error topic) instead of silently passing with no
entity-level restriction. Set `ALLOW_ENTITY_REGEX` to the entity prefixes you
intend to allow (see [Command Safety](#command-safety)) to restore command
execution — scoped, this time, to the entities you actually meant to expose.

Everything else in v3.0.0 is additive and backward compatible: existing MQTT
topics, payload shapes, and all other environment variables keep their prior
defaults and behavior.
 
---
 
## Requirements
 
**Bare metal:**
- Python 3.8+
- MQTT broker
- Home Assistant (WebSocket API enabled by default)
**Docker:**
- Docker Engine or Docker Desktop
- MQTT broker (can run alongside in the same compose stack)
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

## Testing

    pip install -r requirements-dev.txt
    pytest tests/ -v

The suite covers command-allowlist edge cases (full entity-list validation,
area/device/label denial, default-deny with no entity regex), low-battery
threshold crossing/suppression, the single-reader WebSocket dispatch, and the
thread-safe MQTT command queue — against mocked MQTT/WebSocket objects, not a
live Home Assistant or MQTT broker.

---

## Known Limitations

- One Home Assistant instance and one MeshMonitor MQTT broker per running
  bridge process — multi-instance support is not yet implemented.
- There is no native Home Assistant custom integration (`custom_components/`)
  yet; this is the standalone MQTT bridge only.
- `ALLOW_TARGET_AREA_DEVICE_LABEL=true` does not validate *which* area,
  device, or label was targeted — it only stops blocking that target type.
  Only enable it if you trust everything publishing to the command topic.
- Low-battery threshold detection only applies to numeric `sensor.*` entities
  with `device_class: battery`; the existing binary `battery` device-class
  alert (on `binary_sensor.*`) is unchanged.

---
 
## License
 
This project is licensed under the MIT License.
 
See the [LICENSE](LICENSE) file for details.
Full license text: https://opensource.org/licenses/MIT
 
---
 
## Contributing
 
Pull requests are welcome. Open an issue first to discuss ideas or report bugs.
 
---
 
## Acknowledgments
 
* MeshMonitor built by [Yeraze](https://github.com/Yeraze)
* [Home Assistant](https://www.home-assistant.io/)
* [Eclipse Mosquitto](https://mosquitto.org/)
* Shout out to [dziban303](https://github.com/dziban303) for Docker support.

Discover other community-contributed scripts for MeshMonitor: https://meshmonitor.org/user-scripts.html
 





