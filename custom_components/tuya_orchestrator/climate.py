"""Climate platform - a real `climate.*` thermostat entity built from a
profile's `climates:` block, instead of separate switch/number/select
entities for power/setpoint/mode. See `profile.ClimateMapping` for the
full field reference and design rationale.

Direct passthrough only: no scheduling, no learned thermal model, no
anticipation - this integration's job is faithfully exposing the device's
own native climate DPs as a proper HA entity type, nothing more. (A
scheduling/optimization layer on top is exactly what Climate Orchestrator
is for - this project stays in its lane.)
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaOrchestratorCoordinator
from .profile import ClimateMapping

_HVAC_MODE_VALUES = {m.value for m in HVACMode}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(TuyaClimate(coordinator, cm) for cm in coordinator.profile.climates)


class TuyaClimate(CoordinatorEntity[TuyaOrchestratorCoordinator], ClimateEntity):
    _attr_has_entity_name = True
    _attr_temperature_unit = "°C"

    def __init__(self, coordinator: TuyaOrchestratorCoordinator, mapping: ClimateMapping) -> None:
        super().__init__(coordinator)
        self._mapping = mapping
        device_id = coordinator.device.device_id
        self._attr_unique_id = f"{device_id}_climate_{mapping.switch_dp or mapping.target_temp_dp}"
        self._attr_name = mapping.name
        if mapping.icon:
            self._attr_icon = mapping.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.profile.name,
            manufacturer="Tuya",
            model=coordinator.profile.name,
        )

        features = ClimateEntityFeature(0)
        if mapping.target_temp_dp is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
            # HA REQUIRES target_temperature_low/high (not plain
            # target_temperature) whenever the entity can be in
            # HVACMode.HEAT_COOL - without this the temperature control
            # simply doesn't render/work while in that mode (confirmed:
            # same real HA requirement already handled in Climate
            # Orchestrator's dual-setpoint zones). This device only has
            # ONE physical setpoint DP regardless of mode, so both bounds
            # mirror the same value/DP - not a real independent range, but
            # the only honest option without a second DP to back it.
            if mapping.mode_dp is not None and mapping.mode_map and "heat_cool" in mapping.mode_map.values():
                features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if mapping.fan_dp is not None:
            features |= ClimateEntityFeature.FAN_MODE
        if mapping.preset_dp is not None:
            features |= ClimateEntityFeature.PRESET_MODE
        if mapping.swing_dp is not None:
            features |= ClimateEntityFeature.SWING_MODE
        features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        self._attr_supported_features = features

        if mapping.mode_dp is not None and mapping.mode_map:
            modes = {HVACMode.OFF}
            for label in mapping.mode_map.values():
                if label in _HVAC_MODE_VALUES:
                    modes.add(HVACMode(label))
            self._attr_hvac_modes = sorted(modes, key=lambda m: m.value)
        else:
            # No distinct mode DP - a simple on/off device (e.g. a plain
            # heater): only OFF/HEAT, driven purely by switch_dp.
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

        if mapping.fan_dp is not None and mapping.fan_map:
            self._attr_fan_modes = list(mapping.fan_map.values())
        if mapping.preset_dp is not None and mapping.preset_map:
            self._attr_preset_modes = list(mapping.preset_map.values())
        if mapping.swing_dp is not None and mapping.swing_map:
            self._attr_swing_modes = list(mapping.swing_map.values())

        self._attr_min_temp = mapping.target_temp_min
        self._attr_max_temp = mapping.target_temp_max
        self._attr_target_temperature_step = mapping.target_temp_step

    @property
    def _data(self) -> dict[int, Any]:
        return self.coordinator.data or {}

    def _decode_scaled(self, dp_id: int | None, scale: float | None) -> float | None:
        if dp_id is None:
            return None
        raw = self._data.get(dp_id)
        if raw is None:
            return None
        return raw / scale if scale else raw

    def _encode_scaled(self, value: float, scale: float | None) -> Any:
        return round(value * scale) if scale else round(value)

    @property
    def hvac_mode(self) -> HVACMode:
        m = self._mapping
        if m.switch_dp is not None and not self._data.get(m.switch_dp):
            return HVACMode.OFF
        if m.mode_dp is not None and m.mode_map:
            raw = self._data.get(m.mode_dp)
            label = m.mode_map.get(raw)
            if label in _HVAC_MODE_VALUES:
                return HVACMode(label)
        return HVACMode.HEAT

    @property
    def current_temperature(self) -> float | None:
        return self._decode_scaled(self._mapping.current_temp_dp, self._mapping.current_temp_scale)

    @property
    def target_temperature(self) -> float | None:
        # Not meaningful in HEAT_COOL mode - HA expects target_temperature_low/
        # high there instead (see __init__'s comment on TARGET_TEMPERATURE_RANGE).
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return None
        return self._decode_scaled(self._mapping.target_temp_dp, self._mapping.target_temp_scale)

    @property
    def target_temperature_low(self) -> float | None:
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._decode_scaled(self._mapping.target_temp_dp, self._mapping.target_temp_scale)

    @property
    def target_temperature_high(self) -> float | None:
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        return self._decode_scaled(self._mapping.target_temp_dp, self._mapping.target_temp_scale)

    @property
    def current_humidity(self) -> float | None:
        if self._mapping.humidity_dp is None:
            return None
        return self._data.get(self._mapping.humidity_dp)

    @property
    def fan_mode(self) -> str | None:
        m = self._mapping
        if m.fan_dp is None or not m.fan_map:
            return None
        return m.fan_map.get(self._data.get(m.fan_dp))

    @property
    def preset_mode(self) -> str | None:
        m = self._mapping
        if m.preset_dp is None or not m.preset_map:
            return None
        return m.preset_map.get(self._data.get(m.preset_dp))

    @property
    def swing_mode(self) -> str | None:
        m = self._mapping
        if m.swing_dp is None or not m.swing_map:
            return None
        return m.swing_map.get(self._data.get(m.swing_dp))

    async def _send(self, dps: dict[int, Any]) -> None:
        await self.coordinator.device.set_dps(dps)
        merged = dict(self.coordinator.data or {})
        merged.update(dps)
        self.coordinator.async_set_updated_data(merged)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        m = self._mapping
        dps: dict[int, Any] = {}
        if hvac_mode == HVACMode.OFF:
            if m.switch_dp is not None:
                dps[m.switch_dp] = False
        else:
            if m.switch_dp is not None:
                dps[m.switch_dp] = True
            if m.mode_dp is not None and m.mode_map:
                reverse = {v: k for k, v in m.mode_map.items()}
                raw = reverse.get(hvac_mode.value)
                if raw is not None:
                    dps[m.mode_dp] = raw
        if dps:
            await self._send(dps)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if self._mapping.target_temp_dp is None:
            return
        # In HEAT_COOL mode HA's climate.set_temperature service sends
        # target_temp_low/target_temp_high (HA's actual service parameter
        # names - note: no "erature", unlike the target_temperature_low/
        # high PROPERTY names) instead of a plain temperature. Both map to
        # the SAME single physical DP here (see the property getters
        # above), so either one (or their average if both arrive in the
        # same call) is written.
        temp = kwargs.get("temperature")
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        if temp is None:
            if low is not None and high is not None:
                temp = (low + high) / 2
            else:
                temp = low if low is not None else high
        if temp is None:
            return
        raw = self._encode_scaled(temp, self._mapping.target_temp_scale)
        await self._send({self._mapping.target_temp_dp: raw})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        m = self._mapping
        if m.fan_dp is None or not m.fan_map:
            return
        reverse = {v: k for k, v in m.fan_map.items()}
        if (raw := reverse.get(fan_mode)) is not None:
            await self._send({m.fan_dp: raw})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        m = self._mapping
        if m.preset_dp is None or not m.preset_map:
            return
        reverse = {v: k for k, v in m.preset_map.items()}
        if (raw := reverse.get(preset_mode)) is not None:
            await self._send({m.preset_dp: raw})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        m = self._mapping
        if m.swing_dp is None or not m.swing_map:
            return
        reverse = {v: k for k, v in m.swing_map.items()}
        if (raw := reverse.get(swing_mode)) is not None:
            await self._send({m.swing_dp: raw})

    async def async_turn_on(self) -> None:
        if self._mapping.switch_dp is not None:
            await self._send({self._mapping.switch_dp: True})

    async def async_turn_off(self) -> None:
        if self._mapping.switch_dp is not None:
            await self._send({self._mapping.switch_dp: False})
