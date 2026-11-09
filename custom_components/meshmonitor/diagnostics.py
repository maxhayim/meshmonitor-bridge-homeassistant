"""Diagnostics support for the MeshMonitor native integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN

# Never include the raw API token (or anything that looks like an
# Authorization header value) in diagnostics output.
TO_REDACT = {CONF_API_TOKEN, "authorization", "Authorization"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry: redacted config + last poll data."""
    coordinator = entry.runtime_data

    redacted_entry_data = async_redact_data(dict(entry.data), TO_REDACT)
    redacted_entry_options = async_redact_data(dict(entry.options), TO_REDACT)

    coordinator_data = asdict(coordinator.data) if coordinator.data else None

    return {
        "entry_data": redacted_entry_data,
        "entry_options": redacted_entry_options,
        "last_update_success": coordinator.last_update_success,
        "last_success_time": (
            coordinator.last_success_time.isoformat()
            if coordinator.last_success_time
            else None
        ),
        "coordinator_data": coordinator_data,
    }
