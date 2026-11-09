"""DataUpdateCoordinator for the MeshMonitor native integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MeshMonitorApiClient,
    MeshMonitorAuthError,
    MeshMonitorConnectionError,
    MeshMonitorResponseError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Max number of *already-known* nodes whose telemetry is refetched per poll
# cycle, on top of any brand-new nodes (always refetched immediately). Caps
# per-cycle request count on a large mesh while still refreshing every
# node's telemetry over time via round robin (see _async_update_data).
TELEMETRY_REFRESH_BATCH_SIZE = 5


@dataclass
class MeshMonitorData:
    """Snapshot of the data polled from one MeshMonitor instance."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    channels: list[dict[str, Any]] = field(default_factory=list)
    network: dict[str, Any] = field(default_factory=dict)
    # node_id -> list of telemetry rows for that node. Populated lazily per
    # node (see MeshMonitorCoordinator._async_update_telemetry) rather than
    # blindly fetching /telemetry for every node every poll cycle, to avoid
    # hammering the instance on large meshes.
    telemetry_by_node: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class MeshMonitorCoordinator(DataUpdateCoordinator[MeshMonitorData]):
    """Polls a MeshMonitor instance's REST API on a configurable interval."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MeshMonitorApiClient,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=update_interval,
        )
        self.client = client
        self.entry = entry
        # Tracked manually (not part of the base DataUpdateCoordinator API)
        # so the "last update" sensor in sensor.py has a concrete timestamp
        # to show, since MeshMonitor has no /health endpoint to poll for one.
        self.last_success_time: datetime | None = None
        # node_id -> when its telemetry was last fetched; drives the
        # round-robin refresh in _async_update_data. Bookkeeping only, not
        # exposed data, so it lives on the coordinator rather than MeshMonitorData.
        self._telemetry_fetched_at: dict[str, datetime] = {}

    async def _async_update_data(self) -> MeshMonitorData:
        try:
            nodes = await self.client.async_get_nodes()
            channels = await self.client.async_get_channels()
            network = await self.client.async_get_network()
        except MeshMonitorAuthError as err:
            # Token stopped working (revoked/rotated on the MeshMonitor
            # side). DataUpdateCoordinator's base implementation catches
            # ConfigEntryAuthFailed and starts HA's reauth flow for this
            # config entry, rather than just failing silently on every poll.
            raise ConfigEntryAuthFailed(str(err)) from err
        except MeshMonitorConnectionError as err:
            raise UpdateFailed(f"Could not reach MeshMonitor: {err}") from err
        except MeshMonitorResponseError as err:
            raise UpdateFailed(f"Unexpected response from MeshMonitor: {err}") from err

        existing = self.data
        telemetry_by_node = dict(existing.telemetry_by_node) if existing else {}

        # Keep per-node telemetry fresh without issuing one request per node
        # on every single poll: brand-new nodes are always refetched
        # immediately, and a bounded batch of the stalest already-known
        # nodes is refetched each cycle (round robin), so every node's
        # telemetry keeps updating over time instead of freezing at its
        # first-ever reading.
        node_ids = {self._node_id(node) for node in nodes if self._node_id(node)}

        new_ids = [n for n in node_ids if n not in telemetry_by_node]
        known_ids_by_age = sorted(
            (n for n in node_ids if n in telemetry_by_node),
            key=lambda n: self._telemetry_fetched_at.get(n, dt_util.utcnow()),
        )
        to_refresh = new_ids + known_ids_by_age[:TELEMETRY_REFRESH_BATCH_SIZE]

        for node_id in to_refresh:
            try:
                telemetry_by_node[node_id] = await self.client.async_get_node_telemetry(
                    node_id
                )
                self._telemetry_fetched_at[node_id] = dt_util.utcnow()
            except MeshMonitorAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (MeshMonitorConnectionError, MeshMonitorResponseError) as err:
                _LOGGER.debug("Failed to fetch telemetry for node %s: %s", node_id, err)
                telemetry_by_node.setdefault(node_id, [])

        # Drop telemetry (and refresh bookkeeping) for nodes that no longer exist.
        for stale_id in list(telemetry_by_node):
            if stale_id not in node_ids:
                telemetry_by_node.pop(stale_id, None)
                self._telemetry_fetched_at.pop(stale_id, None)

        self.last_success_time = dt_util.utcnow()
        return MeshMonitorData(
            nodes=nodes,
            channels=channels,
            network=network if isinstance(network, dict) else {},
            telemetry_by_node=telemetry_by_node,
        )

    @staticmethod
    def _node_id(node: dict[str, Any]) -> str | None:
        # Per the verified schema, nodeId is the only stable join key for
        # every row (Meshtastic and MeshCore alike) -- nodeNum is synthetic
        # for MeshCore rows and must never be used as a key.
        node_id = node.get("nodeId")
        return str(node_id) if node_id is not None else None
