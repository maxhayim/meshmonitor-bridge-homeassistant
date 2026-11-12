#!/usr/bin/env python3
# mm_meta:
#   name: Homeassistant Bridge
#   emoji: 🏘️
#   language: Python
#   description: Bidirectional Home Assistant ↔ MeshMonitor bridge via MQTT (state events + command execution) with MQTT health heartbeat.
__version__ = "3.2.0"

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

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


# Tracks per-entity low-battery alert state so a numeric sensor crossing the
# threshold alerts once, not on every subsequent update while still low.
LOW_BATTERY_STATE: Dict[str, bool] = {}


def check_low_battery(entity_id: str, new_state: Dict[str, Any], threshold: float) -> Optional[Tuple[str, str, str]]:
    if new_state.get("domain") != "sensor":
        return None

    attrs = new_state.get("attributes_min") or {}
    if (attrs.get("device_class") or "").lower() != "battery":
        return None

    raw_state = new_state.get("state")
    if raw_state is None or str(raw_state).strip().lower() in ("", "unknown", "unavailable"):
        return None

    try:
        value = float(raw_state)
    except (TypeError, ValueError):
        return None

    name = new_state.get("name") or entity_id
    was_low = LOW_BATTERY_STATE.get(entity_id, False)

    if value <= threshold:
        if was_low:
            return None
        LOW_BATTERY_STATE[entity_id] = True
        return ("low_battery", "warning", f"Low battery: {name} ({value:.0f}%)")

    if was_low:
        LOW_BATTERY_STATE[entity_id] = False

    return None


def state_passes_filter(
    entity_id: str,
    domain: str,
    domain_filter: Optional[re.Pattern],
    entity_filter: Optional[re.Pattern],
) -> bool:
    if domain_filter and not domain_filter.search(domain):
        return False
    if entity_filter and not entity_filter.search(entity_id):
        return False
    return True


def publish_state(
    client: mqtt.Client,
    topic_root: str,
    alert_root: str,
    retain: bool,
    enable_alerts: bool,
    low_battery_threshold: Optional[float],
    state_obj: Dict[str, Any],
    domain_filter: Optional[re.Pattern],
    entity_filter: Optional[re.Pattern],
) -> Optional[str]:
    norm = normalize_state(state_obj)
    entity_id = norm["entity_id"]
    domain = norm["domain"]

    if not entity_id or not state_passes_filter(entity_id, domain, domain_filter, entity_filter):
        return None

    ent_slug = slug(entity_id)
    base = f"{topic_root}/{domain}/{ent_slug}"
    publish_json(client, f"{base}/state", norm, retain)

    if enable_alerts:
        alert = classify_alert(entity_id, norm)
        if alert is None and low_battery_threshold is not None:
            alert = check_low_battery(entity_id, norm, low_battery_threshold)
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
                    "ts": int(time.time()),
                },
                False,
            )

    return entity_id


# ============================================================
# Command Safety
# ============================================================

def extract_command_targets(
    service_data: Dict[str, Any],
    target: Optional[Dict[str, Any]],
    allow_area_device_label: bool,
) -> Tuple[List[str], bool]:
    """Returns (entity_ids, has_disallowed_target).

    has_disallowed_target is True if the command names an area/device/label
    target (and that's not explicitly permitted) or any non-string/non-list
    entity reference we can't validate.
    """
    entity_ids: List[str] = []
    has_disallowed_target = False

    def collect_entities(val: Any) -> None:
        nonlocal has_disallowed_target
        if val is None:
            return
        if isinstance(val, str):
            entity_ids.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    entity_ids.append(item)
                else:
                    has_disallowed_target = True
        else:
            has_disallowed_target = True

    collect_entities(service_data.get("entity_id"))

    if target:
        collect_entities(target.get("entity_id"))
        if not allow_area_device_label and any(
            target.get(k) for k in ("area_id", "device_id", "label_id")
        ):
            has_disallowed_target = True

    return entity_ids, has_disallowed_target


