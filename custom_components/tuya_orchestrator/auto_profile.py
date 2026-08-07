"""Automatic profile generation from a device's real Tuya Cloud schema.

This is the actual answer to "DP ids differ per device, don't hardcode
them": at pairing time (the one moment this integration talks to the
cloud, see tuya_cloud.py) it reads the device's OWN declared DP schema
(`code` + numeric `dp_id` + type, via `TuyaCloudApi.get_device_schema`) and
builds a profile from that - by matching each `code` (Tuya's *semantic*
name, stable across many products even when the numeric dp_id isn't) to a
role in a composite entity (climate/light/vacuum), gated by the device's
own `category` to avoid cross-category ambiguity (e.g. "mode" means
something different on an AC than on a vacuum). Anything not consumed by a
composite role gets auto-typed into a plain entity from its Tuya `type`
(Boolean->switch/binary_sensor, Integer->number/sensor, Enum->select/
sensor, ...) - no hardcoded product_id, no hand-authored YAML required.

The generated profile is always shown to the user in the config flow's
"profile" step before creation, editable/fixable there - this replaces
guessing with a real starting point, not a black box.

Manually-authored profiles in `profiles/*.yaml` remain available as a
fallback/manual override path (paste-your-own), but are no longer the
primary mechanism.
"""
from __future__ import annotations

from typing import Any

from .profile import ClimateMapping, DeviceProfile, DPMapping, LightMapping, VacuumMapping

# ---------------------------------------------------------------------------
# code -> role tables, one per HA composite entity type. Gated by Tuya
# `category` (kt/qn=climate-like, dj=light, sd=vacuum) so the same code
# string ("mode", "switch"...) is never resolved ambiguously across
# unrelated device types.
# ---------------------------------------------------------------------------
_CLIMATE_CATEGORIES = {"kt", "qn"}
_LIGHT_CATEGORIES = {"dj"}
_VACUUM_CATEGORIES = {"sd"}

# Tuples, not sets: order is the preference order when a device happens to
# expose more than one candidate code for the same role (e.g. both swing
# axes) - `_find_role` takes the first match, so first-listed wins.
_CLIMATE_ROLE_CODES = {
    "power": ("switch", "power", "switch_1"),
    "target_temp": ("temp_set",),
    "current_temp": ("temp_current",),
    "mode": ("mode", "work_mode"),
    "fan": ("windspeed", "fan_speed", "level", "speed"),
    "humidity": ("humidity_current",),
    # intensity-style modes that aren't real off/heat/cool HVAC modes (e.g.
    # a heater's High/Low, an AC's sleep timer curve) -> preset_mode instead
    "preset": ("sleep",),
    # only ONE swing axis is modeled (HA's climate entity has a single
    # swing_mode dimension) - up/down preferred over left/right when a
    # device has both; the other stays a plain dps: select entry.
    "swing": ("up_down_sweep", "swing", "shake", "left_right_sweep"),
}
_LIGHT_ROLE_CODES = {
    "power": ("switch_led", "switch", "switch_1", "power"),
    "brightness": ("bright_value_v2", "bright_value"),
    "color_temp": ("temp_value_v2", "temp_value"),
    "color": ("colour_data_v2", "colour_data"),
    "work_mode": ("work_mode",),
}
_VACUUM_ROLE_CODES = {
    "start": ("power_go", "start_clean", "start"),
    "pause": ("pause",),
    "return": ("switch_charge", "return_home", "go_home"),
    "locate": ("find_robot", "findrobot", "seek"),
    "battery": ("electricity_left", "electricity", "battery_percentage"),
    "status": ("status", "workstatus", "work_status"),
    "fan_speed": ("suction", "fanstatus", "fan_speed", "cistern"),
}


def _find_role(by_code: dict[str, dict], role_codes: dict[str, tuple[str, ...]]) -> dict[str, dict]:
    """Return {role: schema_entry} for whichever roles matched a code
    present on this device (case-insensitive match against the device's
    real codes). `role_codes` values are ORDERED - first match wins."""
    lower_map = {code.lower(): code for code in by_code}
    found = {}
    for role, candidates in role_codes.items():
        for candidate in candidates:
            if candidate in lower_map:
                found[role] = by_code[lower_map[candidate]]
                break
    return found


def _humanize(code: str) -> str:
    words = code.replace("-", "_").split("_")
    return " ".join(w.upper() if w.lower() in ("led", "id", "ac", "rgb") else w.capitalize() for w in words if w)


