"""Tests for custom_components/meshmonitor/api.py.

Scope: this exercises MeshMonitorApiClient's request/response/error
handling against mocked HTTP responses (via aioresponses), with no real
MeshMonitor instance and no Home Assistant core involved -- api.py itself
has no homeassistant.* imports, so it can be tested in isolation.

NOT covered here (deferred / out of scope for this test file):
- config_flow.py / coordinator.py / __init__.py wiring against a real HA
  core instance (would need pytest-homeassistant-custom-component).
- entity.py / sensor.py / binary_sensor.py entity behavior.
See the Phase 2 report for what that implies is still untested.
"""

import sys
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
import yarl
from aioresponses import aioresponses

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.meshmonitor.api import (
    MeshMonitorApiClient,
    MeshMonitorAuthError,
    MeshMonitorConnectionError,
    MeshMonitorResponseError,
)

BASE = "http://192.0.2.10:8080/api/v1"


@pytest_asyncio.fixture
async def client():
    async with aiohttp.ClientSession() as session:
        yield MeshMonitorApiClient(
            session, "192.0.2.10", 8080, "mm_v1_testtoken", scheme="http"
        )


@pytest.mark.asyncio
async def test_async_get_info_success(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", payload={"version": "1.2.3"})
        result = await client.async_get_info()
        assert result == {"version": "1.2.3"}


@pytest.mark.asyncio
async def test_authorization_header_sent(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", payload={"version": "1.2.3"})
        await client.async_get_info()
        request = m.requests[("GET", yarl.URL(f"{BASE}/"))][0]
        assert request.kwargs["headers"]["Authorization"] == "Bearer mm_v1_testtoken"


@pytest.mark.asyncio
async def test_async_get_nodes_success(client):
    nodes_payload = [
        {"nodeId": "!a1b2c3d4", "protocol": "meshtastic", "snr": 8.5},
        {"nodeId": "mc:deadbeef", "protocol": "meshcore"},
    ]
    with aioresponses() as m:
        m.get(f"{BASE}/nodes", payload=nodes_payload)
        result = await client.async_get_nodes()
        assert result == nodes_payload


@pytest.mark.asyncio
async def test_async_get_nodes_wrong_shape_raises_response_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/nodes", payload={"not": "a list"})
        with pytest.raises(MeshMonitorResponseError):
            await client.async_get_nodes()


@pytest.mark.asyncio
async def test_async_get_channels_success(client):
    with aioresponses() as m:
        m.get(f"{BASE}/channels", payload=[{"channelId": 0, "name": "LongFast"}])
        result = await client.async_get_channels()
        assert result[0]["name"] == "LongFast"


@pytest.mark.asyncio
async def test_401_raises_auth_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", status=401)
        with pytest.raises(MeshMonitorAuthError):
            await client.async_get_info()


@pytest.mark.asyncio
async def test_403_raises_auth_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", status=403)
        with pytest.raises(MeshMonitorAuthError):
            await client.async_get_info()


@pytest.mark.asyncio
async def test_500_raises_response_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", status=500, body="internal error")
        with pytest.raises(MeshMonitorResponseError):
            await client.async_get_info()


@pytest.mark.asyncio
async def test_404_raises_response_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/nodes/doesnotexist", status=404, body="not found")
        with pytest.raises(MeshMonitorResponseError):
            await client.async_get_node("doesnotexist")


@pytest.mark.asyncio
async def test_connection_error_raises_connection_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/", exception=aiohttp.ClientConnectionError("refused"))
        with pytest.raises(MeshMonitorConnectionError):
            await client.async_get_info()


@pytest.mark.asyncio
async def test_invalid_json_raises_response_error(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/",
            body="not json at all",
            content_type="application/json",
        )
        with pytest.raises(MeshMonitorResponseError):
            await client.async_get_info()


@pytest.mark.asyncio
async def test_node_telemetry_endpoint(client):
    telemetry_payload = [
        {
            "nodeId": "!a1b2c3d4",
            "timestamp": 1000,
            "telemetryType": "device_metrics",
            "batteryLevel": 87,
            "voltage": 4.01,
        },
        {
            "nodeId": "!a1b2c3d4",
            "timestamp": 1010,
            "telemetryType": "environment_metrics",
            "temperature": 21.4,
            "relativeHumidity": 55.0,
        },
    ]
    with aioresponses() as m:
        # "!" is an RFC 3986 sub-delim, valid unencoded in a URL path
        # segment, so yarl/aiohttp send it through as a literal "!" rather
        # than percent-encoding it -- the mock URL below matches that.
        m.get(f"{BASE}/telemetry/!a1b2c3d4", payload=telemetry_payload)
        result = await client.async_get_node_telemetry("!a1b2c3d4")
        assert len(result) == 2
        assert result[0]["batteryLevel"] == 87


@pytest.mark.asyncio
async def test_network_topology_endpoint(client):
    with aioresponses() as m:
        m.get(f"{BASE}/network/topology", payload={"edges": [], "nodes": []})
        result = await client.async_get_network_topology()
        assert result == {"edges": [], "nodes": []}


@pytest.mark.asyncio
async def test_solar_range_endpoint(client):
    with aioresponses() as m:
        m.get(f"{BASE}/solar/range", payload={"start": 0, "end": 1})
        result = await client.async_get_solar_range()
        assert result == {"start": 0, "end": 1}