def is_allowed(
    domain: str,
    service: str,
    entity_ids: List[str],
    has_disallowed_target: bool,
    allow_domain: Optional[re.Pattern],
    allow_service: Optional[re.Pattern],
    allow_entity: Optional[re.Pattern],
) -> bool:
    # Default-deny: an entity allowlist is mandatory. Without one there is no
    # way to validate *what* a command would affect, so nothing is permitted.
    if not allow_domain or not allow_service or not allow_entity:
        return False
    if has_disallowed_target:
        return False
    if not entity_ids:
        return False
    # domain/service are single exact values, so a full match prevents an
    # unanchored user pattern (e.g. "light") from matching a substring
    # (e.g. domain "lighting_rig"). Entity patterns are documented/used as
    # prefix matches (e.g. "^(light\.|switch\.)"), so that one stays a search.
    if not allow_domain.fullmatch(domain):
        return False
    if not allow_service.fullmatch(service):
        return False
    return all(allow_entity.search(e) for e in entity_ids)


# ============================================================
# Globals / Health
# ============================================================

START_TIME = int(time.time())
MQTT_CONNECTED = False
HA_CONNECTED = False

HA_WS: Optional[websockets.WebSocketClientProtocol] = None
HA_SEND_LOCK = asyncio.Lock()
NEXT_HA_ID = 100
HA_PENDING: Dict[int, "asyncio.Future[Dict[str, Any]]"] = {}


# ============================================================
# MQTT
# ============================================================

def mqtt_connect(
    cmd_queue: "asyncio.Queue[Tuple[str, str]]",
    enable_cmd: bool,
    cmd_topic: str,
    avail_topic: str,
    queue_maxsize: int,
) -> mqtt.Client:
    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = env_int("MQTT_PORT", 1883)
    user = os.getenv("MQTT_USERNAME", "")
    pwd = os.getenv("MQTT_PASSWORD", "")
    use_tls = env_bool("MQTT_TLS", False)

    ha_url_for_id = os.getenv("HA_URL", "http://127.0.0.1:8123").strip()
    client_id = os.getenv("MQTT_CLIENT_ID", "").strip() or f"mm-ha-{slug(ha_url_for_id)}"

    loop = asyncio.get_running_loop()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

    if user:
        client.username_pw_set(user, pwd)
    if use_tls:
        client.tls_set()

    # Last Will and Testament: broker publishes "offline" (retained) if we
    # disconnect uncleanly, so availability stays correct without a client-side timeout.
    client.will_set(avail_topic, payload="offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        global MQTT_CONNECTED
        if reason_code == 0:
            MQTT_CONNECTED = True
            logging.info("MQTT connected to %s:%s", host, port)
            if enable_cmd:
                c.subscribe(cmd_topic)
                logging.info("Command channel (re)subscribed: %s", cmd_topic)
            try:
                c.publish(avail_topic, "online", retain=True)
            except Exception:
                pass
        else:
            MQTT_CONNECTED = False
            logging.error("MQTT connect failed: %s", reason_code)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):
        global MQTT_CONNECTED
        MQTT_CONNECTED = False
        logging.warning("MQTT disconnected: %s", reason_code)

    def _queue_put_safe(item: Tuple[str, str]) -> None:
        try:
            cmd_queue.put_nowait(item)
        except asyncio.QueueFull:
            logging.warning("Command queue full (maxsize=%d); dropping message", queue_maxsize)

    def on_message(c, userdata, msg):
        # Paho invokes this on its own network thread; asyncio.Queue is not
        # thread-safe, so hand the put to the event loop instead of calling it directly.
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            loop.call_soon_threadsafe(_queue_put_safe, (msg.topic, payload))
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

