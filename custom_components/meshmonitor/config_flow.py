"""Config flow for the MeshMonitor native integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .api import (
    MeshMonitorApiClient,
    MeshMonitorAuthError,
    MeshMonitorConnectionError,
    MeshMonitorResponseError,
)
from .const import (
    CONF_API_TOKEN,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SCHEME,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_SCHEME,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def _make_unique_id(host: str, port: int, api_token: str) -> str:
    """Build a stable-ish unique_id for a config entry.

    MeshMonitor's API does not expose any documented stable instance
    identifier (no serial number / install ID field), so we cannot use
    anything the server itself hands us. The spec for this integration
    explicitly says not to use the bare host/IP as the unique_id (a host
    can be reassigned, and the user may want to track "this MeshMonitor
    install" across a host change).

    As a pragmatic stand-in, we hash (host, port, api_token) together.
    KNOWN LIMITATION: if the user rotates their API token on the
    MeshMonitor side (e.g. after a security event), this unique_id changes
    and Home Assistant will treat it as a new, separate instance rather
    than recognizing it as "the same MeshMonitor, new token". This is a
    documented tradeoff, not a bug: there is no other stable identifier to
    hash against today. A future phase could revisit this if MeshMonitor
    ever adds an instance-id field to GET /api/v1/.
    """
    raw = f"{host}:{port}:{api_token}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def _async_validate_connection(
    hass: Any, host: str, port: int, scheme: str, api_token: str
) -> None:
    """Call GET /api/v1/ to confirm host/port/scheme/token actually work.

    Raises MeshMonitorAuthError / MeshMonitorConnectionError /
    MeshMonitorResponseError on failure; callers translate those into
    config-flow error codes.
    """
    session = async_get_clientsession(hass)
    client = MeshMonitorApiClient(session, host, port, api_token, scheme=scheme)
    await client.async_get_info()


class MeshMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a single MeshMonitor instance."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None
        self._discovered_host: str | None = None
        self._discovered_port: int | None = None
        self._discovered_scheme: str = DEFAULT_SCHEME

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            scheme = user_input[CONF_SCHEME]
            api_token = user_input[CONF_API_TOKEN]
            name = user_input.get(CONF_NAME) or DEFAULT_NAME

            try:
                await _async_validate_connection(self.hass, host, port, scheme, api_token)
            except MeshMonitorAuthError:
                errors["base"] = "invalid_auth"
            except MeshMonitorConnectionError:
                errors["base"] = "cannot_connect"
            except MeshMonitorResponseError:
                errors["base"] = "unknown"
            else:
                unique_id = _make_unique_id(host, port, api_token)
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SCHEME: scheme,
                        CONF_API_TOKEN: api_token,
                        CONF_NAME: name,
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_SCHEME, default=DEFAULT_SCHEME): vol.In(
                    ["http", "https"]
                ),
                vol.Required(CONF_API_TOKEN): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a MeshMonitor instance discovered via the bridge script's
        optional mDNS self-advertisement (MeshMonitor itself has no
        Zeroconf support; see ``_meshmonitor-ha._tcp.local.`` in the
        bridge's ``start_zeroconf_advertisement()``).

        The TXT record's host/port/scheme are authoritative here, not the
        mDNS packet's source address -- the bridge advertises MeshMonitor's
        own connection details, which the operator configured explicitly,
        rather than whatever host happened to send the packet.
        """
        props = discovery_info.properties
        host = props.get("host")
        port_raw = props.get("port")
        scheme = props.get("scheme", DEFAULT_SCHEME)

        if not host or not port_raw:
            return self.async_abort(reason="invalid_discovery_info")

        try:
            port = int(port_raw)
        except ValueError:
            return self.async_abort(reason="invalid_discovery_info")

        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
                return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(f"zeroconf_{host}_{port}")
        self._abort_if_unique_id_configured()

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_scheme = scheme
        self.context["title_placeholders"] = {"host": host, "port": str(port)}

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the API token for a Zeroconf-discovered MeshMonitor instance.

        The mDNS advertisement never carries the API token (TXT records are
        plain, unauthenticated LAN broadcast) -- only host/port/scheme are
        pre-filled; the user still has to paste a token here.
        """
        errors: dict[str, str] = {}
        host = self._discovered_host
        port = self._discovered_port
        scheme = self._discovered_scheme
        assert host is not None and port is not None

        if user_input is not None:
            api_token = user_input[CONF_API_TOKEN]
            name = user_input.get(CONF_NAME) or DEFAULT_NAME

            try:
                await _async_validate_connection(self.hass, host, port, scheme, api_token)
            except MeshMonitorAuthError:
                errors["base"] = "invalid_auth"
            except MeshMonitorConnectionError:
                errors["base"] = "cannot_connect"
            except MeshMonitorResponseError:
                errors["base"] = "unknown"
            else:
                unique_id = _make_unique_id(host, port, api_token)
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SCHEME: scheme,
                        CONF_API_TOKEN: api_token,
                        CONF_NAME: name,
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_API_TOKEN): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=data_schema,
            description_placeholders={"host": host, "port": str(port)},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauth flow when the stored token stops working."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            new_token = user_input[CONF_API_TOKEN]
            try:
                await _async_validate_connection(
                    self.hass,
                    entry.data[CONF_HOST],
                    entry.data[CONF_PORT],
                    entry.data[CONF_SCHEME],
                    new_token,
                )
            except MeshMonitorAuthError:
                errors["base"] = "invalid_auth"
            except MeshMonitorConnectionError:
                errors["base"] = "cannot_connect"
            except MeshMonitorResponseError:
                errors["base"] = "unknown"
            else:
                new_data = {**entry.data, CONF_API_TOKEN: new_token}
                # NOTE: unique_id is derived from (host, port, token) and is
                # intentionally NOT recomputed/reassigned here. Home
                # Assistant's reauth flow updates the existing entry's data
                # in place; the entry's original unique_id stays as-is so
                # the entry itself is treated as "the same instance, token
                # refreshed" rather than spawning a duplicate entry.
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "MeshMonitorOptionsFlow":
        return MeshMonitorOptionsFlow(entry)


class MeshMonitorOptionsFlow(OptionsFlow):
    """Options flow: lets the user change the polling interval."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self._entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
        )
        data_schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL_SECONDS)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)
