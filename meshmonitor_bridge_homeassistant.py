#!/usr/bin/env python3
# mm_meta:
#   name: Home Assistant Bridge
#   emoji: 🏘️
#   language: Python
#   description: Bidirectional Home Assistant ↔ MeshMonitor bridge via MQTT (state events + command execution).
__version__ = "1.0.0"

import asyncio
import json
import logging
import os
import re
import time
import hashlib
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt
import websockets


# ============================================================
# Environment Helpers
# ============================================================

def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


def compile_optional_regex(env_key: str) -> Optional[re.Pattern]:
    val = os.getenv(env_key, "").strip()
    return re.compile(val, re.I) if val else None


# ============================================================
# Utility
# ============================================================

def slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "unnamed"


def stable_id(*parts: str) -> str:
    h = hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()
    return h[:12]


def derive_ws_url() -> str:
    explicit = os.getenv("HA_WS_URL", "").strip()
    if explicit:
        return explicit

    ha_url = os.getenv("HA_URL", "http://127.0.0.1:8123").strip().rstrip("/")
    if ha_url.startswith("https://"):
        return "wss://" + ha_url[len("https://"):] + "/api/websocket"
    if ha_url.startswith("http://"):
        return "ws://" + ha_url[len("http://"):] + "/api/websocket"

    return "ws://" + ha_url + "/api/websocket"


def publish_json(client: mqtt.Client, topic: str, payload_obj: Dict[str, Any], retain: bool):
    payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
    client.publish(topic, payload=payload, qos=0, retain=retain)


# ============================================================
# State Normalization
# ============================================================

def friendly_name(state_obj: Dict[str, Any]) -> str:
    attrs = state_obj.get("attributes") or {}
    return (attrs.get("friendly_name") or state_obj.get("entity_id") or "unknown").strip()


