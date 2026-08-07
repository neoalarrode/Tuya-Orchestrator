"""Number platform - one entity per DP mapped with platform: number (live-adjustable)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TuyaOrchestratorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("number")
    async_add_entities(TuyaNumber(coordinator, m) for m in mappings)


class TuyaNumber(TuyaOrchestratorEntity, NumberEntity):
    def __init__(self, coordinator, mapping) -> None:
        super().__init__(coordinator, mapping)
        if mapping.device_class:
            self._attr_device_class = mapping.device_class
        if mapping.unit:
            self._attr_native_unit_of_measurement = mapping.unit
        self._attr_native_min_value = mapping.min_value if mapping.min_value is not None else 0
        self._attr_native_max_value = mapping.max_value if mapping.max_value is not None else 100
        self._attr_native_step = mapping.step or 1

    @property
    def native_value(self):
        return self._mapping.decode(self._raw)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_dp(self._mapping.dp_id, self._mapping.encode(value))
