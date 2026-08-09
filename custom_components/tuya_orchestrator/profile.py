"""Declarative device profiles - the ESPHome-style customization layer.

Instead of hardcoding per-model Python classes (the localtuya/tuya_local
approach), every device is driven by a small YAML document describing its
datapoints (DPs) and how each one maps to a Home Assistant entity. Profiles
can be:

- Built-in, shipped in `profiles/*.yaml` in this repo (community-contributed,
  matched to a device by `product_id`).
- Pasted/edited freely by the user in the config_flow "profile" step - no
  code, no PR needed to support a new/weird device.

Example profile (a smart plug with energy monitoring):

    name: Generic smart plug (energy)
    dps:
      - id: 1
        platform: switch
        name: Power
      - id: 18
        platform: sensor
        name: Current
        device_class: current
        unit: mA
        scale: 1        # raw_value / scale = actual value
      - id: 19
        platform: sensor
        name: Power
        device_class: power
        unit: W
        scale: 10
      - id: 20
        platform: sensor
        name: Voltage
        device_class: voltage
        unit: V
        scale: 10

`scale` divides the raw integer DP value (Tuya devices commonly send
fixed-point integers). `map` (instead of `scale`) translates raw values to
labels for `select`/`sensor` (enum-style DPs). `invert: true` flips a
boolean DP before it reaches HA (some devices report closed=true for
"off"). Anything not listed here is intentionally NOT supported yet (no
templating engine, no cross-DP formulas) - keep it simple and inspectable;
open an issue if a real device needs more.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class DPMapping:
    dp_id: int
    platform: str  # switch | sensor | number | binary_sensor | select
    name: str
    device_class: str | None = None
    unit: str | None = None
    scale: float | None = None
    invert: bool = False
    value_map: dict[Any, str] | None = None  # raw -> label (select/sensor enum)
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    icon: str | None = None
    # `bit`: this DP's raw value is a hex-encoded multi-byte bitfield (some
    # Tuya devices pack several unrelated booleans - e.g. display light,
    # buzzer/beep, eco mode - into ONE string DP instead of giving each its
    # own) rather than a plain bool/number/enum. `bit` is a flat index into
    # that byte array (bit 0-7 = first byte, 8-15 = second byte, ...) -
    # only meaningful on `platform: switch`/`binary_sensor`. See
    # decode_bit()/encode_bit(); a real example is documented in
    # profiles/tuya_ac_basic.yaml (display light / buzzer on a boolCode DP).
    bit: int | None = None

    def decode(self, raw: Any) -> Any:
        if raw is None:
            return None
        if self.bit is not None:
            return self.decode_bit(raw)
        if self.value_map is not None:
            return self.value_map.get(raw, raw)
        if self.invert and isinstance(raw, bool):
            return not raw
        if self.scale:
            try:
                return raw / self.scale
            except TypeError:
                return raw
        return raw

    def encode(self, value: Any) -> Any:
        # NOTE: bit fields do NOT go through this method - encoding a
        # single bit requires the DP's CURRENT raw value (to preserve the
        # other, unrelated bits packed into the same field) which this
        # stateless method has no access to. Callers use encode_bit()
        # directly instead, passing the coordinator's current raw value.
        if self.value_map is not None:
            reverse = {v: k for k, v in self.value_map.items()}
            return reverse.get(value, value)
        if self.invert and isinstance(value, bool):
            return not value
        if self.scale:
            return round(value * self.scale)
        return value

    def decode_bit(self, raw: Any) -> bool | None:
        """Read this mapping's single `bit` out of a hex-encoded bitfield
        DP value. Missing/undersized data reads as False (bit not set),
        not None - a packed boolean field's absence means "off", not
        "unknown", matching how the device itself would report a
        never-toggled byte."""
        if self.bit is None or not raw:
            return None
        try:
            data = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
        except (ValueError, TypeError):
            return None
        byte_idx, bit_in_byte = divmod(self.bit, 8)
        if byte_idx >= len(data):
            return False
        return bool((data[byte_idx] >> bit_in_byte) & 1)

    def encode_bit(self, value: bool, current_raw: Any) -> str:
        """Flip this mapping's single `bit` while preserving every other
        bit already set in the field - a plain `encode(value)` can't do
        this safely since it has no access to the field's current value."""
        byte_idx, bit_in_byte = divmod(self.bit, 8)
        try:
            data = bytearray.fromhex(current_raw) if isinstance(current_raw, str) and current_raw else bytearray()
        except ValueError:
            data = bytearray()
        if len(data) <= byte_idx:
            data.extend(b"\x00" * (byte_idx + 1 - len(data)))
        if value:
            data[byte_idx] |= 1 << bit_in_byte
        else:
            data[byte_idx] &= ~(1 << bit_in_byte) & 0xFF
        return data.hex()


