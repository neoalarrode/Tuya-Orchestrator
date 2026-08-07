"""Config flow: two entry points.

- "account" (recommended): Tuya Cloud credentials only. Creates one
  ConfigEntry with no device of its own - its background poller
  (account.py) then offers every device it finds as a native HA discovery
  flow (`async_step_integration_discovery` below), shown as a
  "Discovered" card with Configure/Ignore, same UX as HomeKit Controller
  or Tapo. Clicking Configure resumes straight into the same profile
  review step as before (auto-generated from the device's real DP schema,
  always editable, never created blind).
- "manual": a single device, no cloud, no discovery - IP/device_id/
  local_key entered by hand, same as always.
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

from .auto_profile import build_profile_from_schema
from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_ACCOUNT_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_PROFILE_YAML,
    CONF_PROTOCOL_VERSION,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_UID,
    DEFAULT_PROTOCOL_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_ACCOUNT,
    ENTRY_TYPE_DEVICE,
    SUPPORTED_PROTOCOL_VERSIONS,
    TUYA_REGIONS,
)
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
        self._chosen_device: dict[str, Any] | None = None
        self._chosen_ip: str | None = None
        self._auto_profile_yaml: str | None = None
        self._cloud_api: TuyaCloudApi | None = None
        self._manual_protocol: str = DEFAULT_PROTOCOL_VERSION

    # -- step 1: account (recommended) vs fully manual single device --------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="user", menu_options=["account", "manual"])

    # -- account setup: cloud credentials only, no device picked here -------
    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> FlowResult:
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
                devices = await api.get_user_devices(user_input[CONF_UID])
            except TuyaCloudAuthError:
                errors["base"] = "invalid_auth"
            except (TuyaCloudApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices_found"
                else:
                    await self.async_set_unique_id(f"account_{user_input[CONF_ACCESS_ID]}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Tuya Cloud ({user_input[CONF_UID]})",
                        data={CONF_ENTRY_TYPE: ENTRY_TYPE_ACCOUNT, **user_input},
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default="eu"): vol.In(list(TUYA_REGIONS)),
                vol.Required(CONF_ACCESS_ID): str,
                vol.Required(CONF_ACCESS_SECRET): str,
                vol.Required(CONF_UID): str,
            }
        )
        return self.async_show_form(step_id="account", data_schema=schema, errors=errors)

    # -- entry point the account's background poller triggers per device ----
    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> FlowResult:
        device_id = discovery_info["device_id"]
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        self._chosen_device = discovery_info
        self._chosen_ip = discovery_info.get("ip")
        self.context["title_placeholders"] = {"name": discovery_info.get("name", device_id)}

        version = discovery_info.get("version")
        if self._chosen_ip is None:
            # Not seen in the account poller's last LAN pass - try once more,
            # right now, before giving up and asking the user for a static IP.
            found = (await discover_devices()).get(device_id)
            self._chosen_ip = found.ip if found else None
            version = found.version if found else version
        if version and version not in ("3.1", "3.3"):
            # Protocol 3.4/3.5 isn't implemented yet (see tuya_lan.py) -
            # abort here with a clear reason instead of creating an entry
            # that's guaranteed to fail at setup.
            return self.async_abort(reason="unsupported_protocol_version")
        if version in SUPPORTED_PROTOCOL_VERSIONS:
            self._manual_protocol = version

        account_entry = self.hass.config_entries.async_get_entry(discovery_info.get(CONF_ACCOUNT_ENTRY_ID))
        if account_entry is not None:
            session = async_get_clientsession(self.hass)
            self._cloud_api = TuyaCloudApi(
                session,
                account_entry.data[CONF_REGION],
                account_entry.data[CONF_ACCESS_ID],
                account_entry.data[CONF_ACCESS_SECRET],
            )
            await self._build_auto_profile()

        if self._chosen_ip is None:
            return await self.async_step_discovery_ip()
        return await self.async_step_profile()

    async def async_step_discovery_ip(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Device known via cloud but not currently seen on the LAN broadcast -
        ask for a static IP rather than blocking the whole discovery card."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._chosen_ip = user_input["address"]
            return await self.async_step_profile()

        schema = vol.Schema({vol.Required("address"): str})
        return self.async_show_form(
            step_id="discovery_ip",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": self._chosen_device.get("name", "")},
        )

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
            profile, warnings = build_profile_from_schema(
                name=self._chosen_device["name"],
                category=self._chosen_device.get("category"),
                product_id=self._chosen_device.get("product_id"),
                schema=schema,
            )
            if profile.dps or profile.lights or profile.climates or profile.vacuums:
                header_lines = [
                    "# Auto-generated from this device's real Tuya Cloud DP schema.",
                    "# Review before saving - numeric min/max/step and enum labels are",
                    "# taken as-is from the cloud, which is not always right.",
                ]
                if warnings:
                    header_lines.append("#")
                    header_lines.append("# SPECIFIC ISSUES FOUND IN THIS DEVICE'S DATA - fix before saving:")
                    for w in warnings:
                        header_lines.append(f"#  - {w}")
                header = "\n".join(header_lines) + "\n"
                self._auto_profile_yaml = header + profile_to_yaml(profile)
        except Exception as err:  # noqa: BLE001 - best-effort, never block onboarding
            _LOGGER.warning("Auto-profile generation failed for %s: %s", self._chosen_device["device_id"], err)

    # -- fully manual (IP + device id + local_key already known, no cloud) --
    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured()
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

    # -- review the auto-detected profile, or pick a built-in / paste one ---
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
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_DEVICE_ID: device["device_id"],
            CONF_LOCAL_KEY: device.get("local_key"),
            "address": self._chosen_ip,
            CONF_PROTOCOL_VERSION: self._manual_protocol,
            CONF_PROFILE_YAML: profile_yaml,
        }
        return self.async_create_entry(title=device["name"], data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "TuyaOrchestratorOptionsFlow":
        return TuyaOrchestratorOptionsFlow()


class TuyaOrchestratorOptionsFlow(config_entries.OptionsFlow):
    """Edit a device entry's profile or polling fallback interval after
    setup. Not shown for "account" entries - nothing to configure there
    beyond re-adding it (delete + re-add if credentials change)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ACCOUNT:
            return self.async_abort(reason="account_entry_no_options")

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