def normalize_state(state_obj: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = str(state_obj.get("entity_id") or "")
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
    attrs = state_obj.get("attributes") or {}

    return {
        "entity_id": entity_id,
        "domain": domain,
        "name": friendly_name(state_obj),
        "state": state_obj.get("state"),
        "last_changed": state_obj.get("last_changed"),
        "last_updated": state_obj.get("last_updated"),
        "attributes_min": {
            "device_class": attrs.get("device_class"),
            "unit_of_measurement": attrs.get("unit_of_measurement"),
            "battery_level": attrs.get("battery_level"),
            "supported_features": attrs.get("supported_features"),
        },
        "ts": int(time.time()),
    }


def classify_alert(entity_id: str, new_state: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    domain = new_state.get("domain", "unknown")
    attrs = new_state.get("attributes_min") or {}
    device_class = (attrs.get("device_class") or "").lower()
    state = str(new_state.get("state") or "").lower()
    name = new_state.get("name") or entity_id

    if domain == "binary_sensor":
        if device_class in ("motion", "occupancy") and state == "on":
            return ("motion", "info", f"Motion detected: {name}")
        if device_class in ("door", "window", "opening") and state == "on":
            return ("contact_open", "info", f"Contact open: {name}")
        if device_class in ("moisture", "water", "leak") and state == "on":
            return ("leak", "warning", f"Leak detected: {name}")
        if device_class == "smoke" and state == "on":
            return ("smoke", "critical", f"Smoke detected: {name}")
        if device_class in ("carbon_monoxide", "gas") and state == "on":
            return ("carbon_monoxide", "critical", f"CO/Gas detected: {name}")
        if device_class == "battery" and state == "on":
            return ("low_battery", "warning", f"Low battery: {name}")

    return None


# ============================================================
# Command Safety
# ============================================================

def is_allowed(
    domain: str,
    service: str,
    entity_id: Optional[str],
    allow_domain: Optional[re.Pattern],
    allow_service: Optional[re.Pattern],
    allow_entity: Optional[re.Pattern],
) -> bool:
    if not allow_domain or not allow_service:
        return False
    if not allow_domain.search(domain):
        return False
    if not allow_service.search(service):
        return False
    if allow_entity:
        if not entity_id or not allow_entity.search(entity_id):
            return False
    return True


# ============================================================
# Globals
# ============================================================

MQTT_CMD_QUEUE: "asyncio.Queue[Tuple[str, str]]" = asyncio.Queue()
HA_WS: Optional[websockets.WebSocketClientProtocol] = None
HA_SEND_LOCK = asyncio.Lock()
NEXT_HA_ID = 100


# ============================================================
# MQTT
# ============================================================

def mqtt_connect() -> mqtt.Client:
    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = env_int("MQTT_PORT", 1883)
    user = os.getenv("MQTT_USERNAME", "")
    pwd = os.getenv("MQTT_PASSWORD", "")
    use_tls = env_bool("MQTT_TLS", False)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mm-ha-{os.getpid()}")

    if user:
        client.username_pw_set(user, pwd)
    if use_tls:
        client.tls_set()

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logging.info("MQTT connected to %s:%s", host, port)
        else:
            logging.error("MQTT connect failed: %s", reason_code)

    def on_message(c, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            MQTT_CMD_QUEUE.put_nowait((msg.topic, payload))
        except Exception:
            pass

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client


# ============================================================
# HA Service Calls
# ============================================================

async def ha_call_service(domain: str, service: str, service_data: Dict[str, Any]) -> Dict[str, Any]:
    global NEXT_HA_ID, HA_WS

    if HA_WS is None:
        raise RuntimeError("Home Assistant websocket not connected")

    async with HA_SEND_LOCK:
        NEXT_HA_ID += 1
        msg_id = NEXT_HA_ID

        await HA_WS.send(json.dumps({
            "id": msg_id,
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data or {}
        }))

        while True:
            raw = await HA_WS.recv()
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                return resp


# ============================================================
# Main Bridge
# ============================================================

async def run_bridge() -> int:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s"
    )

    ha_token = os.getenv("HA_TOKEN", "").strip()
    if not ha_token:
        logging.error("HA_TOKEN is required")
        return 2

    ws_url = derive_ws_url()

    topic_root = os.getenv("TOPIC_ROOT", "meshmonitor/homeassistant").rstrip("/")
    alert_root = os.getenv("ALERT_ROOT", "meshmonitor/alerts/homeassistant").rstrip("/")
    retain = env_bool("PUBLISH_RETAIN", False)

    enable_cmd = env_bool("ENABLE_COMMANDS", False)
    cmd_topic = os.getenv("COMMAND_TOPIC", f"{topic_root}/cmd/service")
    ack_topic = os.getenv("ACK_TOPIC", f"{topic_root}/cmd/ack")
    err_topic = os.getenv("ERROR_TOPIC", f"{topic_root}/cmd/error")

    allow_domain = compile_optional_regex("ALLOW_DOMAIN_REGEX")
    allow_service = compile_optional_regex("ALLOW_SERVICE_REGEX")
    allow_entity = compile_optional_regex("ALLOW_ENTITY_REGEX")

    client = mqtt_connect()

    if enable_cmd:
        client.subscribe(cmd_topic)
        logging.info("Command channel enabled: %s", cmd_topic)

    global HA_WS

    logging.info("meshmonitor-bridge-homeassistant v%s starting", __version__)

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                HA_WS = ws

                # Auth handshake
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
                await ws.recv()

                # Subscribe state_changed
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed"
                }))
                await ws.recv()

                logging.info("Connected to Home Assistant WS")

                while True:
                    raw = await ws.recv()
                    evt = json.loads(raw)
                    if evt.get("type") != "event":
                        continue

                    data = evt.get("event", {}).get("data", {})
                    new_state = data.get("new_state")
                    if not new_state:
                        continue

                    norm = normalize_state(new_state)
                    entity_id = norm["entity_id"]
                    domain = norm["domain"]

                    ent_slug = slug(entity_id)
                    base = f"{topic_root}/{domain}/{ent_slug}"

                    publish_json(client, f"{base}/state", norm, retain)

                    alert = classify_alert(entity_id, norm)
                    if alert:
                        kind, level, message = alert
                        publish_json(
                            client,
                            f"{alert_root}/{kind}",
                            {
                                "source": "homeassistant",
                                "entity_id": entity_id,
                                "level": level,
                                "message": message,
                                "ts": int(time.time())
                            },
                            False
                        )

        except Exception as e:
            logging.error("Bridge error: %s", e)
            await asyncio.sleep(5)


def main() -> int:
    try:
        return asyncio.run(run_bridge()) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
