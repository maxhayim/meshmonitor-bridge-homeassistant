"""Config flow tests, using pytest-homeassistant-custom-component's hass fixture.

Focused on the Zeroconf discovery path added alongside the bridge script's
``start_zeroconf_advertisement()``: the mDNS TXT record carries host/port/
scheme (never the API token -- that's unauthenticated LAN broadcast), so the
discovery flow must pre-fill those from the TXT properties and still prompt
for a token before creating the entry.
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.meshmonitor.const import CONF_API_TOKEN, CONF_HOST, CONF_PORT, DOMAIN

pytestmark = pytest.mark.asyncio


def _discovery_info(host="192.0.2.10", port=8080, scheme="http"):
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

    ip = ipaddress.ip_address(host)
    return ZeroconfServiceInfo(
        ip_address=ip,
        ip_addresses=[ip],
        port=port,
        hostname="meshmonitor-bridge.local.",
        type="_meshmonitor-ha._tcp.local.",
        name="meshmonitor-bridge._meshmonitor-ha._tcp.local.",
        properties={"host": host, "port": str(port), "scheme": scheme},
    )


async def test_zeroconf_discovery_prefills_host_and_port_then_asks_for_token(hass, enable_custom_integrations):
    with patch(
        "custom_components.meshmonitor.config_flow._async_validate_connection",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.meshmonitor.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_discovery_info(),
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "zeroconf_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_TOKEN: "mm_v1_discoveredtoken"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.0.2.10"
    assert result["data"][CONF_PORT] == 8080


async def test_zeroconf_discovery_never_prompts_for_host_or_port(hass, enable_custom_integrations):
    with patch(
        "custom_components.meshmonitor.config_flow._async_validate_connection",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_discovery_info(),
        )

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_HOST not in schema_keys
    assert CONF_PORT not in schema_keys
    assert CONF_API_TOKEN in schema_keys


async def test_zeroconf_discovery_aborts_if_host_port_already_configured(hass, enable_custom_integrations):
    with patch(
        "custom_components.meshmonitor.config_flow._async_validate_connection",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.meshmonitor.async_setup_entry", return_value=True
    ):
        first = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_discovery_info(host="192.0.2.20"),
        )
        await hass.config_entries.flow.async_configure(
            first["flow_id"], {CONF_API_TOKEN: "mm_v1_firsttoken"}
        )
        await hass.async_block_till_done()

        second = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_discovery_info(host="192.0.2.20"),
        )

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_zeroconf_discovery_rejects_missing_properties(hass, enable_custom_integrations):
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

    ip = ipaddress.ip_address("192.0.2.30")
    bad_info = ZeroconfServiceInfo(
        ip_address=ip,
        ip_addresses=[ip],
        port=8080,
        hostname="meshmonitor-bridge.local.",
        type="_meshmonitor-ha._tcp.local.",
        name="meshmonitor-bridge._meshmonitor-ha._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=bad_info,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"
