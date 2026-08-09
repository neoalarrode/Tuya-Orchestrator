"""Tuya Orchestrator - fully local (LAN) Tuya device integration with
declarative, auto-generated device profiles (ESPHome-style customization,
Tuya-over-LAN instead of ESPHome-over-WiFi).

Two kinds of ConfigEntry:
- "account" (no device of its own): Tuya Cloud credentials, runs a
  background poller (account.py) that offers newly-seen devices as native
  HA discovery flows (Configure/Ignore cards).
- "device" (one per paired device, same as before): the actual LAN
  connection + entities.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account import async_setup_account
from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_PROFILE_YAML,
    CONF_PROTOCOL_VERSION,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_UID,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_DATA_KEY,
    FAILED_TRACES_KEY,
    DOMAIN,
    ENTRY_TYPE_ACCOUNT,
    PLATFORMS,
    RECONNECT_INTERVAL,
)
from .coordinator import TuyaOrchestratorCoordinator
from .discovery import DiscoveredDevice, PersistentDiscovery
from .profile import parse_profile
from .tuya_cloud import TuyaCloudApi
from .tuya_lan import TuyaLocalDevice

_LOGGER = logging.getLogger(__name__)


async def _async_try_connect(device: TuyaLocalDevice) -> None:
    """Best-effort reconnect - a failure here is normal (device still
    booting, briefly unreachable) and is retried by the next broadcast or
    the next RECONNECT_INTERVAL tick, so it must never raise."""
    try:
        await device.connect()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Tuya Orchestrator: reconnect to %s failed: %s", device.device_id, err)


def _snapshot_failure(device: TuyaLocalDevice, err: Exception) -> dict:
    """State + frame trace to keep when a device entry fails to set up.

    GAP FIXED HERE: on a failed setup the device object was simply dropped,
    taking its frame trace with it - so diagnostics.py had nothing to report
    for exactly the entries worth diagnosing (a failing entry showed only
    `{"loaded": false}`, confirmed on a live instance). Keeping this lets the
    next diagnostics download show what actually went over the wire before
    it gave up.
    """
    return {
        "error": f"{type(err).__name__}: {err}",
        "address": device.address,
        "protocol_version": device.protocol_version,
        "dev_type": device.dev_type,
        "session_key_negotiated": device.local_key != device.real_local_key,
        "sequence_counter": device._seq,  # noqa: SLF001
        "pending_by_sequence": sorted(device._pending),  # noqa: SLF001
        "pending_by_command": [f"0x{c:02x}" for c in device._pending_cmd],  # noqa: SLF001
        "frame_trace": device.trace(),
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Called ONCE per HA startup, before any ConfigEntry is set up -
    starts the LAN broadcast listener here so it stays open and
    accumulating for the WHOLE session, matching localtuya's own
    `__init__.py` (its `TuyaDiscovery` is started exactly this way, not
    per-poll). See `discovery.PersistentDiscovery`'s docstring for why an
    ephemeral few-second listen-and-close window per poll wasn't reliable
    enough - found by reviewing localtuya's full codebase after a live
    report that devices confirmed present on the LAN still weren't being
    discovered even after v0.2.9's port/framing fixes."""

    def _on_device_seen(device: DiscoveredDevice) -> None:
        # BUG FIXED HERE (v0.7.0): a configured device's IP is a plain DHCP
        # lease, not something guaranteed to stay put forever - when it
        # changed, this integration kept dialing the OLD address forever,
        # forcing the user to remove and re-pair the device by hand to
        # "notice" the new one. localtuya reacts to every broadcast it
        # hears (`_device_discovered` in its `__init__.py`) and, on an IP
        # mismatch against the stored entry, calls
        # `hass.config_entries.async_update_entry(...)` with the new
        # address - that alone is enough, because HA calls every
        # registered `add_update_listener` (already wired below, in
        # `_async_setup_device_entry`) on ANY entry data change, which
        # reloads the entry and reconnects with the fresh IP. Same
        # mechanism ported here, 1:1.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_DEVICE_ID) != device.device_id:
                continue

            if entry.data.get("address") != device.ip:
                _LOGGER.info(
                    "Tuya Orchestrator: device %s changed IP %s -> %s, updating and reloading",
                    device.device_id,
                    entry.data.get("address"),
                    device.ip,
                )
                new_data = dict(entry.data)
                new_data["address"] = device.ip
                # This alone reconnects: HA fires the entry's update
                # listener, which reloads the entry (tearing the old
                # connection down), so do NOT also try to connect here -
                # same reasoning as localtuya's own comment on this branch
                # ("Updating settings triggers a reload of the config
                # entry, which tears down the device so no need to
                # connect in that case").
                hass.config_entries.async_update_entry(entry, data=new_data)
                break

            # Same IP, but we just HEARD from it - localtuya's
            # `_device_discovered` ends by calling `device.async_connect()`
            # for any device it isn't currently connected to, on every
            # broadcast. That matters: a device that reboots keeps its DHCP
            # lease (so the branch above never fires) but has dropped our
            # TCP connection - a broadcast is proof it's back NOW, so
            # reconnecting immediately beats waiting up to a full
            # RECONNECT_INTERVAL for the periodic retry to notice.
            stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            local_device = stored.get("device") if isinstance(stored, dict) else None
            if local_device is not None and not local_device.connected:
                _LOGGER.debug(
                    "Tuya Orchestrator: heard from disconnected device %s, reconnecting now",
                    device.device_id,
                )
                hass.async_create_task(_async_try_connect(local_device))
            break

    listener = PersistentDiscovery(on_device=_on_device_seen)
    await listener.start()
    hass.data.setdefault(DOMAIN, {})[DISCOVERY_DATA_KEY] = listener

    async def _async_reconnect(now) -> None:
        # Companion fix to the above: an IP-unchanged disconnect (device
        # rebooted, briefly lost power/wifi, TCP reset...) doesn't trigger
        # a reload on its own - matches localtuya's own periodic
        # `_async_reconnect` (`RECONNECT_INTERVAL`), which retries any
        # device it still has marked as disconnected instead of waiting
        # for the user to intervene.
        for stored in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(stored, dict) or "device" not in stored:
                continue
            device: TuyaLocalDevice = stored["device"]
            if device.connected:
                continue
            _LOGGER.debug("Tuya Orchestrator: retrying connection to %s", device.device_id)
            await _async_try_connect(device)

    hass.data[DOMAIN]["_reconnect_unsub"] = async_track_time_interval(
        hass, _async_reconnect, timedelta(seconds=RECONNECT_INTERVAL)
    )

    def _on_stop(event) -> None:
        listener.close()
        hass.data[DOMAIN]["_reconnect_unsub"]()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return True


