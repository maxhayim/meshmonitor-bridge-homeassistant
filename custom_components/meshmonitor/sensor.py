"""Sensor platform for the MeshMonitor native integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MeshMonitorCoordinator
from .entity import MeshMonitorHubEntity, MeshMonitorNodeEntity

_LOGGER = logging.getLogger(__name__)

# Fields confirmed on the Telemetry schema (docs/public/openapi.yaml in
# Yeraze/meshmonitor). The schema does NOT document which telemetryType
# each field belongs to (verified: no field-to-type mapping exists in the
# spec, only the telemetryType enum itself). Rather than guess a grouping
# that isn't documented, each node's sensors are built from whichever of
# these fields actually show up with a non-null value in that node's
# telemetry rows, regardless of which telemetryType row carried it. This
# also satisfies "don't create a sensor with a permanently-unknown state
# for telemetry a node never reports."
_TELEMETRY_FIELDS = (
    "batteryLevel",
    "voltage",
    "channelUtilization",
    "airUtilTx",
    "temperature",
    "relativeHumidity",
    "barometricPressure",
)


@dataclass(frozen=True, kw_only=True)
class MeshMonitorTelemetryDescription(SensorEntityDescription):
    """Describes one telemetry-field sensor."""

    value_fn: Callable[[dict[str, Any]], Any] | None = None


TELEMETRY_DESCRIPTIONS: dict[str, MeshMonitorTelemetryDescription] = {
    "batteryLevel": MeshMonitorTelemetryDescription(
        key="batteryLevel",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "voltage": MeshMonitorTelemetryDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "channelUtilization": MeshMonitorTelemetryDescription(
        key="channelUtilization",
        translation_key="channel_utilization",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
    ),
    "airUtilTx": MeshMonitorTelemetryDescription(
        key="airUtilTx",
        translation_key="air_util_tx",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:antenna",
    ),
    "temperature": MeshMonitorTelemetryDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "relativeHumidity": MeshMonitorTelemetryDescription(
        key="relativeHumidity",
        translation_key="relative_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "barometricPressure": MeshMonitorTelemetryDescription(
        key="barometricPressure",
        translation_key="barometric_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
    ),
}


def _latest_telemetry_values(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a node's telemetry rows to the latest non-null per field.

    Rows carry a mix of telemetryType values; a given field may appear on
    rows of more than one type (or only one). We don't filter by type (see
    module docstring) -- we just want each field's most recent known value.
    """
    values: dict[str, Any] = {}
    for row in sorted(rows, key=lambda r: r.get("timestamp") or 0):
        for field in _TELEMETRY_FIELDS:
            val = row.get(field)
            if val is not None:
                values[field] = val
    return values


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshMonitor sensors, including dynamically-added per-node ones."""
    coordinator: MeshMonitorCoordinator = entry.runtime_data

    async_add_entities(
        [
            MeshMonitorNodeCountSensor(coordinator, entry),
            MeshMonitorChannelCountSensor(coordinator, entry),
            MeshMonitorLastUpdateSensor(coordinator, entry),
        ]
    )

    known_keys: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_node_sensors() -> None:
        new_entities: list[SensorEntity] = []
        for node in coordinator.data.nodes:
            node_id = node.get("nodeId")
            if node_id is None:
                continue
            node_id = str(node_id)

            if node.get("snr") is not None and (node_id, "snr") not in known_keys:
                new_entities.append(MeshMonitorSnrSensor(coordinator, entry, node_id))
                known_keys.add((node_id, "snr"))

            latest_values = _latest_telemetry_values(
                coordinator.data.telemetry_by_node.get(node_id, [])
            )
            for field, value in latest_values.items():
                key = (node_id, field)
                if key in known_keys:
                    continue
                description = TELEMETRY_DESCRIPTIONS[field]
                new_entities.append(
                    MeshMonitorTelemetrySensor(coordinator, entry, node_id, description)
                )
                known_keys.add(key)

        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_node_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_node_sensors))


class MeshMonitorNodeCountSensor(MeshMonitorHubEntity, SensorEntity):
    """Number of nodes currently known to this MeshMonitor instance."""

    _attr_translation_key = "node_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "node_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.nodes)


class MeshMonitorChannelCountSensor(MeshMonitorHubEntity, SensorEntity):
    """Number of channels currently known to this MeshMonitor instance."""

    _attr_translation_key = "channel_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "channel_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.channels)


class MeshMonitorLastUpdateSensor(MeshMonitorHubEntity, SensorEntity):
    """Timestamp of the last successful poll.

    There is no /health endpoint on MeshMonitor, so this (and the
    connectivity binary_sensor in binary_sensor.py) are both purely derived
    from whether DataUpdateCoordinator's last scheduled refresh succeeded --
    not from any liveness signal MeshMonitor itself sends.
    """

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_update")

    @property
    def native_value(self):
        return self.coordinator.last_success_time


class MeshMonitorSnrSensor(MeshMonitorNodeEntity, SensorEntity):
    """SNR for one node -- a real, documented Node-level field.

    (rssi is deliberately NOT implemented as a per-node sensor: it only
    appears on packet/traceroute data in the verified schema, never on
    Node.)
    """

    _attr_translation_key = "snr"
    _attr_native_unit_of_measurement = "dB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:signal"

    def __init__(
        self, coordinator: MeshMonitorCoordinator, entry: ConfigEntry, node_id: str
    ) -> None:
        super().__init__(coordinator, entry, node_id, "snr")

    @property
    def native_value(self):
        node = self._current_node()
        return node.get("snr") if node else None


class MeshMonitorTelemetrySensor(MeshMonitorNodeEntity, SensorEntity):
    """One telemetry-field sensor for one node."""

    def __init__(
        self,
        coordinator: MeshMonitorCoordinator,
        entry: ConfigEntry,
        node_id: str,
        description: MeshMonitorTelemetryDescription,
    ) -> None:
        super().__init__(coordinator, entry, node_id, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        rows = self.coordinator.data.telemetry_by_node.get(self._node_id, [])
        values = _latest_telemetry_values(rows)
        return values.get(self.entity_description.key)
