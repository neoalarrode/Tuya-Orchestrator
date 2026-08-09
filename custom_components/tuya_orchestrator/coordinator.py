"""Per-device coordinator: owns the LAN connection, decodes DPs via the
device's profile, and pushes updates to entities in real time.

Reactive by design (matches this project family's philosophy, see
Climate Orchestrator): the LAN socket itself delivers unsolicited DP-change
frames (`on_update` callback in TuyaLocalDevice), so entities update
instantly on a real device change. `DataUpdateCoordinator`'s periodic
`_async_update_data` is kept only as a slow-interval fallback/reconnect
check (`DEFAULT_SCAN_INTERVAL`), not the primary data path.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .profile import DeviceProfile
from .tuya_lan import TuyaLocalDevice

_LOGGER = logging.getLogger(__name__)

# Consecutive empty polls before warning that a device is unreadable. A few
# empty replies are normal (a device with nothing to report yet); a steady
# run of them is not.
_UNDECODABLE_POLLS_BEFORE_WARNING = 3


class TuyaOrchestratorCoordinator(DataUpdateCoordinator[dict[int, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        device: TuyaLocalDevice,
        profile: DeviceProfile,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.device_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device = device
        self.profile = profile
        self._undecodable_polls = 0
        device._on_update = self._handle_push  # noqa: SLF001 - internal wiring
        # Mirrors localtuya's `disconnected()` -> dispatch None -> entities
        # go unavailable. Without this, a connection that dropped between
        # two polls left every entity showing its last known value as
        # though it were live.
        device._on_disconnect = self._handle_disconnect  # noqa: SLF001

    def _handle_disconnect(self) -> None:
        self.async_set_update_error(
            UpdateFailed(f"Lost LAN connection to {self.device.device_id}")
        )

    def _handle_push(self, dps: dict[int, Any]) -> None:
        merged = dict(self.data or {})
        merged.update(dps)
        self.async_set_updated_data(merged)

    async def _async_update_data(self) -> dict[int, Any]:
        # BUG FIXED HERE: this used to `return` the raw status() result
        # directly, which DataUpdateCoordinator uses to REPLACE self.data
        # wholesale - but a real Tuya device's DP_QUERY reply is not
        # guaranteed to include every DP every time (some report only a
        # subset, or an initial near-empty ack before the real values
        # arrive as separate push frames handled by _handle_push above).
        # Every periodic poll could silently wipe out previously-known DP
        # values the fresh reply simply didn't repeat - matching a live
        # report of entities never showing a current value. Merge instead,
        # exactly like _handle_push already (correctly) does for pushes.
        try:
            fresh = await self.device.status()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Could not reach device on LAN: {err}") from err
        _LOGGER.debug("%s: DP_QUERY returned %s", self.device.device_id, fresh)
        # GAP FIXED HERE: a device answering every query with something we
        # cannot decrypt looked exactly like a healthy device with nothing
        # to say - connected, heartbeats fine, entities simply empty
        # forever, and not one line in the log. That is the signature of a
        # WRONG local_key or, more often, a right key pointed at the wrong
        # host: found on a live instance, where one entry had been given
        # another device's IP by an old active-scan false positive and had
        # sat there with zero datapoints ever since. Say so, once, instead
        # of failing silently.
        if not fresh and not self.data:
            self._undecodable_polls += 1
            if self._undecodable_polls == _UNDECODABLE_POLLS_BEFORE_WARNING:
                _LOGGER.warning(
                    "%s at %s: connected, but %d consecutive queries returned no usable data. "
                    "The device is replying and we cannot read it - typically a local_key that "
                    "does not belong to whatever is actually at this address. Check that %s is "
                    "really this device.",
                    self.device.device_id,
                    self.device.address,
                    self._undecodable_polls,
                    self.device.address,
                )
        elif fresh:
            self._undecodable_polls = 0
        merged = dict(self.data or {})
        merged.update(fresh)
        return merged

    async def async_set_dp(self, dp_id: int, raw_value: Any) -> None:
        await self.device.set_dps({dp_id: raw_value})
        # Optimistic local update; a real push/poll will confirm shortly after.
        self._handle_push({dp_id: raw_value})