async def _async_refresh_local_key(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Re-fetch this device's local_key from the Tuya cloud and rewrite the
    entry if it changed. Returns True only if a genuinely DIFFERENT key was
    stored (i.e. retrying is now worthwhile). Best-effort: any failure just
    returns False, leaving the original connection error to be reported."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    account = next(
        (
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT
        ),
        None,
    )
    if account is None or device_id is None:
        return False

    try:
        api = TuyaCloudApi(
            async_get_clientsession(hass),
            account.data[CONF_REGION],
            account.data[CONF_ACCESS_ID],
            account.data[CONF_ACCESS_SECRET],
        )
        devices = await api.get_user_devices(account.data[CONF_UID])
    except Exception as err:  # noqa: BLE001 - purely a recovery attempt
        _LOGGER.debug("Could not refresh local_key for %s: %s", device_id, err)
        return False

    fresh = next((d for d in devices if d["device_id"] == device_id), None)
    if fresh is None or not fresh.get("local_key"):
        return False
    if fresh["local_key"] == entry.data.get(CONF_LOCAL_KEY):
        return False  # key is fine - the connection failed for another reason

    _LOGGER.info(
        "Tuya Orchestrator: local_key for %s changed in the cloud (device re-paired?) - updating",
        device_id,
    )
    new_data = dict(entry.data)
    new_data[CONF_LOCAL_KEY] = fresh["local_key"]
    hass.config_entries.async_update_entry(entry, data=new_data)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
        await async_setup_account(hass, entry)
        return True
    return await _async_setup_device_entry(hass, entry)


async def _async_setup_device_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    profile = parse_profile(data[CONF_PROFILE_YAML])

    device = TuyaLocalDevice(
        device_id=data[CONF_DEVICE_ID],
        address=entry.data["address"],
        local_key=data[CONF_LOCAL_KEY],
        protocol_version=data.get(CONF_PROTOCOL_VERSION, "3.3"),
    )
    # Must be registered BEFORE the first status(): if this device turns
    # out to speak the type_0d dialect, its very first query already needs
    # the explicit DP list (see DEV_TYPE_0D in tuya_lan.py). The reference
    # wires this identically, from its configured entity list.
    device.add_dps_to_request(profile.all_dp_ids())
    # BUG FIXED HERE: a connection failure (device briefly offline, reset,
    # 3.4 handshake failure...) used to propagate as a raw exception - HA
    # logs that as a scary "Error setting up entry" traceback and does NOT
    # treat it as a normal retry-able state (that traceback is exactly
    # what a live report pasted for a ConnectionResetError). Tuya devices
    # going briefly unreachable is routine, not exceptional - the correct
    # HA pattern is ConfigEntryNotReady, which shows a clean "not ready,
    # will retry" status and actually retries on HA's own schedule.
    try:
        await device.connect()
    except Exception as err:  # noqa: BLE001
        hass.data.setdefault(DOMAIN, {}).setdefault(FAILED_TRACES_KEY, {})[
            entry.entry_id
        ] = _snapshot_failure(device, err)
        # GAP FIXED HERE (vs the reference's `update_local_key()`): Tuya
        # rotates a device's local_key whenever it is re-paired from the
        # phone app - a completely routine thing for a user to do. With a
        # stale key the LAN handshake fails forever and the only fix was
        # deleting and re-adding the device by hand. The reference
        # re-fetches the key from the cloud and rewrites the entry; do the
        # same, on a best-effort basis (it needs an account entry to be
        # configured, and it only helps if the key genuinely changed).
        if await _async_refresh_local_key(hass, entry):
            raise ConfigEntryNotReady(
                f"local_key for {data[CONF_DEVICE_ID]} was stale and has been refreshed from "
                "the Tuya cloud; retrying with the new key"
            ) from err
        raise ConfigEntryNotReady(f"Could not connect to {data[CONF_DEVICE_ID]} at {entry.data['address']}: {err}") from err

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = TuyaOrchestratorCoordinator(hass, device, profile, scan_interval)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        hass.data.setdefault(DOMAIN, {}).setdefault(FAILED_TRACES_KEY, {})[
            entry.entry_id
        ] = _snapshot_failure(device, err)
        await device.close()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "device": device,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
        return True  # nothing to unload beyond the poller's own async_on_unload
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id)
        await stored["device"].close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