@dataclass
class LightMapping:
    """A composite entity: several DPs (power/brightness/color_temp/color)
    driving ONE `light.*` entity - unlike every other platform, which is
    one DP per entity. Kept as its own top-level `lights:` list rather than
    shoehorned into `dps:` for that reason.

    Many real Tuya RGBCW bulbs are dual-mode: a `work_mode_dp` enum
    ("white"/"colour") picks whether the bulb is currently governed by the
    plain brightness/color-temp DPs, or by a single JSON color DP
    (`color_dp`, Tuya's `colour_data_v2`-style `{"h":...,"s":...,"v":...}`)
    which ALSO carries brightness (its "v" field) while in that mode -
    that's a real, verified device behavior (checked against a live
    account), not a guess.

    WIRE-FORMAT CAVEAT (genuinely unverified, flagged rather than assumed):
    Tuya's Cloud API returns `color_dp`'s value as a JSON-encoded STRING
    (`'{"h":0,"s":1000,"v":1000}'`). Some Tuya firmware sends the equivalent
    LOCAL protocol DP as a nested JSON object instead of a string - this
    integration's `_decode_color_json`/`_encode_color_json` (see light.py)
    accept and emit BOTH forms defensively, but which one YOUR device
    actually expects when receiving a command has not been confirmed
    against a live LAN session. Report back if color-setting doesn't work.
    """

    name: str
    switch_dp: int
    brightness_dp: int | None = None
    brightness_min: float = 0
    brightness_max: float = 255
    color_temp_dp: int | None = None
    color_temp_min: float = 0
    color_temp_max: float = 255
    color_dp: int | None = None  # JSON {"h":.., "s":.., "v":..} DP
    color_h_max: float = 360
    color_s_max: float = 1000
    color_v_max: float = 1000  # also used as the brightness scale WHILE in colour mode
    work_mode_dp: int | None = None  # enum DP selecting white vs colour mode
    work_mode_white: str = "white"
    work_mode_colour: str = "colour"
    icon: str | None = None


@dataclass
class ClimateMapping:
    """Composite entity for a real `climate.*` entity - this is the answer
    to "it's clearly an AC/heater, why not a thermostat card instead of
    four separate entities". A profile author (built-in or custom) opts a
    device INTO this by adding a `climate:` block instead of listing its
    power/mode/setpoint DPs individually under `dps:` - whichever DPs are
    consumed here should NOT also appear in `dps:`, to avoid two entities
    fighting over the same datapoint.

    Deliberately thin/direct: unlike Climate Orchestrator (a *scheduling*
    engine with learned thermal inertia, presets, anticipation...), this is
    a straight passthrough to the device's OWN native climate DPs - no
    scheduling, no learning. That belongs in a separate project if wanted;
    this one's job is just "expose the device faithfully as a proper HA
    entity type".
    """

    name: str = "Climate"
    switch_dp: int | None = None  # on/off; hvac_mode OFF when False
    current_temp_dp: int | None = None
    current_temp_scale: float | None = None
    target_temp_dp: int | None = None
    target_temp_scale: float | None = None
    target_temp_min: float = 5
    target_temp_max: float = 35
    target_temp_step: float = 0.5
    # mode_dp + mode_map: raw device value -> HA HVACMode string
    # ("cool"/"heat"/"dry"/"fan_only"/"auto"/"heat_cool"). If absent, the
    # entity only ever exposes [off, heat] toggled purely by switch_dp -
    # matches simple heaters with no distinct "mode" DP.
    mode_dp: int | None = None
    mode_map: dict[str, str] | None = None
    # fan_dp + fan_map: raw <-> label, exposed as ClimateEntityFeature.FAN_MODE
    fan_dp: int | None = None
    fan_map: dict[str, str] | None = None
    # preset_dp + preset_map: raw <-> label, exposed as PRESET_MODE - for
    # devices whose "mode" is really an intensity/preset, not a real
    # off/heat/cool HVAC mode (e.g. a heater's High/Low).
    preset_dp: int | None = None
    preset_map: dict[str, str] | None = None
    humidity_dp: int | None = None  # display-only current_humidity
    # swing_dp + swing_map: raw <-> label, exposed as
    # ClimateEntityFeature.SWING_MODE. Some ACs have two independent swing
    # axes (up/down AND left/right) - only one is modeled here (HA's
    # climate entity has a single swing_mode dimension); if a device
    # exposes both, only the first one auto-detected becomes swing_dp, the
    # other is left as a plain dps: select entry.
    swing_dp: int | None = None
    swing_map: dict[str, str] | None = None
    icon: str | None = None


