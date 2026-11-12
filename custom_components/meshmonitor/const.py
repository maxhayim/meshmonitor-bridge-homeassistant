"""Constants for the MeshMonitor native integration.

This integration is a separate, optional code path from the standalone
``meshmonitor_bridge_homeassistant.py`` MQTT bridge script shipped in this
same repository. It talks directly to MeshMonitor's REST API
(``/api/v1/...``) instead of MQTT, and is versioned independently
(see manifest.json's "version", unrelated to the bridge script's own
version number).
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "meshmonitor"

# -- Config entry / options keys ---------------------------------------
CONF_HOST = "host"
CONF_PORT = "port"
CONF_SCHEME = "scheme"
CONF_API_TOKEN = "api_token"
CONF_NAME = "name"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCHEME = "http"
DEFAULT_PORT = 8080
DEFAULT_NAME = "MeshMonitor"

# Polling interval, configurable via the options flow.
DEFAULT_SCAN_INTERVAL_SECONDS = 60
MIN_SCAN_INTERVAL_SECONDS = 15
DEFAULT_SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS)

# -- API ------------------------------------------------------------------
API_BASE_PATH = "/api/v1"
API_TOKEN_PREFIX = "mm_v1_"

# -- Platforms -------------------------------------------------------------
PLATFORMS = ["sensor", "binary_sensor"]

# -- Node protocol values (real, documented enum) --------------------------
PROTOCOL_MESHTASTIC = "meshtastic"
PROTOCOL_MESHCORE = "meshcore"

# -- Telemetry type values (real, documented enum) --------------------------
TELEMETRY_DEVICE_METRICS = "device_metrics"
TELEMETRY_ENVIRONMENT_METRICS = "environment_metrics"
TELEMETRY_POWER_METRICS = "power_metrics"
TELEMETRY_AIR_QUALITY_METRICS = "air_quality_metrics"

# Diagnostics: keys that must never be included verbatim.
REDACT_KEYS = {CONF_API_TOKEN, "authorization", "Authorization"}
