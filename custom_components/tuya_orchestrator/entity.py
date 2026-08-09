"""Shared entity base for all platforms."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TuyaOrchestratorCoordinator
from .profile import DPMapping


class TuyaOrchestratorEntity(CoordinatorEntity[TuyaOrchestratorCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TuyaOrchestratorCoordinator, mapping: DPMapping) -> None:
        super().__init__(coordinator)
        self._mapping = mapping
        device_id = coordinator.device.device_id
        # BUG FIXED HERE: two DPMapping entries sharing the same dp_id (a
        # real, intended case - see profile.py's `bit:` field docstring,
        # e.g. this AC's display-light/buzzer switches both live on dp 123
        # as different bits) produced IDENTICAL unique_ids without the bit
        # suffix - HA silently drops one of the colliding entities.
        suffix = f"_bit{mapping.bit}" if mapping.bit is not None else ""
        self._attr_unique_id = f"{device_id}_{mapping.dp_id}{suffix}"
        self._attr_name = mapping.name
        if mapping.icon:
            self._attr_icon = mapping.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.profile.name,
            manufacturer="Tuya",
            model=coordinator.profile.name,
        )

    @property
    def _raw(self):
        return (self.coordinator.data or {}).get(self._mapping.dp_id)