def _label_for_enum_value(raw: str, field_name: str | None = None) -> str:
    """Label for one enum option. `_humanize` alone is a no-op on a purely
    numeric raw value (Tuya position/level DPs are commonly encoded as
    plain digit strings like "0"/"1"/"2" with no semantic name anywhere in
    the cloud schema this integration reads) - "0" stayed "0", useless in
    a select/swing-mode dropdown. Real example that prompted this: an AC's
    swing positions showed as bare "0"/"1"/"2"/"3". Never invents what a
    position MEANS (e.g. guessing "0 = Off") - just makes each option
    identifiable by field name + index instead of a bare, ambiguous digit.
    """
    if raw.lstrip("-").isdigit():
        prefix = f"{field_name} " if field_name else ""
        return f"{prefix}Position {raw}"
    return _humanize(raw)


_PLAUSIBLE_SETPOINT_MAX_C = 50  # no real consumer AC/heater setpoint goes above this


def _try_build_climate(
    by_code: dict[str, dict], consumed: set[str], warnings: list[str]
) -> ClimateMapping | None:
    roles = _find_role(by_code, _CLIMATE_ROLE_CODES)
    if "power" not in roles or "target_temp" not in roles:
        return None  # not enough of a real thermostat shape to justify one entity

    cm = ClimateMapping(name="Climate")
    cm.switch_dp = roles["power"]["dp_id"]
    consumed.add(roles["power"]["code"])

    t = roles["target_temp"]
    values = t.get("values", {})
    scale = 10 ** values.get("scale", 0) if values.get("scale") else None
    cm.target_temp_dp = t["dp_id"]
    cm.target_temp_scale = scale
    if values.get("min") is not None:
        cm.target_temp_min = values["min"] / scale if scale else values["min"]
    if values.get("max") is not None:
        cm.target_temp_max = values["max"] / scale if scale else values["max"]
    if values.get("step") is not None:
        cm.target_temp_step = (values["step"] / scale) if scale else values["step"]
    consumed.add(t["code"])

    # Sanity check, not a correction: Tuya's own cloud metadata has been
    # observed to be flatly wrong here (a real AC's declared max was 88 -
    # copy-pasted from its parallel Fahrenheit DP's range). Never silently
    # invent a "fixed" number - that would be its own kind of black box -
    # just make the implausible value impossible to miss in the reviewed
    # YAML instead of relying on a generic boilerplate warning nobody reads.
    if cm.target_temp_max > _PLAUSIBLE_SETPOINT_MAX_C:
        warnings.append(
            f"climate '{t['code']}' (dp {cm.target_temp_dp}): declared max "
            f"temperature is {cm.target_temp_max}°C - implausible for a "
            f"thermostat setpoint, Tuya's cloud metadata is very likely "
            f"wrong here (a real example: 88°C copy-pasted from a parallel "
            f"Fahrenheit DP). Fix `target_temp_max` below before saving."
        )

    if "current_temp" in roles:
        c = roles["current_temp"]
        cv = c.get("values", {})
        cm.current_temp_dp = c["dp_id"]
        if cv.get("scale"):
            cm.current_temp_scale = 10 ** cv["scale"]
        consumed.add(c["code"])

    if "mode" in roles:
        m = roles["mode"]
        raw_range = m.get("values", {}).get("range", [])
        # Only treat as a real HVAC mode DP if its values look like actual
        # HVAC modes; otherwise leave it OUT of the climate entity (a plain
        # dps: select entry is safer than guessing a bad hvac_mode mapping).
        hvac_like = {"cold": "cool", "cool": "cool", "hot": "heat", "heat": "heat", "wet": "dry", "dry": "dry",
                     "wind": "fan_only", "fan": "fan_only", "auto": "heat_cool"}
        mode_map = {raw: hvac_like[raw] for raw in raw_range if raw in hvac_like}
        if mode_map:
            cm.mode_dp = m["dp_id"]
            cm.mode_map = mode_map
            consumed.add(m["code"])

    if "fan" in roles:
        f = roles["fan"]
        raw_range = f.get("values", {}).get("range", [])
        if raw_range:
            cm.fan_dp = f["dp_id"]
            cm.fan_map = {raw: _label_for_enum_value(raw, "Fan") for raw in raw_range}
            consumed.add(f["code"])

    if "humidity" in roles:
        h = roles["humidity"]
        cm.humidity_dp = h["dp_id"]
        consumed.add(h["code"])

    if "preset" in roles:
        p = roles["preset"]
        raw_range = p.get("values", {}).get("range", [])
        if raw_range:
            cm.preset_dp = p["dp_id"]
            cm.preset_map = {raw: _label_for_enum_value(raw, "Preset") for raw in raw_range}
            consumed.add(p["code"])

    if "swing" in roles:
        sw = roles["swing"]
        raw_range = sw.get("values", {}).get("range", [])
        if raw_range:
            cm.swing_dp = sw["dp_id"]
            field_name = _humanize(sw["code"])  # e.g. "Up Down Sweep" - more specific than a bare "Swing"
            cm.swing_map = {raw: _label_for_enum_value(raw, field_name) for raw in raw_range}
            consumed.add(sw["code"])

    return cm


