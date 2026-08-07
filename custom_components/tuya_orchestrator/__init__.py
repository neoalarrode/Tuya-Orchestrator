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

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
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
)
from .coordinator import TuyaOrchestratorCoordinator
from .discovery import PersistentDiscovery
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
    listener = PersistentDiscovery()
    await listener.start()
    hass.data.setdefault(DOMAIN, {})[DISCOVERY_DATA_KEY] = listener

    def _on_stop(event) -> None:
        listener.close()

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
    await device.connect()

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = TuyaOrchestratorCoordinator(hass, device, profile, scan_interval)
    await coordinator.async_config_entry_first_refresh()

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
