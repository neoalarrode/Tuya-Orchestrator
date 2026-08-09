"""Tuya Orchestrator - fully local (LAN) Tuya device integration with
declarative, auto-generated device profiles (ESPHome-style customization,
Tuya-over-LAN instead of ESPHome-over-WiFi).

Two kinds of ConfigEntry:
- "account" (no device of its own): Tuya Cloud credentials, runs a
  background poller (account.py) that offers newly-seen devices as native
  HA discovery flows (Configure/Ignore cards).
- "device" (one per paired device, same as before): the actual LAN
  connection + entities.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .account import async_setup_account
from .const import (
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_PROFILE_YAML,
    CONF_PROTOCOL_VERSION,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_DATA_KEY,
    DOMAIN,
    ENTRY_TYPE_ACCOUNT,
    PLATFORMS,
    RECONNECT_INTERVAL,
)
from .coordinator import TuyaOrchestratorCoordinator
from .discovery import DiscoveredDevice, PersistentDiscovery
from .profile import parse_profile
from .tuya_lan import TuyaLocalDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Called ONCE per HA startup, before any ConfigEntry is set up -
    starts the LAN broadcast listener here so it stays open and
    accumulating for the WHOLE session, matching localtuya's own
    `__init__.py` (its `TuyaDiscovery` is started exactly this way, not
    per-poll). See `discovery.PersistentDiscovery`'s docstring for why an
    ephemeral few-second listen-and-close window per poll wasn't reliable
    enough - found by reviewing localtuya's full codebase after a live
    report that devices confirmed present on the LAN still weren't being
    discovered even after v0.2.9's port/framing fixes."""

    def _on_device_seen(device: DiscoveredDevice) -> None:
        # BUG FIXED HERE (v0.7.0): a configured device's IP is a plain DHCP
        # lease, not something guaranteed to stay put forever - when it
        # changed, this integration kept dialing the OLD address forever,
        # forcing the user to remove and re-pair the device by hand to
        # "notice" the new one. localtuya reacts to every broadcast it
        # hears (`_device_discovered` in its `__init__.py`) and, on an IP
        # mismatch against the stored entry, calls
        # `hass.config_entries.async_update_entry(...)` with the new
        # address - that alone is enough, because HA calls every
        # registered `add_update_listener` (already wired below, in
        # `_async_setup_device_entry`) on ANY entry data change, which
        # reloads the entry and reconnects with the fresh IP. Same
        # mechanism ported here, 1:1.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_DEVICE_ID) != device.device_id:
                continue
            if entry.data.get("address") == device.ip:
                break
            _LOGGER.info(
                "Tuya Orchestrator: device %s changed IP %s -> %s, updating and reloading",
                device.device_id,
                entry.data.get("address"),
                device.ip,
            )
            new_data = dict(entry.data)
            new_data["address"] = device.ip
            hass.config_entries.async_update_entry(entry, data=new_data)
            break

    listener = PersistentDiscovery(on_device=_on_device_seen)
    await listener.start()
    hass.data.setdefault(DOMAIN, {})[DISCOVERY_DATA_KEY] = listener

    async def _async_reconnect(now) -> None:
        # Companion fix to the above: an IP-unchanged disconnect (device
        # rebooted, briefly lost power/wifi, TCP reset...) doesn't trigger
        # a reload on its own - matches localtuya's own periodic
        # `_async_reconnect` (`RECONNECT_INTERVAL`), which retries any
        # device it still has marked as disconnected instead of waiting
        # for the user to intervene.
        for stored in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(stored, dict) or "device" not in stored:
                continue
            device: TuyaLocalDevice = stored["device"]
            if device.connected:
                continue
            _LOGGER.debug("Tuya Orchestrator: retrying connection to %s", device.device_id)
            try:
                await device.connect()
            except Exception as err:  # noqa: BLE001 - just retry again next interval
                _LOGGER.debug("Tuya Orchestrator: reconnect to %s failed: %s", device.device_id, err)

    hass.data[DOMAIN]["_reconnect_unsub"] = async_track_time_interval(
        hass, _async_reconnect, timedelta(seconds=RECONNECT_INTERVAL)
    )

    def _on_stop(event) -> None:
        listener.close()
        hass.data[DOMAIN]["_reconnect_unsub"]()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
        await async_setup_account(hass, entry)
        return True
    return await _async_setup_device_entry(hass, entry)


async def _async_setup_device_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    profile = parse_profile(data[CONF_PROFILE_YAML])

    device = TuyaLocalDevice(
        device_id=data[CONF_DEVICE_ID],
        address=entry.data["address"],
        local_key=data[CONF_LOCAL_KEY],
        protocol_version=data.get(CONF_PROTOCOL_VERSION, "3.3"),
    )
    # BUG FIXED HERE: a connection failure (device briefly offline, reset,
    # 3.4 handshake failure...) used to propagate as a raw exception - HA
    # logs that as a scary "Error setting up entry" traceback and does NOT
    # treat it as a normal retry-able state (that traceback is exactly
    # what a live report pasted for a ConnectionResetError). Tuya devices
    # going briefly unreachable is routine, not exceptional - the correct
    # HA pattern is ConfigEntryNotReady, which shows a clean "not ready,
    # will retry" status and actually retries on HA's own schedule.
    try:
        await device.connect()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Could not connect to {data[CONF_DEVICE_ID]} at {entry.data['address']}: {err}") from err

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = TuyaOrchestratorCoordinator(hass, device, profile, scan_interval)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await device.close()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "device": device,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
        return True  # nothing to unload beyond the poller's own async_on_unload
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id)
        await stored["device"].close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