@dataclass
class VacuumMapping:
    """Composite entity for a real `vacuum.*` entity (HA's native
    StateVacuumEntity: one status/battery/fan-speed card with
    start/pause/dock/locate buttons) instead of a pile of loose
    switch/select/sensor entities for a robot vacuum's control surface.

    Deliberately narrower than the whole device: consumable-life/clean-area
    /clean-time DPs stay as plain `dps: sensor` entries alongside this
    entity - that split (one vacuum card + a few detail sensors) is the
    same shape HA's own reference vacuum integrations use, and is more
    "usable in reality" than cramming everything into one entity.
    """

    name: str = "Vacuum"
    start_dp: int | None = None
    start_map: dict[str, str] | None = None  # raw <-> {"start": ..., "pause": ...} when start is an enum, not a bool
    pause_dp: int | None = None  # separate boolean pause DP (devices that don't reuse start_dp for it)
    return_dp: int | None = None
    locate_dp: int | None = None
    battery_dp: int | None = None
    battery_scale: float | None = None
    status_dp: int | None = None
    status_map: dict[str, str] | None = None  # raw -> HA VacuumActivity value
    fan_speed_dp: int | None = None
    fan_speed_map: dict[str, str] | None = None  # raw <-> label
    icon: str | None = None


@dataclass
class DeviceProfile:
    name: str
    dps: list[DPMapping] = field(default_factory=list)
    lights: list[LightMapping] = field(default_factory=list)
    climates: list[ClimateMapping] = field(default_factory=list)
    vacuums: list[VacuumMapping] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)

    def dps_for_platform(self, platform: str) -> list[DPMapping]:
        return [d for d in self.dps if d.platform == platform]

    def all_dp_ids(self) -> list[int]:
        """Every DP id this profile touches, plain and composite alike.

        Needed for `TuyaLocalDevice.add_dps_to_request()`: a "type_0d"
        device won't answer a plain DP_QUERY at all - it requires the query
        to name the DPs explicitly (see DEV_TYPE_0D in tuya_lan.py). The
        reference builds this same list from its configured entity list;
        here the profile IS the entity list, so it must contribute the
        composite mappings' DPs too, not just the flat `dps:` entries -
        a device whose entities are all composite (a bare vacuum or
        climate profile) would otherwise register an EMPTY request list
        and report nothing.
        """
        ids: set[int] = {d.dp_id for d in self.dps}
        composites: list[Any] = [*self.lights, *self.climates, *self.vacuums]
        for mapping in composites:
            for field_name, value in vars(mapping).items():
                if field_name.endswith("_dp") and isinstance(value, int):
                    ids.add(value)
        return sorted(ids)


