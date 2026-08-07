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
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
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

    async def _poll(now=None) -> None:
        try:
            await api.validate()
            devices = await api.get_user_devices(entry.data[CONF_UID])
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
        configured_ids = {e.unique_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id}
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
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    **device,
                    "ip": found.ip,
                    "version": found.version,
                    CONF_ACCOUNT_ENTRY_ID: entry.entry_id,
                },
            )
            new_count += 1
        if new_count:
            _LOGGER.info("Tuya account %s: %s new device(s) offered for discovery", entry.title, new_count)

    unsub = async_track_time_interval(hass, _poll, timedelta(seconds=DISCOVERY_POLL_INTERVAL))
    entry.async_on_unload(unsub)
    # First pass shortly after startup - don't block entry setup on it.
    hass.async_create_task(_poll())
