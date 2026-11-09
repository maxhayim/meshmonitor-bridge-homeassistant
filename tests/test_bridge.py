import asyncio
import json
import re
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import meshmonitor_bridge_homeassistant as bridge


# ============================================================
# Command target extraction / allowlist
# ============================================================

ALLOW_DOMAIN = re.compile(r"^(light|switch)$", re.I)
ALLOW_SERVICE = re.compile(r"^(turn_on|turn_off)$", re.I)
ALLOW_ENTITY = re.compile(r"^(light\.|switch\.)", re.I)


def test_extract_targets_simple_entity():
    ids, bad = bridge.extract_command_targets({"entity_id": "light.porch"}, None, False)
    assert ids == ["light.porch"]
    assert bad is False


def test_extract_targets_validates_every_list_entry():
    ids, bad = bridge.extract_command_targets({"entity_id": ["light.porch", 123]}, None, False)
    assert bad is True  # non-string entry flags the whole command, not just index 0


def test_extract_targets_area_device_label_denied_by_default():
    for key in ("area_id", "device_id", "label_id"):
        ids, bad = bridge.extract_command_targets({}, {key: "x"}, False)
        assert bad is True

    ids, bad = bridge.extract_command_targets({}, {"area_id": "x"}, True)
    assert bad is False  # explicit opt-in


def test_is_allowed_requires_entity_allowlist_default_deny():
    ids, bad = bridge.extract_command_targets({"entity_id": "light.porch"}, None, False)
    assert bridge.is_allowed("light", "turn_on", ids, bad, ALLOW_DOMAIN, ALLOW_SERVICE, None) is False


def test_is_allowed_checks_every_entity_in_a_list():
    ids, bad = bridge.extract_command_targets(
        {"entity_id": ["light.porch", "climate.furnace"]}, None, False
    )
    assert bridge.is_allowed("light", "turn_on", ids, bad, ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY) is False


def test_is_allowed_rejects_disallowed_target():
    ids, bad = bridge.extract_command_targets({}, {"area_id": "living_room"}, False)
    assert bridge.is_allowed("light", "turn_on", ids, bad, ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY) is False


def test_is_allowed_domain_fullmatch_rejects_substring():
    loose_domain = re.compile(r"light", re.I)  # unanchored
    ids, bad = bridge.extract_command_targets({"entity_id": "lighting_rig.1"}, None, False)
    assert bridge.is_allowed(
        "lighting_rig", "turn_on", ids, bad, loose_domain, ALLOW_SERVICE, ALLOW_ENTITY
    ) is False


def test_is_allowed_entity_prefix_match_still_works():
    # Regression guard: ALLOW_ENTITY_REGEX is documented/shipped as a prefix
    # pattern with no trailing "$" — it must stay a search(), not a fullmatch().
    ids, bad = bridge.extract_command_targets({"entity_id": "light.porch"}, None, False)
    assert bridge.is_allowed("light", "turn_on", ids, bad, ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY) is True


# ============================================================
# State normalization / alert classification
# ============================================================

def test_normalize_state_basic():
    norm = bridge.normalize_state({
        "entity_id": "light.porch",
        "state": "on",
        "attributes": {"friendly_name": "Porch Light", "device_class": "light"},
    })
    assert norm["domain"] == "light"
    assert norm["name"] == "Porch Light"


def test_classify_alert_motion():
    norm = {"domain": "binary_sensor", "attributes_min": {"device_class": "motion"}, "state": "on", "name": "Hall"}
    alert = bridge.classify_alert("binary_sensor.hall", norm)
    assert alert[0] == "motion"


def test_check_low_battery_crossing_suppression_recovery():
    bridge.LOW_BATTERY_STATE.clear()

    def ns(v):
        return {"domain": "sensor", "attributes_min": {"device_class": "battery"}, "state": v, "name": "n"}

    assert bridge.check_low_battery("sensor.x", ns("25"), 20) is None
    assert bridge.check_low_battery("sensor.x", ns("15"), 20) is not None  # crosses -> fires
    assert bridge.check_low_battery("sensor.x", ns("10"), 20) is None  # still low -> suppressed
    assert bridge.check_low_battery("sensor.x", ns("30"), 20) is None  # recovers silently
    assert bridge.check_low_battery("sensor.x", ns("5"), 20) is not None  # crosses again -> fires
    assert bridge.check_low_battery("sensor.x", ns("unavailable"), 20) is None  # malformed -> no crash


