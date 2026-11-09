"""Binary sensor platform for the MeshMonitor native integration.

Deliberately thin: MeshMonitor's verified API surface has no per-node
boolean-ish field to back a real binary_sensor (nothing speculative is
built here). The only binary_sensor this integration ships is the
instance-level connectivity state, which is itself derived from
coordinator update success rather than any field MeshMonitor reports.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MeshMonitorCoordinator
from .entity import MeshMonitorHubEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MeshMonitorCoordinator = entry.runtime_data
    async_add_entities([MeshMonitorConnectivitySensor(coordinator, entry)])


class MeshMonitorConnectivitySensor(MeshMonitorHubEntity, BinarySensorEntity):
    """Whether the last scheduled poll of this MeshMonitor instance succeeded.

    Not a real "is MeshMonitor reachable right now" push signal -- there is
    no /health endpoint, so this reflects
    DataUpdateCoordinator.last_update_success as of the most recent poll.
    """

    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    # Always report True for "available": this entity's whole job is to
    # show failure state, so it must stay visible even when polling fails
    # (unlike node entities/other sensors, which go unavailable together
    # with the coordinator).
    _attr_available = True

    def __init__(self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "connectivity")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
