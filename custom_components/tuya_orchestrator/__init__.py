"""Tuya Orchestrator - fully local (LAN) Tuya device integration with
declarative, user-editable device profiles (ESPHome-style customization,
Tuya-over-LAN instead of ESPHome-over-WiFi).

One ConfigEntry = one device (same pattern as this project family's other
integrations - repeat "+ Add integration" per device)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROFILE_YAML,
    CONF_PROTOCOL_VERSION,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import TuyaOrchestratorCoordinator
from .profile import parse_profile
from .tuya_lan import TuyaLocalDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id)
        await stored["device"].close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
