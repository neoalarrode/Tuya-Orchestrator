"""Diagnostics for Tuya Orchestrator.

Home Assistant picks this module up automatically (no manifest entry
needed) and offers a "Download diagnostics" button on each config entry.

Why this exists, concretely: a device can complete its handshake and then
have every query time out, and the only thing that answers *why* is the
sequence of frames that actually crossed the wire. That detail is logged
at DEBUG, which is unreachable from outside the instance - recent Home
Assistant no longer exposes `/api/error_log`, and the API that remains
(`system_log`) only carries WARNING and above. This closes that gap: the
per-device frame trace (see `TuyaLocalDevice.trace()`) comes out through
a supported, redacted, user-initiated channel instead.

REDACTION: local keys, cloud credentials and the account UID never appear
here. Frame traces keep only the first 48 bytes of each frame - enough
for the header, retcode and the start of the payload, not enough to carry
a whole encrypted DP payload out of the instance.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_UID,
    DISCOVERY_DATA_KEY,
    FAILED_TRACES_KEY,
    DOMAIN,
    ENTRY_TYPE_ACCOUNT,
)

TO_REDACT = {CONF_LOCAL_KEY, CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_UID, "local_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "source": entry.source,
            "state": str(entry.state),
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        }
    }

    # Domain-wide LAN discovery state - useful on ANY entry, since "is this
    # device broadcasting at all?" is the first question for a device that
    # will not connect.
    listener = hass.data.get(DOMAIN, {}).get(DISCOVERY_DATA_KEY)
    if listener is not None:
        data["lan_discovery"] = {
            "listening": bool(listener._transports),  # noqa: SLF001
            "devices_seen": {
                device_id: {"ip": d.ip, "version": d.version, "product_key": d.product_key}
                for device_id, d in listener.devices.items()
            },
        }

    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
        return data

    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(stored, dict):
        # The entry is not loaded (setup_retry / setup_error) - which is
        # precisely the case worth diagnosing, so report the snapshot taken
        # when setup failed rather than a bare "not loaded" (which is all
        # this returned at first, and it was useless on a live instance).
        data["device"] = {"loaded": False}
        failed = hass.data.get(DOMAIN, {}).get(FAILED_TRACES_KEY, {}).get(entry.entry_id)
        if failed is not None:
            data["last_setup_failure"] = failed
        return data

    device = stored["device"]
    coordinator = stored["coordinator"]
    profile = coordinator.profile

    data["device"] = {
        "loaded": True,
        "address": device.address,
        "port": device.port,
        "protocol_version": device.protocol_version,
        "dev_type": device.dev_type,
        "connected": device.connected,
        "is_closing": device._is_closing,  # noqa: SLF001
        "sequence_counter": device._seq,  # noqa: SLF001
        "session_key_negotiated": device.local_key != device.real_local_key,
        "dps_requested_explicitly": sorted(device.dps_to_request),
        "pending_waiters": {
            "by_sequence": sorted(device._pending),  # noqa: SLF001
            "by_command": [f"0x{c:02x}" for c in device._pending_cmd],  # noqa: SLF001
        },
    }
    data["coordinator"] = {
        "last_update_success": coordinator.last_update_success,
        "update_interval_s": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
        "dps": coordinator.data,
    }
    data["profile"] = {
        "name": profile.name,
        "dp_ids": profile.all_dp_ids(),
        "counts": {
            "dps": len(profile.dps),
            "lights": len(profile.lights),
            "climates": len(profile.climates),
            "vacuums": len(profile.vacuums),
        },
    }
    # The point of the whole module - see the docstring.
    data["frame_trace"] = device.trace()
    return data