async def ha_call_service(
    domain: str,
    service: str,
    service_data: Dict[str, Any],
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global NEXT_HA_ID, HA_WS

    if HA_WS is None:
        raise RuntimeError("Home Assistant websocket not connected")

    async with HA_SEND_LOCK:
        NEXT_HA_ID += 1
        msg_id = NEXT_HA_ID
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_event_loop().create_future()
        HA_PENDING[msg_id] = fut

        msg = {
            "id": msg_id,
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data or {},
        }
        if target:
            msg["target"] = target

        await HA_WS.send(json.dumps(msg))

    try:
        return await asyncio.wait_for(fut, timeout=15)
    finally:
        HA_PENDING.pop(msg_id, None)


async def ha_get_states() -> List[Dict[str, Any]]:
    global NEXT_HA_ID

    if HA_WS is None:
        raise RuntimeError("Home Assistant websocket not connected")

    async with HA_SEND_LOCK:
        NEXT_HA_ID += 1
        msg_id = NEXT_HA_ID
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_event_loop().create_future()
        HA_PENDING[msg_id] = fut

        await HA_WS.send(json.dumps({"id": msg_id, "type": "get_states"}))

    try:
        resp = await asyncio.wait_for(fut, timeout=15)
    finally:
        HA_PENDING.pop(msg_id, None)

    if not resp.get("success"):
        raise RuntimeError(f"get_states failed: {resp}")

    return resp.get("result") or []


async def command_consumer(
    client: mqtt.Client,
    cmd_queue: "asyncio.Queue[Tuple[str, str]]",
    ack_topic: str,
    err_topic: str,
    allow_domain: Optional[re.Pattern],
    allow_service: Optional[re.Pattern],
    allow_entity: Optional[re.Pattern],
    allow_area_device_label: bool,
    rate_limit_per_minute: int,
    dedup_window_s: int,
    max_payload_bytes: int,
):
    rate_window: "deque[float]" = deque()
    seen_requests: Dict[str, float] = {}

    while True:
        _topic, payload_s = await cmd_queue.get()

        request_id = ""
        try:
            if max_payload_bytes > 0 and len(payload_s.encode("utf-8")) > max_payload_bytes:
                raise ValueError(f"Command payload exceeds {max_payload_bytes} bytes")

            cmd = json.loads(payload_s)
            if not isinstance(cmd, dict):
                raise ValueError("Command payload must be a JSON object")

            request_id = str(cmd.get("request_id") or "")
            now = time.monotonic()

            if dedup_window_s > 0 and request_id:
                for rid in [r for r, ts in seen_requests.items() if now - ts > dedup_window_s]:
                    seen_requests.pop(rid, None)
                if request_id in seen_requests:
                    raise ValueError(f"Duplicate request_id within dedup window: {request_id}")
                seen_requests[request_id] = now

            if rate_limit_per_minute > 0:
                while rate_window and now - rate_window[0] > 60:
                    rate_window.popleft()
                if len(rate_window) >= rate_limit_per_minute:
                    raise ValueError("Command rate limit exceeded")
                rate_window.append(now)

            domain = str(cmd.get("domain") or "").strip()
            service = str(cmd.get("service") or "").strip()
            service_data = cmd.get("service_data") or {}
            target = cmd.get("target")

            if not domain or not service:
                raise ValueError("domain and service are required")
            if not isinstance(service_data, dict):
                raise ValueError("service_data must be a JSON object")
            if target is not None and not isinstance(target, dict):
                raise ValueError("target must be a JSON object")

            entity_ids, has_disallowed_target = extract_command_targets(
                service_data, target, allow_area_device_label
            )

            if not is_allowed(
                domain, service, entity_ids, has_disallowed_target,
                allow_domain, allow_service, allow_entity,
            ):
                logging.warning(
                    "Command denied: domain=%s service=%s entities=%s disallowed_target=%s request_id=%s",
                    domain, service, entity_ids, has_disallowed_target, request_id,
                )
                raise PermissionError(f"Denied by allowlist: {domain}.{service} entities={entity_ids}")

            logging.info(
                "Command allowed: domain=%s service=%s entities=%s request_id=%s",
                domain, service, entity_ids, request_id,
            )

            resp = await ha_call_service(domain, service, service_data, target)
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
# Home Assistant WebSocket dispatch
# ============================================================

async def ha_recv_loop(
    ws,
    client: mqtt.Client,
    topic_root: str,
    alert_root: str,
    retain: bool,
    enable_alerts: bool,
    low_battery_threshold: Optional[float],
    domain_filter: Optional[re.Pattern],
    entity_filter: Optional[re.Pattern],
    touched: set,
):
    """Single reader of the HA websocket: dispatches call_service/get_states
    responses to their waiting future, and streams state_changed events to MQTT.
    Do not add a second concurrent ws.recv() caller (see v2.0.1 fix)."""
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

        entity_id = publish_state(
            client, topic_root, alert_root, retain, enable_alerts, low_battery_threshold,
            new_state, domain_filter, entity_filter,
        )
        if entity_id:
            touched.add(entity_id)


# ============================================================
# Connection lifecycle
# ============================================================

async def _connection_loop(
    ws_url: str,
    ha_token: str,
    client: mqtt.Client,
    topic_root: str,
    alert_root: str,
    retain: bool,
    enable_alerts: bool,
    low_battery_threshold: Optional[float],
    domain_filter: Optional[re.Pattern],
    entity_filter: Optional[re.Pattern],
    enable_cmd: bool,
    cmd_queue: "asyncio.Queue[Tuple[str, str]]",
    ack_topic: str,
    err_topic: str,
    allow_domain: Optional[re.Pattern],
    allow_service: Optional[re.Pattern],
    allow_entity: Optional[re.Pattern],
    allow_area_device_label: bool,
    rate_limit_per_minute: int,
    dedup_window_s: int,
    max_payload_bytes: int,
    avail_topic: str,
):
    global HA_WS, HA_CONNECTED

    while True:
        consumer_task: Optional[asyncio.Task] = None
        recv_task: Optional[asyncio.Task] = None

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

                # Subscribe to state_changed BEFORE requesting the initial
                # snapshot, so no event occurring in between is ever missed.
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }))
                sub = json.loads(await ws.recv())
                if not (sub.get("id") == 1 and sub.get("success") is True):
                    raise RuntimeError(f"HA subscribe failed: {sub}")

                HA_CONNECTED = True
                logging.info("Connected to Home Assistant WS and subscribed to state_changed")

                # Entities touched by a live event before the snapshot finishes
                # publishing are skipped in the snapshot — the live event is
                # guaranteed to be at least as current.
                touched: set = set()
                recv_task = asyncio.create_task(
                    ha_recv_loop(
                        ws, client, topic_root, alert_root, retain, enable_alerts,
                        low_battery_threshold, domain_filter, entity_filter, touched,
                    )
                )

                if enable_cmd:
                    consumer_task = asyncio.create_task(
                        command_consumer(
                            client, cmd_queue, ack_topic, err_topic,
                            allow_domain, allow_service, allow_entity, allow_area_device_label,
                            rate_limit_per_minute, dedup_window_s, max_payload_bytes,
                        )
                    )

                try:
                    states = await ha_get_states()
                    published = 0
                    for st in states:
                        if st.get("entity_id") in touched:
                            continue
                        if publish_state(
                            client, topic_root, alert_root, retain, enable_alerts,
                            low_battery_threshold, st, domain_filter, entity_filter,
                        ):
                            published += 1
                    logging.info("Published initial snapshot of %d entities", published)
                except Exception as e:
                    logging.error("Initial state snapshot failed: %s", e)

                await recv_task

        except asyncio.CancelledError:
            raise
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
            for t in (recv_task, consumer_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except Exception:
                        pass


# ============================================================
# Zeroconf Self-Advertisement (optional)
# ============================================================

ZEROCONF_SERVICE_TYPE = "_meshmonitor-ha._tcp.local."


async def start_zeroconf_advertisement() -> Optional[Tuple[Any, Any]]:
    """Advertise a MeshMonitor instance over mDNS, if configured.

    MeshMonitor itself has no Zeroconf/mDNS support (verified against its
    source: no match for zeroconf/mdns/bonjour/avahi anywhere in the repo).
    The operator already runs this bridge alongside MeshMonitor, so the
    bridge is what advertises on its behalf -- letting the companion
    native Home Assistant integration's config flow discover host/port
    instead of the user typing them in by hand.

    Opt-in via MESHMONITOR_ADVERTISE=true; off by default since mDNS is
    unauthenticated LAN broadcast traffic some operators won't want. The
    API token is never advertised -- only host/port/scheme, which are not
    secret.

    Returns an opaque handle for stop_zeroconf_advertisement(), or None if
    advertisement is disabled or misconfigured (logged as a warning, not
    fatal -- the bridge's core HA<->MQTT function doesn't depend on this).
    """
    if not env_bool("MESHMONITOR_ADVERTISE", False):
        return None

    host = os.getenv("MESHMONITOR_HOST", "").strip()
    if not host:
        logging.warning(
            "MESHMONITOR_ADVERTISE is true but MESHMONITOR_HOST is not set; "
            "skipping mDNS advertisement"
        )
        return None

    try:
        import socket
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        logging.warning(
            "MESHMONITOR_ADVERTISE is true but the 'zeroconf' package is not "
            "installed; skipping mDNS advertisement"
        )
        return None

    port = env_int("MESHMONITOR_PORT", 8080)
    scheme = os.getenv("MESHMONITOR_SCHEME", "http").strip() or "http"
    instance_name = os.getenv("MESHMONITOR_INSTANCE_NAME", "").strip() or slug(host)

    try:
        address = socket.inet_aton(socket.gethostbyname(host))
    except OSError:
        logging.warning(
            "MESHMONITOR_ADVERTISE: could not resolve MESHMONITOR_HOST=%r; "
            "skipping mDNS advertisement", host
        )
        return None

    info = ServiceInfo(
        ZEROCONF_SERVICE_TYPE,
        f"{instance_name}.{ZEROCONF_SERVICE_TYPE}",
        addresses=[address],
        port=port,
        properties={"host": host, "port": str(port), "scheme": scheme},
    )

    azc = AsyncZeroconf()
    await azc.async_register_service(info)
    logging.info(
        "Advertising MeshMonitor at %s://%s:%s over mDNS as %s",
        scheme, host, port, info.name,
    )
    return (azc, info)


async def stop_zeroconf_advertisement(handle: Optional[Tuple[Any, Any]]) -> None:
    if handle is None:
        return
    azc, info = handle
    try:
        await azc.async_unregister_service(info)
    except Exception:
        pass
    finally:
        await azc.async_close()


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
    allow_area_device_label = env_bool("ALLOW_TARGET_AREA_DEVICE_LABEL", False)

    rate_limit_per_minute = env_int("COMMAND_RATE_LIMIT_PER_MINUTE", 60)
    dedup_window_s = env_int("COMMAND_DEDUP_WINDOW_SECONDS", 60)
    max_payload_bytes = env_int("MAX_COMMAND_PAYLOAD_BYTES", 8192)
    queue_maxsize = env_int("MQTT_CMD_QUEUE_MAXSIZE", 500)

    domain_filter = compile_optional_regex("STATE_DOMAIN_FILTER_REGEX")
    entity_filter = compile_optional_regex("STATE_ENTITY_FILTER_REGEX")

    low_battery_raw = env_int("LOW_BATTERY_THRESHOLD", 20)
    low_battery_threshold: Optional[float] = float(low_battery_raw) if low_battery_raw >= 0 else None

    # Heartbeat / availability topics + interval
    heartbeat_seconds = env_int("HEARTBEAT_SECONDS", 30)
    status_topic = os.getenv("STATUS_TOPIC", f"{topic_root}/status").strip()
    version_topic = os.getenv("VERSION_TOPIC", f"{topic_root}/version").strip()
    avail_topic = os.getenv("AVAILABILITY_TOPIC", f"{topic_root}/available").strip()

    cmd_queue: "asyncio.Queue[Tuple[str, str]]" = asyncio.Queue(maxsize=queue_maxsize)

    client = mqtt_connect(cmd_queue, enable_cmd, cmd_topic, avail_topic, queue_maxsize)

    # Start heartbeat (retained status/version)
    asyncio.create_task(heartbeat_task(client, heartbeat_seconds, status_topic, version_topic))

    zc_handle = await start_zeroconf_advertisement()

    logging.info("homeassistant-bridge v%s starting (ws=%s)", __version__, ws_url)

    shutdown_event = asyncio.Event()

    def _handle_signal(sig_name: str) -> None:
        logging.info("%s received, shutting down", sig_name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, _handle_signal, sig_name)
            except (NotImplementedError, RuntimeError):
                pass  # e.g. Windows, or signal already handled elsewhere

    main_task = asyncio.create_task(_connection_loop(
        ws_url, ha_token, client, topic_root, alert_root, retain, enable_alerts,
        low_battery_threshold, domain_filter, entity_filter,
        enable_cmd, cmd_queue, ack_topic, err_topic,
        allow_domain, allow_service, allow_entity, allow_area_device_label,
        rate_limit_per_minute, dedup_window_s, max_payload_bytes,
        avail_topic,
    ))
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    await asyncio.wait({main_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)

    if shutdown_event.is_set():
        main_task.cancel()
        try:
            await main_task
        except Exception:
            pass
        await stop_zeroconf_advertisement(zc_handle)
        try:
            client.publish(avail_topic, "offline", retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return 0

    shutdown_task.cancel()
    try:
        await main_task
    except Exception:
        pass
    await stop_zeroconf_advertisement(zc_handle)
    return 1


def main() -> int:
    try:
        return asyncio.run(run_bridge()) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
