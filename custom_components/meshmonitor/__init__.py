"""The MeshMonitor native Home Assistant integration.

Talks directly to a MeshMonitor instance's REST API (/api/v1/...). This is
a separate, optional alternative to the MQTT bridge script
(meshmonitor_bridge_homeassistant.py) at the repository root -- the two do
not share code or runtime state.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MeshMonitorApiClient
from .const import (
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SCHEME,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import MeshMonitorCoordinator

# Note: avoiding the PEP 695 `type X = ...` alias syntax here so this file
# stays parseable on the Python versions this repo's CI/tooling targets;
# entry.runtime_data is simply a MeshMonitorCoordinator at runtime.


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a MeshMonitor config entry: build the API client + coordinator."""
    session = async_get_clientsession(hass)
    client = MeshMonitorApiClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_API_TOKEN],
        scheme=entry.data[CONF_SCHEME],
    )

    scan_interval_seconds = entry.options.get(
        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
    )
    coordinator = MeshMonitorCoordinator(
        hass, entry, client, timedelta(seconds=scan_interval_seconds)
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a MeshMonitor config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options (e.g. scan interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)
