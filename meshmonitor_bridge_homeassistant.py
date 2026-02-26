#!/usr/bin/env python3
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

    client.on_connect = on_connect
    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client


def publish_json(client: mqtt.Client, topic: str, payload_obj: Dict[str, Any], retain: bool):
    payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
    client.publish(topic, payload=payload, qos=0, retain=retain)


def passes_filters(
    entity_id: str,
    name: str,
    inc_ent: Optional[re.Pattern],
    exc_ent: Optional[re.Pattern],
    inc_name: Optional[re.Pattern],
    exc_name: Optional[re.Pattern],
) -> bool:
    if exc_ent and exc_ent.search(entity_id):
        return False
    if inc_ent and not inc_ent.search(entity_id):
        return False
    if exc_name and exc_name.search(name):
        return False
    if inc_name and not inc_name.search(name):
        return False
    return True


def friendly_name(state_obj: Dict[str, Any]) -> str:
    attrs = state_obj.get("attributes") or {}
    return (attrs.get("friendly_name") or state_obj.get("entity_id") or "unknown").strip()


def normalize_state(state_obj: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = str(state_obj.get("entity_id") or "")
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
    attrs = state_obj.get("attributes") or {}

    out = {
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
    return out


def classify_alert(entity_id: str, new_state: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    domain = new_state.get("domain", "unknown")
    attrs = (new_state.get("attributes_min") or {})
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

    if domain == "sensor":
        try:
            batt = attrs.get("battery_level")
            if batt is None and "battery" in entity_id.lower():
                batt = float(new_state.get("state"))
            if batt is not None:
                batt_f = float(batt)
                if batt_f <= 15:
                    return ("low_battery", "warning", f"Low battery ({batt_f:.0f}%): {name}")
        except Exception:
            pass

    return None


async def run_bridge() -> int:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    ha_token = os.getenv("HA_TOKEN", "").strip()
    if not ha_token or ha_token == "REPLACE_ME_LONG_LIVED_TOKEN":
        logging.error("HA_TOKEN is not set. Edit /etc/meshmonitor/meshmonitor-bridge-homeassistant.env")
        return 2

    ws_url = derive_ws_url()
    topic_root = os.getenv("TOPIC_ROOT", "meshmonitor/homeassistant").strip().rstrip("/")
    alert_root = os.getenv("ALERT_ROOT", "meshmonitor/alerts/homeassistant").strip().rstrip("/")
    retain = env_bool("PUBLISH_RETAIN", False)
    publish_attributes = env_bool("PUBLISH_ATTRIBUTES", False)
    enable_alerts = env_bool("ENABLE_ALERT_TOPICS", True)
    heartbeat_s = env_int("HEARTBEAT_SECONDS", 30)

    inc_ent = re.compile(os.getenv("INCLUDE_ENTITY_REGEX", "").strip(), re.I) if os.getenv("INCLUDE_ENTITY_REGEX", "").strip() else None
    exc_ent = re.compile(os.getenv("EXCLUDE_ENTITY_REGEX", "").strip(), re.I) if os.getenv("EXCLUDE_ENTITY_REGEX", "").strip() else None
    inc_name = re.compile(os.getenv("INCLUDE_NAME_REGEX", "").strip(), re.I) if os.getenv("INCLUDE_NAME_REGEX", "").strip() else None
    exc_name = re.compile(os.getenv("EXCLUDE_NAME_REGEX", "").strip(), re.I) if os.getenv("EXCLUDE_NAME_REGEX", "").strip() else None

    client = mqtt_connect()

    bridge_id = stable_id(ws_url, str(os.getpid()))
    hb_topic = f"{topic_root}/bridge/{bridge_id}/heartbeat"

    last_hb = 0.0
    backoff = 1

    logging.info("meshmonitor-bridge-homeassistant starting (ws=%s)", ws_url)

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1

                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    logging.warning("Unexpected first message: %s", msg)

                await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"HA auth failed: {msg}")

                sub_id = 1
                await ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}))
                ack = json.loads(await ws.recv())
                if not (ack.get("id") == sub_id and ack.get("success") is True):
                    raise RuntimeError(f"Subscribe failed: {ack}")

                logging.info("Subscribed to HA state_changed events")

                while True:
                    now = time.time()
                    if now - last_hb >= heartbeat_s:
                        publish_json(
                            client,
                            hb_topic,
                            {"source": "homeassistant", "bridge_id": bridge_id, "ws": ws_url, "ts": int(now)},
                            retain=retain,
                        )
                        last_hb = now

                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    evt = json.loads(raw)
                    if evt.get("type") != "event":
                        continue

                    event = evt.get("event") or {}
                    data = event.get("data") or {}
                    new_state = data.get("new_state")
                    if not new_state:
                        continue

                    entity_id = str(new_state.get("entity_id") or "")
                    norm = normalize_state(new_state)
                    name = norm.get("name") or entity_id

                    if not passes_filters(entity_id, name, inc_ent, exc_ent, inc_name, exc_name):
                        continue

                    domain = norm.get("domain", "unknown")
                    ent_slug = slug(entity_id)
                    base = f"{topic_root}/{domain}/{ent_slug}"

                    publish_json(client, f"{base}/state", norm, retain=retain)

                    if publish_attributes:
                        attrs = (new_state.get("attributes") or {})
                        publish_json(
                            client,
                            f"{base}/attributes",
                            {"entity_id": entity_id, "name": name, "attributes": attrs, "ts": int(time.time())},
                            retain=retain,
                        )

                    if enable_alerts:
                        alert = classify_alert(entity_id, norm)
                        if alert:
                            kind, level, message = alert
                            publish_json(
                                client,
                                f"{alert_root}/{kind}",
                                {
                                    "source": "homeassistant",
                                    "device": {"entity_id": entity_id, "name": name},
                                    "kind": kind,
                                    "level": level,
                                    "message": message,
                                    "state": norm.get("state"),
                                    "ts": int(time.time()),
                                },
                                retain=retain,
                            )

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logging.error("HA WS error: %s", e)
            await asyncio.sleep(min(60, backoff))
            backoff = min(60, backoff * 2)


def main() -> int:
    try:
        return asyncio.run(run_bridge()) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