def _int_or_none(value: Any) -> int | None:
    """Coerce an optional dp_id-like field to int. BUG FIXED HERE: composite
    mapping fields (LightMapping/ClimateMapping/VacuumMapping's `*_dp`
    fields) were stored exactly as YAML parsed them, unlike the plain
    `dps:` list's `id` (already `int()`-cast). A hand-edited profile with
    an accidentally-quoted dp id (`brightness_dp: "22"` instead of `22`)
    silently never matches coordinator.data's int keys - the entity just
    always reads None, no error anywhere. Every composite `*_dp`/`*_scale`
    field below is now coerced the same way `dps:` already was."""
    if value is None:
        return None
    return int(value)


def parse_profile(yaml_text: str) -> DeviceProfile:
    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict) or not any(k in raw for k in ("dps", "lights", "climates", "vacuums")):
        raise ValueError("Profile must be a mapping with at least one of 'dps'/'lights'/'climates'/'vacuums'")

    dps = []
    for entry in raw.get("dps", []):
        dps.append(
            DPMapping(
                dp_id=int(entry["id"]),
                platform=entry["platform"],
                name=entry.get("name", f"DP {entry['id']}"),
                device_class=entry.get("device_class"),
                unit=entry.get("unit"),
                scale=entry.get("scale"),
                invert=entry.get("invert", False),
                value_map=entry.get("map"),
                min_value=entry.get("min"),
                max_value=entry.get("max"),
                step=entry.get("step"),
                icon=entry.get("icon"),
                bit=_int_or_none(entry.get("bit")),
            )
        )
    lights = []
    for entry in raw.get("lights", []):
        lights.append(
            LightMapping(
                name=entry.get("name", "Light"),
                switch_dp=int(entry["switch_dp"]),
                brightness_dp=_int_or_none(entry.get("brightness_dp")),
                brightness_min=entry.get("brightness_min", 0),
                brightness_max=entry.get("brightness_max", 255),
                color_temp_dp=_int_or_none(entry.get("color_temp_dp")),
                color_temp_min=entry.get("color_temp_min", 0),
                color_temp_max=entry.get("color_temp_max", 255),
                color_dp=_int_or_none(entry.get("color_dp")),
                color_h_max=entry.get("color_h_max", 360),
                color_s_max=entry.get("color_s_max", 1000),
                color_v_max=entry.get("color_v_max", 1000),
                work_mode_dp=_int_or_none(entry.get("work_mode_dp")),
                work_mode_white=entry.get("work_mode_white", "white"),
                work_mode_colour=entry.get("work_mode_colour", "colour"),
                icon=entry.get("icon"),
            )
        )

    climates = []
    for entry in raw.get("climates", []):
        climates.append(
            ClimateMapping(
                name=entry.get("name", "Climate"),
                switch_dp=_int_or_none(entry.get("switch_dp")),
                current_temp_dp=_int_or_none(entry.get("current_temp_dp")),
                current_temp_scale=entry.get("current_temp_scale"),
                target_temp_dp=_int_or_none(entry.get("target_temp_dp")),
                target_temp_scale=entry.get("target_temp_scale"),
                target_temp_min=entry.get("target_temp_min", 5),
                target_temp_max=entry.get("target_temp_max", 35),
                target_temp_step=entry.get("target_temp_step", 0.5),
                mode_dp=_int_or_none(entry.get("mode_dp")),
                mode_map=entry.get("mode_map"),
                fan_dp=_int_or_none(entry.get("fan_dp")),
                fan_map=entry.get("fan_map"),
                preset_dp=_int_or_none(entry.get("preset_dp")),
                preset_map=entry.get("preset_map"),
                humidity_dp=_int_or_none(entry.get("humidity_dp")),
                swing_dp=_int_or_none(entry.get("swing_dp")),
                swing_map=entry.get("swing_map"),
                icon=entry.get("icon"),
            )
        )

    vacuums = []
    for entry in raw.get("vacuums", []):
        vacuums.append(
            VacuumMapping(
                name=entry.get("name", "Vacuum"),
                start_dp=_int_or_none(entry.get("start_dp")),
                start_map=entry.get("start_map"),
                pause_dp=_int_or_none(entry.get("pause_dp")),
                return_dp=_int_or_none(entry.get("return_dp")),
                locate_dp=_int_or_none(entry.get("locate_dp")),
                battery_dp=_int_or_none(entry.get("battery_dp")),
                battery_scale=entry.get("battery_scale"),
                status_dp=_int_or_none(entry.get("status_dp")),
                status_map=entry.get("status_map"),
                fan_speed_dp=_int_or_none(entry.get("fan_speed_dp")),
                fan_speed_map=entry.get("fan_speed_map"),
                icon=entry.get("icon"),
            )
        )

    return DeviceProfile(
        name=raw.get("name", "Custom profile"),
        dps=dps,
        lights=lights,
        climates=climates,
        vacuums=vacuums,
        product_ids=raw.get("product_ids", []),
    )


