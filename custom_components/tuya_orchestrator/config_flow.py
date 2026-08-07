"""Config flow: cloud login (local_key extraction only) -> LAN discovery ->
device pick -> profile pick/edit -> done. Everything after setup runs 100%
on LAN; the cloud step's only job is fetching each device's local_key,
which Tuya does not expose any other (documented) way.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROFILE_YAML,
    CONF_PROTOCOL_VERSION,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_UID,
    DEFAULT_PROTOCOL_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SUPPORTED_PROTOCOL_VERSIONS,
    TUYA_REGIONS,
)
from .auto_profile import build_profile_from_schema
from .discovery import discover_devices
from .profile import DeviceProfile, parse_profile, profile_to_yaml
from .tuya_cloud import TuyaCloudApi, TuyaCloudAuthError, TuyaCloudApiError

_LOGGER = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent / "profiles"


def _builtin_profiles() -> dict[str, DeviceProfile]:
    profiles = {}
    if PROFILES_DIR.exists():
        for f in PROFILES_DIR.glob("*.yaml"):
            try:
                profiles[f.stem] = parse_profile(f.read_text(encoding="utf-8"))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Skipping invalid built-in profile %s: %s", f, err)
    return profiles


def _match_profile(profiles: dict[str, DeviceProfile], product_id: str | None) -> str | None:
    if not product_id:
        return None
    for key, profile in profiles.items():
        if product_id in profile.product_ids:
            return key
    return None


class TuyaOrchestratorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cloud_devices: list[dict[str, Any]] = []
        self._discovered: dict[str, Any] = {}
        self._chosen_device: dict[str, Any] | None = None
        self._chosen_ip: str | None = None
        self._auto_profile_yaml: str | None = None
        self._cloud_api: TuyaCloudApi | None = None

    # -- step 1: how do we get the local_key? --------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "manual"])

    # -- step 2a: cloud credentials (only used to fetch local_keys) ----------
    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = TuyaCloudApi(
                session,
                user_input[CONF_REGION],
                user_input[CONF_ACCESS_ID],
                user_input[CONF_ACCESS_SECRET],
            )
            try:
                await api.validate()
                self._cloud_devices = await api.get_user_devices(user_input[CONF_UID])
                self._cloud_api = api
            except TuyaCloudAuthError:
                errors["base"] = "invalid_auth"
            except (TuyaCloudApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                if not self._cloud_devices:
                    errors["base"] = "no_devices_found"
                else:
                    self._cloud_creds = user_input
                    return await self.async_step_pick_cloud_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default="eu"): vol.In(list(TUYA_REGIONS)),
                vol.Required(CONF_ACCESS_ID): str,
                vol.Required(CONF_ACCESS_SECRET): str,
                vol.Required(CONF_UID): str,
            }
        )
        return self.async_show_form(step_id="cloud", data_schema=schema, errors=errors)

    async def async_step_pick_cloud_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        options = {d["device_id"]: f"{d['name']} ({d['device_id']})" for d in self._cloud_devices}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            self._chosen_device = next(d for d in self._cloud_devices if d["device_id"] == device_id)
            self._discovered = await discover_devices()
            found = self._discovered.get(device_id)
            if not found:
                errors["base"] = "not_found_on_lan"
            else:
                self._chosen_ip = found.ip
                await self._build_auto_profile()
                return await self.async_step_profile()

        schema = vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(options)})
        return self.async_show_form(step_id="pick_cloud_device", data_schema=schema, errors=errors)

    async def _build_auto_profile(self) -> None:
        """Fetch this device's real DP schema from the cloud (code + dp_id +
        type per DP, not just its local_key) and auto-build a profile from
        it - see auto_profile.py. Best-effort: any failure here just means
        the "Auto-detected" option won't be offered in the profile step,
        falling back to built-in/custom - never blocks onboarding."""
        if self._cloud_api is None or self._chosen_device is None:
            return
        try:
            schema = await self._cloud_api.get_device_schema(self._chosen_device["device_id"])
            profile = build_profile_from_schema(
                name=self._chosen_device["name"],
                category=self._chosen_device.get("category"),
                product_id=self._chosen_device.get("product_id"),
                schema=schema,
            )
            if profile.dps or profile.lights or profile.climates or profile.vacuums:
                header = (
                    "# Auto-generated from this device's real Tuya Cloud DP schema.\n"
                    "# Review before saving - Tuya's own cloud metadata is sometimes\n"
                    "# wrong (a real example found during development: an AC's declared\n"
                    "# max temperature was 88 degC, copy-pasted from its Fahrenheit DP's\n"
                    "# range). Numeric min/max/step and enum labels below are taken\n"
                    "# as-is from the cloud; fix anything that looks implausible.\n"
                )
                self._auto_profile_yaml = header + profile_to_yaml(profile)
        except Exception as err:  # noqa: BLE001 - best-effort, never block onboarding
            _LOGGER.warning("Auto-profile generation failed for %s: %s", self._chosen_device["device_id"], err)

    # -- step 2b: fully manual (IP + device id + local_key already known) ----
    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._chosen_device = {
                "device_id": user_input[CONF_DEVICE_ID],
                "local_key": user_input[CONF_LOCAL_KEY],
                "name": user_input[CONF_DEVICE_ID],
                "product_id": None,
            }
            self._chosen_ip = user_input["address"]
            self._manual_protocol = user_input[CONF_PROTOCOL_VERSION]
            return await self.async_step_profile()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_LOCAL_KEY): str,
                vol.Required("address"): str,
                vol.Required(CONF_PROTOCOL_VERSION, default=DEFAULT_PROTOCOL_VERSION): vol.In(
                    SUPPORTED_PROTOCOL_VERSIONS
                ),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    # -- step 3: review the auto-detected profile, or pick a built-in / -----
    #    paste a custom one instead ------------------------------------------
    async def async_step_profile(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        profiles = _builtin_profiles()
        builtin_match = _match_profile(profiles, self._chosen_device.get("product_id"))

        options = {}
        if self._auto_profile_yaml:
            options["auto"] = "Auto-detected from this device's real DP schema (recommended)"
        options.update({k: p.name for k, p in profiles.items()})
        options["custom"] = "Custom (paste YAML)"

        default_choice = "auto" if self._auto_profile_yaml else (builtin_match or "custom")
        default_yaml = self._auto_profile_yaml or (
            (PROFILES_DIR / f"{builtin_match}.yaml").read_text(encoding="utf-8") if builtin_match else ""
        )

        if user_input is not None:
            choice = user_input["profile_choice"]
            if choice == "auto":
                yaml_text = self._auto_profile_yaml or ""
            elif choice == "custom":
                yaml_text = user_input.get(CONF_PROFILE_YAML, "")
            else:
                yaml_text = (PROFILES_DIR / f"{choice}.yaml").read_text(encoding="utf-8")
            try:
                parse_profile(yaml_text)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Invalid profile submitted: %s", err)
                errors["base"] = "invalid_profile"
            else:
                return self._create_entry(yaml_text)

        schema = vol.Schema(
            {
                vol.Required("profile_choice", default=default_choice): vol.In(options),
                vol.Optional(CONF_PROFILE_YAML, default=default_yaml): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )
        return self.async_show_form(step_id="profile", data_schema=schema, errors=errors)

    def _create_entry(self, profile_yaml: str) -> FlowResult:
        device = self._chosen_device
        data = {
            CONF_DEVICE_ID: device["device_id"],
            CONF_LOCAL_KEY: device.get("local_key"),
            "address": self._chosen_ip,
            CONF_PROTOCOL_VERSION: getattr(self, "_manual_protocol", DEFAULT_PROTOCOL_VERSION),
            CONF_PROFILE_YAML: profile_yaml,
        }
        return self.async_create_entry(title=device["name"], data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "TuyaOrchestratorOptionsFlow":
        return TuyaOrchestratorOptionsFlow()


class TuyaOrchestratorOptionsFlow(config_entries.OptionsFlow):
    """Edit the device's profile or polling fallback interval after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current_yaml = self.config_entry.data.get(CONF_PROFILE_YAML, "")
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parse_profile(user_input[CONF_PROFILE_YAML])
            except Exception as err:  # noqa: BLE001
                errors["base"] = "invalid_profile"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_PROFILE_YAML: user_input[CONF_PROFILE_YAML]},
                )
                return self.async_create_entry(
                    title="", data={CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL]}
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE_YAML, default=current_yaml): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): int,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
