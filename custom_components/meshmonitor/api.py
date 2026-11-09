"""Async REST client for MeshMonitor's own HTTP API (/api/v1/...).

This talks directly to a MeshMonitor instance's REST API -- NOT the MQTT
bridge in meshmonitor_bridge_homeassistant.py at the repo root, which is a
completely separate, unrelated code path.

Endpoint list below is exactly what has been verified against the real
Yeraze/meshmonitor repository's docs/public/openapi.yaml. Do not add
endpoints here that aren't confirmed there (there is, for example, no
/health, /sources, /gateways, or /status endpoint -- connectivity is
inferred from a successful GET /api/v1/).
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import API_BASE_PATH

_LOGGER = logging.getLogger(__name__)


class MeshMonitorApiError(Exception):
    """Base error for all MeshMonitor API problems."""


class MeshMonitorAuthError(MeshMonitorApiError):
    """Raised on HTTP 401/403 -- the token is missing, wrong, or revoked.

    Callers (config flow, coordinator) should treat this distinctly from
    MeshMonitorConnectionError: a coordinator hitting this should trigger
    Home Assistant's reauth flow, not just retry on the next poll.
    """


class MeshMonitorConnectionError(MeshMonitorApiError):
    """Raised when the instance can't be reached at all (DNS, refused, timeout)."""


class MeshMonitorResponseError(MeshMonitorApiError):
    """Raised when the server responded but the payload wasn't the expected shape.

    Covers non-2xx status codes other than 401/403, and JSON that doesn't
    parse or isn't the type (dict/list) the caller expected -- i.e. "the
    server exists but didn't give us something we can use", as opposed to
    a pure connection failure.
    """


class MeshMonitorApiClient:
    """Thin async wrapper around MeshMonitor's /api/v1/ REST endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        api_token: str,
        scheme: str = "http",
    ) -> None:
        self._session = session
        self._base_url = f"{scheme}://{host}:{port}{API_BASE_PATH}"
        token = api_token.strip()
        # MeshMonitor tokens are generated with the "mm_v1_" prefix from its
        # own web UI (User Settings page). We don't validate the prefix
        # strictly here (a user could paste a differently-prefixed future
        # token format), we just send whatever they gave us as a Bearer token.
        self._headers = {"Authorization": f"Bearer {token}"}

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _request(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status in (401, 403):
                    raise MeshMonitorAuthError(
                        f"MeshMonitor rejected the API token (HTTP {resp.status}) for {url}"
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise MeshMonitorResponseError(
                        f"MeshMonitor returned HTTP {resp.status} for {url}: {body[:200]}"
                    )
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise MeshMonitorResponseError(
                        f"MeshMonitor response for {url} was not valid JSON: {err}"
                    ) from err
        except MeshMonitorApiError:
            raise
        except TimeoutError as err:
            raise MeshMonitorConnectionError(
                f"Timed out contacting MeshMonitor at {url}"
            ) from err
        except aiohttp.ClientError as err:
            raise MeshMonitorConnectionError(
                f"Could not reach MeshMonitor at {url}: {err}"
            ) from err

    async def async_get_info(self) -> Any:
        """GET /api/v1/ -- used as the connectivity/auth health check.

        MeshMonitor has no dedicated /health endpoint; a successful response
        here is treated as proof the host, port, scheme, and token are all
        valid.
        """
        return await self._request("/")

    async def async_get_nodes(self) -> list[dict[str, Any]]:
        """GET /api/v1/nodes."""
        data = await self._request("/nodes")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /nodes, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_node(self, node_id: str) -> dict[str, Any]:
        """GET /api/v1/nodes/{nodeId}."""
        return await self._request(f"/nodes/{node_id}")

    async def async_get_channels(self) -> list[dict[str, Any]]:
        """GET /api/v1/channels."""
        data = await self._request("/channels")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /channels, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_channel(self, channel_id: str) -> dict[str, Any]:
        """GET /api/v1/channels/{channelId}."""
        return await self._request(f"/channels/{channel_id}")

    async def async_get_telemetry(self) -> list[dict[str, Any]]:
        """GET /api/v1/telemetry."""
        data = await self._request("/telemetry")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /telemetry, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_telemetry_count(self) -> Any:
        """GET /api/v1/telemetry/count."""
        return await self._request("/telemetry/count")

    async def async_get_node_telemetry(self, node_id: str) -> list[dict[str, Any]]:
        """GET /api/v1/telemetry/{nodeId}."""
        data = await self._request(f"/telemetry/{node_id}")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /telemetry/{nodeId}, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_messages(self) -> list[dict[str, Any]]:
        """GET /api/v1/messages."""
        data = await self._request("/messages")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /messages, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_message(self, message_id: str) -> dict[str, Any]:
        """GET /api/v1/messages/{messageId}."""
        return await self._request(f"/messages/{message_id}")

    async def async_get_traceroutes(self) -> list[dict[str, Any]]:
        """GET /api/v1/traceroutes."""
        data = await self._request("/traceroutes")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /traceroutes, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_traceroute(self, from_node_id: str, to_node_id: str) -> Any:
        """GET /api/v1/traceroutes/{fromNodeId}/{toNodeId}."""
        return await self._request(f"/traceroutes/{from_node_id}/{to_node_id}")

    async def async_get_network(self) -> Any:
        """GET /api/v1/network."""
        return await self._request("/network")

    async def async_get_network_topology(self) -> Any:
        """GET /api/v1/network/topology."""
        return await self._request("/network/topology")

    async def async_get_packets(self) -> list[dict[str, Any]]:
        """GET /api/v1/packets."""
        data = await self._request("/packets")
        if not isinstance(data, list):
            raise MeshMonitorResponseError(
                "Expected a list from /packets, got "
                f"{type(data).__name__}"
            )
        return data

    async def async_get_packet(self, packet_id: str) -> dict[str, Any]:
        """GET /api/v1/packets/{id}."""
        return await self._request(f"/packets/{packet_id}")

    async def async_get_solar(self) -> Any:
        """GET /api/v1/solar."""
        return await self._request("/solar")

    async def async_get_solar_range(self) -> Any:
        """GET /api/v1/solar/range."""
        return await self._request("/solar/range")