def test_check_low_battery_ignores_non_battery_sensor():
    norm = {"domain": "sensor", "attributes_min": {"device_class": "temperature"}, "state": "5", "name": "n"}
    assert bridge.check_low_battery("sensor.x", norm, 20) is None


# ============================================================
# MQTT thread-safety (on_message runs on paho's network thread)
# ============================================================

class FakeMqttClient:
    """Stands in for paho.mqtt.client.Client without touching a real broker."""

    def __init__(self, *_args, **_kwargs):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.published = []
        self.subscriptions = []
        self.will = None

    def username_pw_set(self, *_a, **_k):
        pass

    def tls_set(self, *_a, **_k):
        pass

    def will_set(self, topic, payload=None, retain=False):
        self.will = (topic, payload, retain)

    def subscribe(self, topic, *_a, **_k):
        self.subscriptions.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def connect(self, *_a, **_k):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_on_message_is_thread_safe_and_resubscribes_on_reconnect(monkeypatch):
    fake_client_holder = {}

    def fake_ctor(*args, **kwargs):
        c = FakeMqttClient(*args, **kwargs)
        fake_client_holder["client"] = c
        return c

    monkeypatch.setattr(bridge.mqtt, "Client", fake_ctor)

    cmd_queue: "asyncio.Queue" = asyncio.Queue(maxsize=10)
    client = bridge.mqtt_connect(cmd_queue, True, "meshmonitor/homeassistant/cmd/service", "meshmonitor/homeassistant/available", 10)

    # LWT registered before connect
    assert client.will == ("meshmonitor/homeassistant/available", "offline", True)

    # Simulate a (re)connect — cmd topic must be (re)subscribed each time, not just once at startup
    client.on_connect(client, None, None, 0, None)
    assert "meshmonitor/homeassistant/cmd/service" in client.subscriptions
    assert ("meshmonitor/homeassistant/available", "online", True) in client.published

    class FakeMsg:
        topic = "meshmonitor/homeassistant/cmd/service"
        payload = b'{"request_id": "t1"}'

    # on_message fires from paho's own network thread, not the event loop thread.
    def deliver_from_thread():
        client.on_message(client, None, FakeMsg())

    t = threading.Thread(target=deliver_from_thread)
    t.start()
    t.join(timeout=2)

    topic, payload = await asyncio.wait_for(cmd_queue.get(), timeout=2)
    assert topic == "meshmonitor/homeassistant/cmd/service"
    assert json.loads(payload)["request_id"] == "t1"


# ============================================================
# WebSocket single-reader dispatch (call_service/get_states vs state_changed)
# ============================================================

class FakeWS:
    def __init__(self, messages):
        self._messages = list(messages)

    async def recv(self):
        if not self._messages:
            # Block forever once drained, like a real idle websocket.
            await asyncio.Event().wait()
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_ha_recv_loop_dispatches_response_and_streams_event():
    bridge.HA_PENDING.clear()
    fut = asyncio.get_event_loop().create_future()
    bridge.HA_PENDING[42] = fut

    state_event = json.dumps({
        "type": "event",
        "event": {"data": {"new_state": {
            "entity_id": "light.porch", "state": "on",
            "attributes": {"friendly_name": "Porch"},
        }}},
    })
    response = json.dumps({"id": 42, "type": "result", "success": True, "result": []})

    ws = FakeWS([response, state_event])
    client = FakeMqttClient()
    touched = set()

    task = asyncio.create_task(bridge.ha_recv_loop(
        ws, client, "meshmonitor/homeassistant", "meshmonitor/alerts/homeassistant",
        False, True, None, None, None, touched,
    ))

    resp = await asyncio.wait_for(fut, timeout=2)
    assert resp["success"] is True

    # give the loop a tick to process the second (event) message
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "light.porch" in touched
    topics = [t for t, _, _ in client.published]
    assert "meshmonitor/homeassistant/light/light-porch/state" in topics