def profile_to_yaml(profile: DeviceProfile) -> str:
    raw = {
        "name": profile.name,
        "product_ids": profile.product_ids,
        "dps": [
            {
                k: v
                for k, v in {
                    "id": d.dp_id,
                    "platform": d.platform,
                    "name": d.name,
                    "device_class": d.device_class,
                    "unit": d.unit,
                    "scale": d.scale,
                    "invert": d.invert or None,
                    "map": d.value_map,
                    "min": d.min_value,
                    "max": d.max_value,
                    "step": d.step,
                    "icon": d.icon,
                    "bit": d.bit,
                }.items()
                if v is not None
            }
            for d in profile.dps
        ],
        "lights": [
            {
                k: v
                for k, v in {
                    "name": lm.name,
                    "switch_dp": lm.switch_dp,
                    "brightness_dp": lm.brightness_dp,
                    "brightness_min": lm.brightness_min,
                    "brightness_max": lm.brightness_max,
                    "color_temp_dp": lm.color_temp_dp,
                    "color_temp_min": lm.color_temp_min,
                    "color_temp_max": lm.color_temp_max,
                    "color_dp": lm.color_dp,
                    "color_h_max": lm.color_h_max,
                    "color_s_max": lm.color_s_max,
                    "color_v_max": lm.color_v_max,
                    "work_mode_dp": lm.work_mode_dp,
                    "work_mode_white": lm.work_mode_white,
                    "work_mode_colour": lm.work_mode_colour,
                    "icon": lm.icon,
                }.items()
                if v is not None
            }
            for lm in profile.lights
        ],
        "climates": [
            {
                k: v
                for k, v in {
                    "name": cm.name,
                    "switch_dp": cm.switch_dp,
                    "current_temp_dp": cm.current_temp_dp,
                    "current_temp_scale": cm.current_temp_scale,
                    "target_temp_dp": cm.target_temp_dp,
                    "target_temp_scale": cm.target_temp_scale,
                    "target_temp_min": cm.target_temp_min,
                    "target_temp_max": cm.target_temp_max,
                    "target_temp_step": cm.target_temp_step,
                    "mode_dp": cm.mode_dp,
                    "mode_map": cm.mode_map,
                    "fan_dp": cm.fan_dp,
                    "fan_map": cm.fan_map,
                    "preset_dp": cm.preset_dp,
                    "preset_map": cm.preset_map,
                    "humidity_dp": cm.humidity_dp,
                    "swing_dp": cm.swing_dp,
                    "swing_map": cm.swing_map,
                    "icon": cm.icon,
                }.items()
                if v is not None
            }
            for cm in profile.climates
        ],
        "vacuums": [
            {
                k: v
                for k, v in {
                    "name": vm.name,
                    "start_dp": vm.start_dp,
                    "start_map": vm.start_map,
                    "pause_dp": vm.pause_dp,
                    "return_dp": vm.return_dp,
                    "locate_dp": vm.locate_dp,
                    "battery_dp": vm.battery_dp,
                    "battery_scale": vm.battery_scale,
                    "status_dp": vm.status_dp,
                    "status_map": vm.status_map,
                    "fan_speed_dp": vm.fan_speed_dp,
                    "fan_speed_map": vm.fan_speed_map,
                    "icon": vm.icon,
                }.items()
                if v is not None
            }
            for vm in profile.vacuums
        ],
    }
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
