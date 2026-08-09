"""Sensor platform - one entity per DP mapped with platform: sensor, plus
composite entities' companion sensors (currently: a vacuum's battery)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaOrchestratorCoordinator
from .entity import TuyaOrchestratorEntity
from .profile import VacuumMapping


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mappings = coordinator.profile.dps_for_platform("sensor")
    entities = [TuyaSensor(coordinator, m) for m in mappings]
    entities += [
        TuyaVacuumBatterySensor(coordinator, vm) for vm in coordinator.profile.vacuums if vm.battery_dp is not None
    ]
    async_add_entities(entities)


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


class TuyaVacuumBatterySensor(CoordinatorEntity[TuyaOrchestratorCoordinator], SensorEntity):
    """Companion battery sensor for a `vacuum:` mapping's `battery_dp`.

    BUG FIXED HERE (live report against HA 2026.8): `vacuum.py` used to
    expose battery via the deprecated `StateVacuumEntity.battery_level`
    property + `VacuumEntityFeature.BATTERY` - HA now logs a deprecation
    warning for both. The modern replacement is exactly this: a plain
    `sensor.*` entity (`device_class: battery`) on the SAME device, which
    HA's vacuum more-info dialog picks up automatically via the shared
    device link - no feature flag or special vacuum-entity property
    needed at all.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: TuyaOrchestratorCoordinator, mapping: VacuumMapping) -> None:
        super().__init__(coordinator)
        self._mapping = mapping
        device_id = coordinator.device.device_id
        self._attr_unique_id = f"{device_id}_vacuum_{mapping.start_dp}_battery"
        self._attr_name = "Battery"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.profile.name,
            manufacturer="Tuya",
            model=coordinator.profile.name,
        )

    @property
    def native_value(self) -> int | None:
        data: dict[int, Any] = self.coordinator.data or {}
        raw = data.get(self._mapping.battery_dp)
        if raw is None:
            return None
        return round(raw / self._mapping.battery_scale) if self._mapping.battery_scale else round(raw)
