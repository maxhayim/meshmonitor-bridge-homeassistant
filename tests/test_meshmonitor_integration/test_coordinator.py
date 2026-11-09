"""Coordinator tests, using pytest-homeassistant-custom-component's hass fixture.

Focused on the telemetry refresh/staleness behavior: a node's telemetry must
not freeze at its first-ever reading (the bug this test guards against), and
auth failures must surface as HA's own ConfigEntryAuthFailed so built-in
reauth kicks in.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.meshmonitor.api import MeshMonitorAuthError, MeshMonitorConnectionError
from custom_components.meshmonitor.coordinator import (
    TELEMETRY_REFRESH_BATCH_SIZE,
    MeshMonitorCoordinator,
)

pytestmark = pytest.mark.asyncio


def _make_client(nodes, telemetry_batch):
    """telemetry_batch: dict of node_id -> rows, used until the test swaps it."""
    client = MagicMock()
    client.async_get_nodes = AsyncMock(return_value=nodes)
    client.async_get_channels = AsyncMock(return_value=[])
    client.async_get_network = AsyncMock(return_value={})
    client.async_get_node_telemetry = AsyncMock(
        side_effect=lambda node_id: telemetry_batch.get(node_id, [])
    )
    return client


async def test_new_node_telemetry_fetched_immediately(hass):
    nodes = [{"nodeId": "a", "snr": 1.0}]
    client = _make_client(nodes, {"a": [{"timestamp": 1, "batteryLevel": 80}]})
    entry = MagicMock(entry_id="e1")

    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))
    data = await coordinator._async_update_data()

    assert data.telemetry_by_node["a"] == [{"timestamp": 1, "batteryLevel": 80}]


async def test_known_node_telemetry_is_refreshed_not_frozen(hass):
    # Regression guard: a node already present on poll 1 must still pick up
    # a changed reading on poll 2 (within the refresh batch), not keep
    # returning its first-ever value forever.
    nodes = [{"nodeId": "a", "snr": 1.0}]
    client = _make_client(nodes, {"a": [{"timestamp": 1, "batteryLevel": 80}]})
    entry = MagicMock(entry_id="e1")
    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))

    first = await coordinator._async_update_data()
    coordinator.data = first
    assert first.telemetry_by_node["a"][0]["batteryLevel"] == 80

    # Simulate the node reporting a new reading before the next poll.
    client.async_get_node_telemetry = AsyncMock(
        side_effect=lambda node_id: [{"timestamp": 2, "batteryLevel": 55}]
    )
    second = await coordinator._async_update_data()
    assert second.telemetry_by_node["a"][0]["batteryLevel"] == 55


async def test_refresh_batch_size_caps_known_node_requests_per_cycle(hass):
    node_count = TELEMETRY_REFRESH_BATCH_SIZE + 3
    nodes = [{"nodeId": f"n{i}"} for i in range(node_count)]
    first_batch = {f"n{i}": [{"timestamp": 1, "batteryLevel": 50}] for i in range(node_count)}
    client = _make_client(nodes, first_batch)
    entry = MagicMock(entry_id="e1")
    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))

    first = await coordinator._async_update_data()
    coordinator.data = first
    assert client.async_get_node_telemetry.call_count == node_count  # all new nodes fetched

    client.async_get_node_telemetry.reset_mock()
    await coordinator._async_update_data()
    # Only the capped batch of already-known nodes refetched, not all of them again.
    assert client.async_get_node_telemetry.call_count == TELEMETRY_REFRESH_BATCH_SIZE


async def test_stale_node_telemetry_is_dropped(hass):
    client = _make_client([{"nodeId": "a"}], {"a": [{"timestamp": 1, "batteryLevel": 80}]})
    entry = MagicMock(entry_id="e1")
    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))

    first = await coordinator._async_update_data()
    coordinator.data = first
    assert "a" in first.telemetry_by_node

    client.async_get_nodes = AsyncMock(return_value=[])  # node "a" vanished
    second = await coordinator._async_update_data()
    assert "a" not in second.telemetry_by_node


async def test_auth_error_raises_config_entry_auth_failed(hass):
    client = MagicMock()
    client.async_get_nodes = AsyncMock(side_effect=MeshMonitorAuthError("bad token"))
    entry = MagicMock(entry_id="e1")
    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_raises_update_failed(hass):
    client = MagicMock()
    client.async_get_nodes = AsyncMock(side_effect=MeshMonitorConnectionError("down"))
    entry = MagicMock(entry_id="e1")
    coordinator = MeshMonitorCoordinator(hass, entry, client, timedelta(seconds=60))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
