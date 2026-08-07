"""Light platform - composite entity from a profile's `lights:` block
(switch DP + optional brightness/color-temp DPs + optional JSON HSV color
DP with a work-mode switch between white and colour). See
`profile.LightMapping` for the full field reference and the documented
wire-format caveat around the color JSON DP.
"""
from __future__ import annotations

import json
from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaOrchestratorCoordinator
from .profile import LightMapping


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(TuyaLight(coordinator, lm) for lm in coordinator.profile.lights)


def _scale(value: float, src_min: float, src_max: float, dst_min: float, dst_max: float) -> float:
    if src_max == src_min:
        return dst_min
    ratio = (value - src_min) / (src_max - src_min)
    return dst_min + ratio * (dst_max - dst_min)


def _decode_color_json(raw: Any) -> dict[str, float] | None:
    """Accept both a nested dict and a JSON-encoded string (Tuya Cloud API
    returns the latter; local wire format may send either - see
    LightMapping's docstring caveat)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return None


class TuyaLight(CoordinatorEntity[TuyaOrchestratorCoordinator], LightEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TuyaOrchestratorCoordinator, mapping: LightMapping) -> None:
        super().__init__(coordinator)
        self._mapping = mapping
        device_id = coordinator.device.device_id
        self._attr_unique_id = f"{device_id}_light_{mapping.switch_dp}"
        self._attr_name = mapping.name
        if mapping.icon:
            self._attr_icon = mapping.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.profile.name,
            manufacturer="Tuya",
            model=coordinator.profile.name,
        )

        modes = set()
        if mapping.color_dp is not None:
            modes.add(ColorMode.HS)
        if mapping.color_temp_dp is not None:
            modes.add(ColorMode.COLOR_TEMP)
        if mapping.brightness_dp is not None and not modes:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes

    @property
    def _data(self) -> dict[int, Any]:
        return self.coordinator.data or {}

    @property
    def _in_colour_mode(self) -> bool:
        m = self._mapping
        if m.color_dp is None:
            return False
        if m.work_mode_dp is None:
            return True  # only color available, no white mode to switch to
        return self._data.get(m.work_mode_dp) == m.work_mode_colour

    @property
    def color_mode(self) -> ColorMode:
        if self._in_colour_mode:
            return ColorMode.HS
        if self._mapping.color_temp_dp is not None:
            return ColorMode.COLOR_TEMP
        if self._mapping.brightness_dp is not None:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

    @property
    def is_on(self) -> bool | None:
        return self._data.get(self._mapping.switch_dp)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        m = self._mapping
        if m.color_dp is None:
            return None
        color = _decode_color_json(self._data.get(m.color_dp))
        if not color:
            return None
        h = _scale(color.get("h", 0), 0, m.color_h_max, 0, 360)
        s = _scale(color.get("s", 0), 0, m.color_s_max, 0, 100)
        return (h, s)

    @property
    def brightness(self) -> int | None:
        m = self._mapping
        if self._in_colour_mode:
            color = _decode_color_json(self._data.get(m.color_dp))
            if not color:
                return None
            return round(_scale(color.get("v", 0), 0, m.color_v_max, 0, 255))
        if m.brightness_dp is None:
            return None
        raw = self._data.get(m.brightness_dp)
        if raw is None:
            return None
        return round(_scale(raw, m.brightness_min, m.brightness_max, 0, 255))

    @property
    def color_temp_kelvin(self) -> int | None:
        m = self._mapping
        if m.color_temp_dp is None or self._in_colour_mode:
            return None
        raw = self._data.get(m.color_temp_dp)
        if raw is None:
            return None
        # Maps the device's raw warm(0)->cool(max) range onto a plausible
        # 2700K-6500K bulb range. Devices with a different real range should
        # override color_temp_min/max in the profile to correct this.
        return round(_scale(raw, m.color_temp_min, m.color_temp_max, 2700, 6500))

    async def async_turn_on(self, **kwargs: Any) -> None:
        m = self._mapping
        dps: dict[int, Any] = {m.switch_dp: True}

        if "hs_color" in kwargs and m.color_dp is not None:
            h, s = kwargs["hs_color"]
            v_raw = self._data.get(m.color_dp, {})
            current = _decode_color_json(v_raw) or {}
            brightness = kwargs.get("brightness")
            v = (
                _scale(brightness, 0, 255, 0, m.color_v_max)
                if brightness is not None
                else current.get("v", m.color_v_max)
            )
            color = {
                "h": round(_scale(h, 0, 360, 0, m.color_h_max)),
                "s": round(_scale(s, 0, 100, 0, m.color_s_max)),
                "v": round(v),
            }
            dps[m.color_dp] = color
            if m.work_mode_dp is not None:
                dps[m.work_mode_dp] = m.work_mode_colour
        else:
            if m.work_mode_dp is not None and (m.color_temp_dp is not None or m.brightness_dp is not None):
                dps[m.work_mode_dp] = m.work_mode_white
            if "brightness" in kwargs and m.brightness_dp is not None:
                dps[m.brightness_dp] = round(
                    _scale(kwargs["brightness"], 0, 255, m.brightness_min, m.brightness_max)
                )
            if "color_temp_kelvin" in kwargs and m.color_temp_dp is not None:
                dps[m.color_temp_dp] = round(
                    _scale(kwargs["color_temp_kelvin"], 2700, 6500, m.color_temp_min, m.color_temp_max)
                )

        await self.coordinator.device.set_dps(dps)
        merged = dict(self.coordinator.data or {})
        merged.update(dps)
        self.coordinator.async_set_updated_data(merged)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dp(self._mapping.switch_dp, False)
