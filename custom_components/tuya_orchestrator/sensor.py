"""Sensor platform - one entity per DP mapped with platform: sensor."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TuyaOrchestratorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("sensor")
    async_add_entities(TuyaSensor(coordinator, m) for m in mappings)


class TuyaSensor(TuyaOrchestratorEntity, SensorEntity):
    def __init__(self, coordinator, mapping) -> None:
        super().__init__(coordinator, mapping)
        if mapping.device_class:
            self._attr_device_class = mapping.device_class
        if mapping.unit:
            self._attr_native_unit_of_measurement = mapping.unit

    @property
    def native_value(self):
        return self._mapping.decode(self._raw)
