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

# hass.data[DOMAIN][DISCOVERY_DATA_KEY] holds the single, domain-wide
# PersistentDiscovery instance started once in async_setup() - see
# __init__.py/discovery.py. Not an entry_id (those are HA-generated UUIDs),
# so no collision with the per-entry storage that also lives under DOMAIN.
DISCOVERY_DATA_KEY = "_persistent_discovery"
# hass.data[DOMAIN][FAILED_TRACES_KEY][entry_id] - frame trace + state
# snapshot kept when a device entry fails to set up. Without this the
# device object (and its trace) is dropped on failure, leaving
# diagnostics.py with nothing to show for precisely the entries that
# need diagnosing. Not an entry_id, so no collision with per-entry data.
FAILED_TRACES_KEY = "_failed_traces"

# Cloud linking (used only during config_flow to fetch local_keys; never
# stored/used again after setup - the running integration is 100% LAN).
CONF_REGION = "region"
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_UID = "uid"

DEFAULT_PROTOCOL_VERSION = "3.3"
SUPPORTED_PROTOCOL_VERSIONS = ["3.1", "3.2", "3.3", "3.4"]  # 3.2 added in v0.10.0
DEFAULT_SCAN_INTERVAL = 30  # seconds; fallback poll, LAN push is the primary path
DEFAULT_PORT = 6668
RECONNECT_INTERVAL = 60  # seconds; matches localtuya's own `RECONNECT_INTERVAL` -
# periodic retry for any configured device currently disconnected (dropped
# TCP connection, device rebooted, etc.) so it comes back on its own instead
# of needing the entry removed/re-added by hand.

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
# LAN UDP discovery (well-documented Tuya broadcast ports/key, used by the
# reference open-source implementations tinytuya/localtuya).
#
# BUG FIXED HERE (found by diffing against localtuya's actual source,
# custom_components/localtuya/discovery.py on the `master` branch): the AES
# key for the encrypted broadcast (port 6667) is `MD5(b"yGAdlopoPVldABfn")`
# - a 16-byte DIGEST, not the 16-character seed string used directly as the
# key. This module previously used the raw seed string AS the key, AND had
# a one-character typo in it ("PVLd" vs the correct "PVld") - two
# independent bugs stacked on the same constant. Net effect: decrypting
# port 6667 broadcasts never worked, so any device that broadcasts only
# on the encrypted port (common) was never actually discovered, even after
# the port-bind fix in v0.1.1. See discovery.py for the MD5 step.
# ---------------------------------------------------------------------------
UDP_PORT_UNENCRYPTED = 6666
UDP_PORT_ENCRYPTED = 6667
UDP_PORT_APP = 7000  # "Tuya app" broadcast port - missing entirely until a
# real report ("solo implementaste una parte del protocolo") pointed at it;
# confirmed against tinytuya's scanner.py (UDPPORTAPP = 7000). Newer/app-
# paired devices commonly broadcast here instead of (or as well as) 6666/6667.
UDP_KEY_SEED = b"yGAdlopoPVldABfn"  # MD5 of this is the real AES key - see discovery.py
DISCOVERY_TIMEOUT = 8
DISCOVERY_POLL_INTERVAL = 300  # seconds; how often an "account" entry re-scans for new devices
ACTIVE_SCAN_INTERVAL = 1800  # seconds; how often to brute-force-scan the LAN for devices
# passive broadcast discovery still hasn't found (see active_scan.py) - much
# less frequent than DISCOVERY_POLL_INTERVAL since a full subnet sweep is
# real network noise and takes real wall-clock time, unlike reading the
# always-on passive listener's cache.

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