@pytest.mark.asyncio
async def test_ha_get_states_uses_single_reader_dispatch():
    bridge.HA_PENDING.clear()

    sent = []

    class FakeHaWs:
        async def send(self, msg):
            sent.append(json.loads(msg))

    bridge.HA_WS = FakeHaWs()

    async def respond_once_sent():
        while not sent:
            await asyncio.sleep(0.01)
        msg_id = sent[0]["id"]
        fut = bridge.HA_PENDING[msg_id]
        fut.set_result({"id": msg_id, "success": True, "result": [{"entity_id": "light.porch"}]})

    responder = asyncio.create_task(respond_once_sent())
    states = await bridge.ha_get_states()
    await responder

    assert states == [{"entity_id": "light.porch"}]
    assert sent[0]["type"] == "get_states"
    bridge.HA_WS = None


# ============================================================
# command_consumer: payload size / dedup / rate limit / happy path
# ============================================================

@pytest.mark.asyncio
async def test_command_consumer_enforces_payload_size_limit():
    client = FakeMqttClient()
    cmd_queue: "asyncio.Queue" = asyncio.Queue()
    await cmd_queue.put(("t", json.dumps({"request_id": "r1", "domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.porch" * 1000}})))

    task = asyncio.create_task(bridge.command_consumer(
        client, cmd_queue, "ack", "err", ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY, False,
        rate_limit_per_minute=0, dedup_window_s=0, max_payload_bytes=100,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    err_payloads = [json.loads(p) for t, p, _ in client.published if t == "err"]
    assert any("exceeds" in e["error"] for e in err_payloads)


@pytest.mark.asyncio
async def test_command_consumer_dedup_rejects_repeat_request_id():
    client = FakeMqttClient()
    cmd_queue: "asyncio.Queue" = asyncio.Queue()
    cmd = {"request_id": "dup1", "domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.porch"}}

    bridge.HA_WS = object()  # truthy sentinel; ha_call_service only checks "is None"
    bridge.HA_PENDING.clear()

    async def fake_ha_call_service(domain, service, service_data, target=None):
        return {"success": True}

    orig = bridge.ha_call_service
    bridge.ha_call_service = fake_ha_call_service
    try:
        await cmd_queue.put(("t", json.dumps(cmd)))
        await cmd_queue.put(("t", json.dumps(cmd)))  # duplicate request_id

        task = asyncio.create_task(bridge.command_consumer(
            client, cmd_queue, "ack", "err", ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY, False,
            rate_limit_per_minute=0, dedup_window_s=60, max_payload_bytes=0,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        bridge.ha_call_service = orig
        bridge.HA_WS = None

    acks = [json.loads(p) for t, p, _ in client.published if t == "ack"]
    errs = [json.loads(p) for t, p, _ in client.published if t == "err"]
    assert len(acks) == 1
    assert any("Duplicate" in e["error"] for e in errs)


@pytest.mark.asyncio
async def test_command_consumer_rate_limit():
    client = FakeMqttClient()
    cmd_queue: "asyncio.Queue" = asyncio.Queue()

    bridge.HA_WS = object()

    async def fake_ha_call_service(domain, service, service_data, target=None):
        return {"success": True}

    orig = bridge.ha_call_service
    bridge.ha_call_service = fake_ha_call_service
    try:
        for i in range(3):
            cmd = {"request_id": f"r{i}", "domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.porch"}}
            await cmd_queue.put(("t", json.dumps(cmd)))

        task = asyncio.create_task(bridge.command_consumer(
            client, cmd_queue, "ack", "err", ALLOW_DOMAIN, ALLOW_SERVICE, ALLOW_ENTITY, False,
            rate_limit_per_minute=1, dedup_window_s=0, max_payload_bytes=0,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        bridge.ha_call_service = orig
        bridge.HA_WS = None

    acks = [json.loads(p) for t, p, _ in client.published if t == "ack"]
    errs = [json.loads(p) for t, p, _ in client.published if t == "err"]
    assert len(acks) == 1
    assert any("rate limit" in e["error"] for e in errs)
