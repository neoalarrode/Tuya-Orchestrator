"""Binary sensor platform - one entity per DP mapped with platform: binary_sensor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TuyaOrchestratorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("binary_sensor")
    async_add_entities(TuyaBinarySensor(coordinator, m) for m in mappings)


class TuyaBinarySensor(TuyaOrchestratorEntity, BinarySensorEntity):
    def __init__(self, coordinator, mapping) -> None:
        super().__init__(coordinator, mapping)
        if mapping.device_class:
            self._attr_device_class = mapping.device_class

    @property
    def is_on(self) -> bool | None:
        return self._mapping.decode(self._raw)
