"""Constants for Tuya Orchestrator."""
from __future__ import annotations

DOMAIN = "tuya_orchestrator"

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------
CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_PROTOCOL_VERSION = "protocol_version"
CONF_PRODUCT_ID = "product_id"
CONF_PROFILE_NAME = "profile_name"
CONF_PROFILE_YAML = "profile_yaml"
CONF_SCAN_INTERVAL = "scan_interval"

# Distinguishes the two kinds of ConfigEntry this integration creates: one
# "account" entry (Tuya Cloud credentials, no device of its own - runs a
# background poller that triggers a discovery flow per device found) and
# one "device" entry per paired device (the actual LAN-connected entity).
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_ACCOUNT = "account"
ENTRY_TYPE_DEVICE = "device"
CONF_ACCOUNT_ENTRY_ID = "account_entry_id"  # stashed on discovery_info to trace back to its account

# Cloud linking (used only during config_flow to fetch local_keys; never
# stored/used again after setup - the running integration is 100% LAN).
CONF_REGION = "region"
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_UID = "uid"

DEFAULT_PROTOCOL_VERSION = "3.3"
SUPPORTED_PROTOCOL_VERSIONS = ["3.1", "3.3", "3.4"]
DEFAULT_SCAN_INTERVAL = 30  # seconds; fallback poll, LAN push is the primary path
DEFAULT_PORT = 6668

# ---------------------------------------------------------------------------
# Tuya Cloud API regions (official OpenAPI endpoints, documented at
# https://developer.tuya.com/en/docs/iot/api-request)
# ---------------------------------------------------------------------------
TUYA_REGIONS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}

# ---------------------------------------------------------------------------
# LAN UDP discovery (well-documented Tuya broadcast ports/keys, used by the
# reference open-source implementations tinytuya/localtuya).
# ---------------------------------------------------------------------------
UDP_PORT_UNENCRYPTED = 6666
UDP_PORT_ENCRYPTED = 6667
UDP_KEY_ENCRYPTED = b"yGAdlopoPVLdABfn"  # fixed, published broadcast key
DISCOVERY_TIMEOUT = 8
DISCOVERY_POLL_INTERVAL = 300  # seconds; how often an "account" entry re-scans for new devices

# ---------------------------------------------------------------------------
# Profile / entity mapping
# ---------------------------------------------------------------------------
PLATFORMS = ["sensor", "switch", "number", "binary_sensor", "select", "light", "climate", "vacuum"]

DP_TYPE_TO_PLATFORM = {
    "switch": "switch",
    "sensor": "sensor",
    "number": "number",
    "binary_sensor": "binary_sensor",
    "select": "select",
}
