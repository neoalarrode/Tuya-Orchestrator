"""Background discovery for the "account" ConfigEntry type.

An account entry holds only Tuya Cloud credentials - no device of its own.
Once set up, it periodically polls Tuya Cloud for this account's full
device list plus a LAN broadcast pass, and for every device NOT already
configured (or ignored) AND actually seen on the local network just now
triggers a native HA discovery flow (`SOURCE_INTEGRATION_DISCOVERY`) -
which HA's frontend renders as a "Discovered" card with Configure/Ignore
buttons, the same UX pattern as HomeKit Controller or Tapo. One
ConfigEntry per device is still created (same as before), just no longer
via a manual multi-step wizard - the account entry is the thing that finds
them.

A device the cloud knows about but that hasn't broadcast on this LAN
(offline, different VLAN/subnet, firewalled) is deliberately NOT offered -
discovery here means "present on this network right now", matching what
HomeKit/Tapo-style discovery actually means; a device merely existing on
the Tuya account isn't the same claim. Such a device can still be added
via the "manual" (no-cloud) flow with a hand-entered IP.

"Seen on this LAN" is judged against `discovery.PersistentDiscovery`'s
continuously-accumulating cache (started once at HA startup, see
__init__.py's `async_setup()`), NOT a fresh few-second listen per poll -
see that class's docstring for why a short on-demand window isn't
reliable enough (a real gap found reviewing localtuya's own __init__.py
end to end after the protocol/port fixes alone didn't resolve a live
report).

Dedup relies on `ConfigFlow.async_set_unique_id()`'s own built-in
behavior: calling it a second time for a still-in-progress flow with the
same unique_id aborts as "already_in_progress" automatically, so polling
every 5 minutes does not spam duplicate discovery cards for a device the
user hasn't acted on yet. The `configured_ids` check below is a cheap
pre-filter for devices that already have a real (or ignored) ConfigEntry.

**Also runs an active-scan fallback** (`active_scan.py`, `_active_scan_poll`,
every `ACTIVE_SCAN_INTERVAL`) for cloud-known devices passive discovery
still hasn't found after a while - real gap found from a live report that
specific device categories (relay-based heaters, an irrigation valve)
never got discovered even with the persistent listener (v0.3.0) running:
some simple/cheap devices only broadcast briefly around boot and then go
quiet, which no amount of passive listening will ever catch. Brute-forces
the local /24 for the LAN control port instead.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .active_scan import active_scan
from .const import (
    ACTIVE_SCAN_INTERVAL,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_ACCOUNT_ENTRY_ID,
    CONF_REGION,
    CONF_UID,
    DISCOVERY_DATA_KEY,
    DISCOVERY_POLL_INTERVAL,
    DOMAIN,
)
from .discovery import discover_devices
from .tuya_cloud import TuyaCloudApi, TuyaCloudApiError, TuyaCloudAuthError

_LOGGER = logging.getLogger(__name__)


async def async_setup_account(hass: HomeAssistant, entry: ConfigEntry) -> None:
    session = async_get_clientsession(hass)
    api = TuyaCloudApi(
        session, entry.data[CONF_REGION], entry.data[CONF_ACCESS_ID], entry.data[CONF_ACCESS_SECRET]
    )

    async def _offer(device: dict, ip: str, version: str | None) -> None:
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={**device, "ip": ip, "version": version, CONF_ACCOUNT_ENTRY_ID: entry.entry_id},
        )

    async def _cloud_devices_and_configured_ids() -> tuple[list[dict], set[str]]:
        devices = await api.get_user_devices(entry.data[CONF_UID])
        configured_ids = {e.unique_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id}
        return devices, configured_ids

    async def _poll(now=None) -> None:
        try:
            await api.validate()
            devices, configured_ids = await _cloud_devices_and_configured_ids()
        except (TuyaCloudAuthError, TuyaCloudApiError) as err:
            _LOGGER.warning("Tuya Cloud poll failed for account %s: %s", entry.title, err)
            return

        # Read from the domain-wide PersistentDiscovery listener (running
        # continuously since async_setup(), see __init__.py) instead of
        # opening a fresh short-window listener every poll - a device that
        # broadcasts on a longer/irregular interval could be missed by any
        # given few-second window, but not by something that's always
        # listening. Falls back to a one-off short listen only if that
        # listener somehow isn't there (shouldn't normally happen).
        persistent = hass.data.get(DOMAIN, {}).get(DISCOVERY_DATA_KEY)
        lan = dict(persistent.devices) if persistent else await discover_devices()
        _LOGGER.debug(
            "Tuya account %s: %s device(s) on cloud, %s seen on LAN broadcast (%s), "
            "%s already configured/ignored (%s)",
            entry.title,
            len(devices),
            len(lan),
            sorted(lan.keys()),
            len(configured_ids),
            sorted(configured_ids),
        )

        new_count = 0
        for device in devices:
            device_id = device["device_id"]
            if device_id in configured_ids:
                _LOGGER.debug("Skipping %s (%s): already configured/ignored", device["name"], device_id)
                continue
            found = lan.get(device_id)
            if found is None:
                _LOGGER.debug(
                    "Skipping %s (%s): known via cloud but not seen on this LAN broadcast pass",
                    device["name"],
                    device_id,
                )
                continue
            await _offer(device, found.ip, found.version)
            new_count += 1
        if new_count:
            _LOGGER.info("Tuya account %s: %s new device(s) offered for discovery", entry.title, new_count)

    async def _active_scan_poll(now=None) -> None:
        """Brute-force fallback (active_scan.py) for devices that STILL
        haven't been found via passive broadcast after a while - some
        simple/cheap devices (relay-based heaters, irrigation valves) only
        broadcast briefly around boot and then go quiet, which no amount
        of passive listening will ever catch. Runs far less often than
        `_poll` (ACTIVE_SCAN_INTERVAL, 30 min) since a full subnet sweep is
        real network noise and takes real wall-clock time."""
        try:
            await api.validate()
            devices, configured_ids = await _cloud_devices_and_configured_ids()
        except (TuyaCloudAuthError, TuyaCloudApiError) as err:
            _LOGGER.warning("Tuya Cloud poll failed for account %s (active scan): %s", entry.title, err)
            return

        missing = [d for d in devices if d["device_id"] not in configured_ids and d.get("local_key")]
        if not missing:
            return
        _LOGGER.debug(
            "Tuya account %s: active-scanning LAN for %d device(s) not yet found passively: %s",
            entry.title,
            len(missing),
            [d["name"] for d in missing],
        )
        matches = await active_scan(hass, [{"device_id": d["device_id"], "local_key": d["local_key"]} for d in missing])
        if not matches:
            return
        by_id = {d["device_id"]: d for d in missing}
        for device_id, ip in matches.items():
            await _offer(by_id[device_id], ip, "3.3")  # identified via a 3.3-protocol probe, see active_scan.py
        _LOGGER.info(
            "Tuya account %s: active scan found %d device(s) passive discovery missed: %s",
            entry.title,
            len(matches),
            list(matches),
        )

    unsub = async_track_time_interval(hass, _poll, timedelta(seconds=DISCOVERY_POLL_INTERVAL))
    entry.async_on_unload(unsub)
    unsub_scan = async_track_time_interval(hass, _active_scan_poll, timedelta(seconds=ACTIVE_SCAN_INTERVAL))
    entry.async_on_unload(unsub_scan)
    # First pass shortly after startup - don't block entry setup on it.
    hass.async_create_task(_poll())
    hass.async_create_task(_active_scan_poll())
