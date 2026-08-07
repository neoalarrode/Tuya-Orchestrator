"""Vacuum platform - a real `vacuum.*` StateVacuumEntity built from a
profile's `vacuums:` block. See `profile.VacuumMapping` for field reference.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaOrchestratorCoordinator
from .profile import VacuumMapping

_ACTIVITY_VALUES = {a.value for a in VacuumActivity}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(TuyaVacuum(coordinator, vm) for vm in coordinator.profile.vacuums)


class TuyaVacuum(CoordinatorEntity[TuyaOrchestratorCoordinator], StateVacuumEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TuyaOrchestratorCoordinator, mapping: VacuumMapping) -> None:
        super().__init__(coordinator)
        self._mapping = mapping
        device_id = coordinator.device.device_id
        self._attr_unique_id = f"{device_id}_vacuum_{mapping.start_dp}"
        self._attr_name = mapping.name
        if mapping.icon:
            self._attr_icon = mapping.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.profile.name,
            manufacturer="Tuya",
            model=coordinator.profile.name,
        )

        features = VacuumEntityFeature(0)
        if mapping.start_dp is not None:
            features |= VacuumEntityFeature.START
        if mapping.pause_dp is not None:
            features |= VacuumEntityFeature.PAUSE
        if mapping.return_dp is not None:
            features |= VacuumEntityFeature.RETURN_HOME
        if mapping.locate_dp is not None:
            features |= VacuumEntityFeature.LOCATE
        if mapping.battery_dp is not None:
            features |= VacuumEntityFeature.BATTERY
        if mapping.status_dp is not None:
            features |= VacuumEntityFeature.STATE
        if mapping.fan_speed_dp is not None and mapping.fan_speed_map:
            features |= VacuumEntityFeature.FAN_SPEED
            self._attr_fan_speed_list = list(mapping.fan_speed_map.values())
        self._attr_supported_features = features

    @property
    def _data(self) -> dict[int, Any]:
        return self.coordinator.data or {}

    @property
    def activity(self) -> VacuumActivity | None:
        m = self._mapping
        if m.status_dp is not None and m.status_map:
            label = m.status_map.get(self._data.get(m.status_dp))
            if label in _ACTIVITY_VALUES:
                return VacuumActivity(label)
        return None

    @property
    def battery_level(self) -> int | None:
        if self._mapping.battery_dp is None:
            return None
        raw = self._data.get(self._mapping.battery_dp)
        if raw is None:
            return None
        return round(raw / self._mapping.battery_scale) if self._mapping.battery_scale else round(raw)

    @property
    def fan_speed(self) -> str | None:
        m = self._mapping
        if m.fan_speed_dp is None or not m.fan_speed_map:
            return None
        return m.fan_speed_map.get(self._data.get(m.fan_speed_dp))

    async def _send(self, dps: dict[int, Any]) -> None:
        await self.coordinator.device.set_dps(dps)
        merged = dict(self.coordinator.data or {})
        merged.update(dps)
        self.coordinator.async_set_updated_data(merged)

    async def async_start(self) -> None:
        m = self._mapping
        if m.start_dp is None:
            return
        if m.start_map:
            reverse = {v: k for k, v in m.start_map.items()}
            if (raw := reverse.get("start")) is not None:
                await self._send({m.start_dp: raw})
        else:
            await self._send({m.start_dp: True})

    async def async_pause(self) -> None:
        m = self._mapping
        if m.pause_dp is not None:
            await self._send({m.pause_dp: True})
        elif m.start_dp is not None and m.start_map:
            reverse = {v: k for k, v in m.start_map.items()}
            if (raw := reverse.get("pause")) is not None:
                await self._send({m.start_dp: raw})

    async def async_return_to_base(self, **kwargs: Any) -> None:
        if self._mapping.return_dp is not None:
            await self._send({self._mapping.return_dp: True})

    async def async_locate(self, **kwargs: Any) -> None:
        if self._mapping.locate_dp is not None:
            await self._send({self._mapping.locate_dp: True})

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        m = self._mapping
        if m.fan_speed_dp is None or not m.fan_speed_map:
            return
        reverse = {v: k for k, v in m.fan_speed_map.items()}
        if (raw := reverse.get(fan_speed)) is not None:
            await self._send({m.fan_speed_dp: raw})
