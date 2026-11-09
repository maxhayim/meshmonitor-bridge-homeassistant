"""Shared base entity classes for the MeshMonitor native integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DOMAIN, PROTOCOL_MESHCORE, PROTOCOL_MESHTASTIC
from .coordinator import MeshMonitorCoordinator


def _hub_device_identifier(entry: ConfigEntry) -> tuple[str, str]:
    return (DOMAIN, entry.entry_id)


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Device info for the MeshMonitor instance itself (the "hub" device)."""
    name = entry.data.get(CONF_NAME) or entry.title
    return DeviceInfo(
        identifiers={_hub_device_identifier(entry)},
        name=name,
        manufacturer="MeshMonitor",
        model="MeshMonitor instance",
        entry_type="service",
    )


_PROTOCOL_LABELS = {
    PROTOCOL_MESHTASTIC: "Meshtastic",
    PROTOCOL_MESHCORE: "MeshCore",
}


def node_device_info(entry: ConfigEntry, node: dict[str, Any]) -> DeviceInfo:
    """Device info for one mesh node, grouped under the hub device.

    Per the verified Node schema, `protocol` is the real, documented field
    distinguishing Meshtastic from MeshCore rows -- it's used here both in
    the device name suffix and to pick a per-protocol model label, so the
    two kinds of nodes are visually distinguishable in the HA device list.
    """
    node_id = str(node.get("nodeId"))
    protocol = node.get("protocol")
    label = _PROTOCOL_LABELS.get(protocol, protocol or "unknown")
    long_name = node.get("longName") or node.get("shortName") or node_id
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{node_id}")},
        name=f"{long_name} ({label})",
        manufacturer="MeshMonitor",
        model=f"{label} node",
        via_device=_hub_device_identifier(entry),
    )


class MeshMonitorEntity(CoordinatorEntity[MeshMonitorCoordinator]):
    """Base entity tying availability to coordinator update success.

    There is no MeshMonitor /health endpoint, so "available" here means
    "the last scheduled poll of /nodes, /channels, /network succeeded" --
    i.e. CoordinatorEntity's own last_update_success, not any live
    connectivity signal pushed by the server.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MeshMonitorCoordinator,
        entry: ConfigEntry,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"


class MeshMonitorHubEntity(MeshMonitorEntity):
    """Base for entities describing the MeshMonitor instance as a whole."""

    def __init__(
        self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry, unique_id_suffix: str
    ) -> None:
        super().__init__(coordinator, entry, unique_id_suffix)
        self._attr_device_info = hub_device_info(entry)


class MeshMonitorNodeEntity(MeshMonitorEntity):
    """Base for entities describing one mesh node."""

    def __init__(
        self,
        coordinator: MeshMonitorCoordinator,
        entry: ConfigEntry,
        node_id: str,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, f"{node_id}_{unique_id_suffix}")
        self._node_id = node_id

    def _current_node(self) -> dict[str, Any] | None:
        for node in self.coordinator.data.nodes:
            if str(node.get("nodeId")) == self._node_id:
                return node
        return None

    @property
    def device_info(self) -> DeviceInfo | None:
        node = self._current_node()
        if node is None:
            return None
        return node_device_info(self._entry, node)

    @property
    def available(self) -> bool:
        # Tied to coordinator success AND the node still being present in
        # the latest poll (a node that's dropped out of /nodes shouldn't
        # keep reporting stale sensor values as "available").
        return super().available and self._current_node() is not None