def _try_build_light(by_code: dict[str, dict], consumed: set[str]) -> LightMapping | None:
    roles = _find_role(by_code, _LIGHT_ROLE_CODES)
    if "power" not in roles:
        return None
    if not ({"brightness", "color_temp", "color"} & roles.keys()):
        return None  # a bare on/off "light" is just a switch, not worth a light entity

    lm = LightMapping(name="Light", switch_dp=roles["power"]["dp_id"])
    consumed.add(roles["power"]["code"])

    if "brightness" in roles:
        b = roles["brightness"]
        bv = b.get("values", {})
        lm.brightness_dp = b["dp_id"]
        lm.brightness_min = bv.get("min", 0)
        lm.brightness_max = bv.get("max", 255)
        consumed.add(b["code"])

    if "color_temp" in roles:
        c = roles["color_temp"]
        cv = c.get("values", {})
        lm.color_temp_dp = c["dp_id"]
        lm.color_temp_min = cv.get("min", 0)
        lm.color_temp_max = cv.get("max", 255)
        consumed.add(c["code"])

    if "color" in roles:
        col = roles["color"]
        cv = col.get("values", {})
        lm.color_dp = col["dp_id"]
        lm.color_h_max = cv.get("h", {}).get("max", 360) if isinstance(cv.get("h"), dict) else 360
        lm.color_s_max = cv.get("s", {}).get("max", 1000) if isinstance(cv.get("s"), dict) else 1000
        lm.color_v_max = cv.get("v", {}).get("max", 1000) if isinstance(cv.get("v"), dict) else 1000
        consumed.add(col["code"])

    if "work_mode" in roles and "color" in roles:
        wm = roles["work_mode"]
        raw_range = wm.get("values", {}).get("range", [])
        white = next((r for r in raw_range if "white" in r.lower()), None)
        colour = next((r for r in raw_range if r.lower() in ("colour", "color")), None)
        if white and colour:
            lm.work_mode_dp = wm["dp_id"]
            lm.work_mode_white = white
            lm.work_mode_colour = colour
            consumed.add(wm["code"])

    return lm


def _try_build_vacuum(by_code: dict[str, dict], consumed: set[str]) -> VacuumMapping | None:
    roles = _find_role(by_code, _VACUUM_ROLE_CODES)
    if "start" not in roles:
        return None

    vm = VacuumMapping(name="Vacuum")
    start = roles["start"]
    vm.start_dp = start["dp_id"]
    raw_range = start.get("values", {}).get("range")
    if start["type"] == "enum" and raw_range:
        # A shared start/pause enum DP (e.g. "0"/"1") - best-effort guess at
        # which raw value means which; genuinely ambiguous without the
        # device's own description text, flagged for the user to check.
        vm.start_map = {raw_range[0]: "pause", raw_range[-1]: "start"} if len(raw_range) >= 2 else None
    consumed.add(start["code"])

    if "pause" in roles:
        vm.pause_dp = roles["pause"]["dp_id"]
        consumed.add(roles["pause"]["code"])
    if "return" in roles:
        vm.return_dp = roles["return"]["dp_id"]
        consumed.add(roles["return"]["code"])
    if "locate" in roles:
        vm.locate_dp = roles["locate"]["dp_id"]
        consumed.add(roles["locate"]["code"])
    if "battery" in roles:
        b = roles["battery"]
        vm.battery_dp = b["dp_id"]
        if b.get("values", {}).get("scale"):
            vm.battery_scale = 10 ** b["values"]["scale"]
        consumed.add(b["code"])
    if "status" in roles:
        s = roles["status"]
        raw_range = s.get("values", {}).get("range", [])
        status_map = _guess_vacuum_status_map(raw_range)
        if status_map:
            vm.status_dp = s["dp_id"]
            vm.status_map = status_map
            consumed.add(s["code"])
    if "fan_speed" in roles:
        f = roles["fan_speed"]
        raw_range = f.get("values", {}).get("range", [])
        if raw_range:
            vm.fan_speed_dp = f["dp_id"]
            vm.fan_speed_map = {raw: _label_for_enum_value(raw, "Fan speed") for raw in raw_range}
            consumed.add(f["code"])

    return vm


def _guess_vacuum_status_map(raw_range: list[str]) -> dict[str, str] | None:
    """Best-effort raw-status -> HA VacuumActivity mapping by keyword. Left
    unmapped (returns None) if nothing recognizable - a raw `sensor` entry
    for the status DP is safer than a wrong `activity`, added as a plain
    dps: fallback by the caller in that case."""
    guess = {}
    for raw in raw_range:
        low = raw.lower()
        if "charg" in low or "dock" in low:
            guess[raw] = "docked"
        elif "clean" in low and "comp" not in low:
            guess[raw] = "cleaning"
        elif "pause" in low:
            guess[raw] = "paused"
        elif "find" in low or "return" in low or "goto_charge" in low:
            guess[raw] = "returning"
        elif "sleep" in low or "standby" in low or "idle" in low or "halt" in low:
            guess[raw] = "idle"
        elif "error" in low or "fault" in low:
            guess[raw] = "error"
    return guess or None


