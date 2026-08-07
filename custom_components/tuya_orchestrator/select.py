"""Select platform - one entity per DP mapped with platform: select + a `map`."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TuyaOrchestratorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("select")
    async_add_entities(TuyaSelect(coordinator, m) for m in mappings)


class TuyaSelect(TuyaOrchestratorEntity, SelectEntity):
    def __init__(self, coordinator, mapping) -> None:
        super().__init__(coordinator, mapping)
        self._attr_options = list((mapping.value_map or {}).values())

    @property
    def current_option(self):
        return self._mapping.decode(self._raw)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_dp(self._mapping.dp_id, self._mapping.encode(option))
