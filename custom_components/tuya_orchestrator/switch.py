"""Switch platform - one entity per DP mapped with platform: switch."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TuyaOrchestratorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("switch")
    async_add_entities(TuyaSwitch(coordinator, m) for m in mappings)


class TuyaSwitch(TuyaOrchestratorEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        return self._mapping.decode(self._raw)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dp(self._mapping.dp_id, self._mapping.encode(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dp(self._mapping.dp_id, self._mapping.encode(False))