def _auto_dp_mapping(entry: dict[str, Any]) -> DPMapping | None:
    """Generic fallback for any DP not consumed by a composite entity:
    auto-typed purely from Tuya's declared `type` + `access`."""
    code, dp_id, dtype, access, values = entry["code"], entry["dp_id"], entry["type"], entry["access"], entry["values"]
    name = _humanize(code)

    if dtype == "bool":
        platform = "switch" if access in ("rw", "wr") else "binary_sensor"
        return DPMapping(dp_id=dp_id, platform=platform, name=name)

    if dtype == "value":
        scale = 10 ** values["scale"] if values.get("scale") else None
        unit = values.get("unit") or None
        platform = "number" if access == "rw" else "sensor"
        kwargs = dict(dp_id=dp_id, platform=platform, name=name, unit=unit, scale=scale)
        if platform == "number":
            if values.get("min") is not None:
                kwargs["min_value"] = values["min"] / scale if scale else values["min"]
            if values.get("max") is not None:
                kwargs["max_value"] = values["max"] / scale if scale else values["max"]
            if values.get("step") is not None:
                kwargs["step"] = (values["step"] / scale) if scale else values["step"]
        return DPMapping(**kwargs)

    if dtype == "enum":
        raw_range = values.get("range", [])
        platform = "select" if access in ("rw", "wr") else "sensor"
        value_map = {raw: _label_for_enum_value(raw, name) for raw in raw_range} if raw_range else None
        return DPMapping(dp_id=dp_id, platform=platform, name=name, value_map=value_map)

    # bitmap / string / raw / json - expose as a raw, undecoded sensor
    # rather than silently dropping the DP (visibility over guessing).
    return DPMapping(dp_id=dp_id, platform="sensor", name=f"{name} (raw)")


def build_profile_from_schema(
    name: str, category: str | None, product_id: str | None, schema: list[dict[str, Any]]
) -> tuple[DeviceProfile, list[str]]:
    """Returns (profile, warnings). `warnings` flags specific fields worth
    double-checking (e.g. an implausible numeric range straight from Tuya's
    own cloud metadata) - surfaced prominently by the config flow instead
    of relying on a generic "review this" boilerplate nobody reads twice."""
    by_code = {e["code"]: e for e in schema}
    consumed: set[str] = set()
    warnings: list[str] = []

    lights: list[LightMapping] = []
    climates: list[ClimateMapping] = []
    vacuums: list[VacuumMapping] = []

    if category in _CLIMATE_CATEGORIES:
        if (cm := _try_build_climate(by_code, consumed, warnings)) is not None:
            climates.append(cm)
    elif category in _LIGHT_CATEGORIES:
        if (lm := _try_build_light(by_code, consumed)) is not None:
            lights.append(lm)
    elif category in _VACUUM_CATEGORIES:
        if (vm := _try_build_vacuum(by_code, consumed)) is not None:
            vacuums.append(vm)

    # Hide unit-twin DPs of anything already consumed - Tuya's convention
    # is a plain "_f" suffix for the Fahrenheit twin of an already-exposed
    # Celsius DP (temp_set/temp_set_f, temp_current/temp_current_f...).
    # Real example that prompted this: an AC's climate entity already
    # exposes target_temperature in °C (from temp_set); temp_set_f was
    # still showing up as a second, disconnected, redundant setpoint
    # control for the exact same physical value. Only hides it when the
    # Celsius twin was ACTUALLY consumed (climate/light/vacuum build
    # succeeded) - an unmatched _f-suffixed DP on its own still surfaces
    # normally, nothing is dropped silently for devices with no composite.
    lower_consumed = {c.lower() for c in consumed}
    for entry in schema:
        code = entry["code"]
        if code.lower().endswith("_f") and code.lower()[:-2] in lower_consumed:
            consumed.add(code)
            warnings.append(
                f"'{code}' (dp {entry['dp_id']}) looks like the Fahrenheit twin of "
                f"an already-exposed Celsius DP - hidden to avoid a duplicate, "
                f"disconnected setpoint control. Add it back manually under `dps:` "
                f"if you actually want both units controllable separately."
            )

    dps = [
        mapping
        for entry in schema
        if entry["code"] not in consumed
        for mapping in [_auto_dp_mapping(entry)]
        if mapping is not None
    ]

    profile = DeviceProfile(
        name=name,
        dps=dps,
        lights=lights,
        climates=climates,
        vacuums=vacuums,
        product_ids=[product_id] if product_id else [],
    )
    return profile, warnings
