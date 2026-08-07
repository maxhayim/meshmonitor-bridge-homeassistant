#!/usr/bin/env python3
# mm_meta:
#   name: Homeassistant Bridge
#   emoji: 🏘️
#   language: Python
#   description: Bidirectional Home Assistant ↔ MeshMonitor bridge via MQTT (state events + command execution) with MQTT health heartbeat.
__version__ = "2.0.1"

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
# Globals / Health
# ============================================================

START_TIME = int(time.time())
MQTT_CONNECTED = False
HA_CONNECTED = False

MQTT_CMD_QUEUE: "asyncio.Queue[Tuple[str, str]]" = asyncio.Queue()
HA_WS: Optional[websockets.WebSocketClientProtocol] = None
HA_SEND_LOCK = asyncio.Lock()
NEXT_HA_ID = 100
HA_PENDING: Dict[int, "asyncio.Future[Dict[str, Any]]"] = {}


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
        global MQTT_CONNECTED
        if reason_code == 0:
            MQTT_CONNECTED = True
            logging.info("MQTT connected to %s:%s", host, port)
        else:
            MQTT_CONNECTED = False
            logging.error("MQTT connect failed: %s", reason_code)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):
        global MQTT_CONNECTED
        MQTT_CONNECTED = False
        logging.warning("MQTT disconnected: %s", reason_code)

    def on_message(c, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            MQTT_CMD_QUEUE.put_nowait((msg.topic, payload))
        except Exception:
            pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client


# ============================================================
# Health Heartbeat
# ============================================================

async def heartbeat_task(client: mqtt.Client, interval_s: int, status_topic: str, version_topic: str):
    # Publish version once (retained)
    try:
        client.publish(
            version_topic,
            json.dumps({"app": "homeassistant-bridge", "version": __version__, "ts": int(time.time())}),
            retain=True,
        )
    except Exception:
        pass

    while True:
        try:
            uptime = int(time.time()) - START_TIME
            payload = {
                "app": "homeassistant-bridge",
                "version": __version__,
                "ha_connected": HA_CONNECTED,
                "mqtt_connected": MQTT_CONNECTED,
                "uptime_seconds": uptime,
                "ts": int(time.time()),
            }
            client.publish(status_topic, json.dumps(payload), retain=True)
        except Exception:
            pass

        await asyncio.sleep(max(5, interval_s))


# ============================================================
# HA Service Calls (optional command execution)
# ============================================================

async def ha_call_service(domain: str, service: str, service_data: Dict[str, Any]) -> Dict[str, Any]:
    global NEXT_HA_ID, HA_WS

    if HA_WS is None:
        raise RuntimeError("Home Assistant websocket not connected")

    async with HA_SEND_LOCK:
        NEXT_HA_ID += 1
        msg_id = NEXT_HA_ID
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_event_loop().create_future()
        HA_PENDING[msg_id] = fut

        await HA_WS.send(json.dumps({
            "id": msg_id,
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data or {}
        }))

    try:
        return await asyncio.wait_for(fut, timeout=15)
    finally:
        HA_PENDING.pop(msg_id, None)


async def command_consumer(
    client: mqtt.Client,
    cmd_topic: str,
    ack_topic: str,
    err_topic: str,
    allow_domain: Optional[re.Pattern],
    allow_service: Optional[re.Pattern],
    allow_entity: Optional[re.Pattern],
):
    while True:
        _topic, payload_s = await MQTT_CMD_QUEUE.get()

        request_id = ""
        try:
            cmd = json.loads(payload_s)
            if not isinstance(cmd, dict):
                raise ValueError("Command payload must be a JSON object")

            request_id = str(cmd.get("request_id") or "")
            domain = str(cmd.get("domain") or "").strip()
            service = str(cmd.get("service") or "").strip()
            service_data = cmd.get("service_data") or {}

            if not domain or not service:
                raise ValueError("domain and service are required")

            if not isinstance(service_data, dict):
                raise ValueError("service_data must be a JSON object")

            ent = service_data.get("entity_id")
            ent_id = None
            if isinstance(ent, str):
                ent_id = ent
            elif isinstance(ent, list) and ent and isinstance(ent[0], str):
                ent_id = ent[0]

            if not is_allowed(domain, service, ent_id, allow_domain, allow_service, allow_entity):
                raise PermissionError(f"Denied by allowlist: {domain}.{service} entity={ent_id}")

            resp = await ha_call_service(domain, service, service_data)
            ok = bool(resp.get("success", False))

            publish_json(
                client,
                ack_topic,
                {"request_id": request_id, "ok": ok, "response": resp, "ts": int(time.time())},
                retain=False,
            )

        except Exception as e:
            publish_json(
                client,
                err_topic,
                {"request_id": request_id, "ok": False, "error": str(e), "ts": int(time.time())},
                retain=False,
            )


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

    enable_alerts = env_bool("ENABLE_ALERT_TOPICS", True)

    enable_cmd = env_bool("ENABLE_COMMANDS", False)
    cmd_topic = os.getenv("COMMAND_TOPIC", f"{topic_root}/cmd/service")
    ack_topic = os.getenv("ACK_TOPIC", f"{topic_root}/cmd/ack")
    err_topic = os.getenv("ERROR_TOPIC", f"{topic_root}/cmd/error")

    allow_domain = compile_optional_regex("ALLOW_DOMAIN_REGEX")
    allow_service = compile_optional_regex("ALLOW_SERVICE_REGEX")
    allow_entity = compile_optional_regex("ALLOW_ENTITY_REGEX")

    # Heartbeat topics + interval
    heartbeat_seconds = env_int("HEARTBEAT_SECONDS", 30)
    status_topic = os.getenv("STATUS_TOPIC", f"{topic_root}/status").strip()
    version_topic = os.getenv("VERSION_TOPIC", f"{topic_root}/version").strip()

    client = mqtt_connect()

    # Start heartbeat (retained status/version)
    asyncio.create_task(heartbeat_task(client, heartbeat_seconds, status_topic, version_topic))

    if enable_cmd:
        client.subscribe(cmd_topic)
        logging.info("Command channel enabled: %s", cmd_topic)

    global HA_WS, HA_CONNECTED

    logging.info("homeassistant-bridge v%s starting (ws=%s)", __version__, ws_url)

    while True:
        consumer_task: Optional[asyncio.Task] = None

        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                HA_WS = ws

                # Auth handshake
                first = json.loads(await ws.recv())
                if first.get("type") != "auth_required":
                    logging.warning("Unexpected first WS message: %s", first)

                await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"HA auth failed: {auth}")

                # Subscribe state_changed
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed"
                }))
                sub = json.loads(await ws.recv())
                if not (sub.get("id") == 1 and sub.get("success") is True):
                    raise RuntimeError(f"HA subscribe failed: {sub}")

                HA_CONNECTED = True
                logging.info("Connected to Home Assistant WS and subscribed to state_changed")

                if enable_cmd:
                    consumer_task = asyncio.create_task(
                        command_consumer(client, cmd_topic, ack_topic, err_topic, allow_domain, allow_service, allow_entity)
                    )

                while True:
                    raw = await ws.recv()
                    evt = json.loads(raw)

                    msg_id = evt.get("id")
                    if msg_id is not None and msg_id in HA_PENDING:
                        fut = HA_PENDING.get(msg_id)
                        if fut and not fut.done():
                            fut.set_result(evt)
                        continue

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

                    if enable_alerts:
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
            HA_CONNECTED = False
            HA_WS = None
            logging.error("Bridge error: %s", e)
            await asyncio.sleep(5)
        finally:
            HA_CONNECTED = False
            HA_WS = None
            for fut in HA_PENDING.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Home Assistant websocket disconnected"))
            HA_PENDING.clear()
            if consumer_task:
                consumer_task.cancel()
                try:
                    await consumer_task
                except Exception:
                    pass


def main() -> int:
    try:
        return asyncio.run(run_bridge()) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
