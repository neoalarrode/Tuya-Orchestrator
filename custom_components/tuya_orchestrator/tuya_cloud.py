"""Tuya Cloud (OpenAPI) client - used ONLY during config_flow to fetch each
device's local_key. Nothing in this module is called again once a device is
configured; normal operation is 100% LAN (see tuya_lan.py).

Uses only the official, documented Tuya OpenAPI endpoints and the
documented HMAC-SHA256 request signing algorithm:
https://developer.tuya.com/en/docs/iot/api-request

Requires a (free) Tuya IoT Platform "Cloud" project with the device's app
account linked under Devices -> Link Tuya App Account - the UID shown there
is what this integration asks for. This is the same official mechanism
LocalTuya/Tuya's own integration document, just without a third-party SDK.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

from .const import TUYA_REGIONS

_LOGGER = logging.getLogger(__name__)


class TuyaCloudAuthError(Exception):
    """Raised on bad credentials / signature rejection."""


class TuyaCloudApiError(Exception):
    """Raised on any other non-success API response."""


class TuyaCloudApi:
    def __init__(self, session: aiohttp.ClientSession, region: str, access_id: str, access_secret: str) -> None:
        self._session = session
        self._base = TUYA_REGIONS[region]
        self._access_id = access_id
        self._access_secret = access_secret
        self._token: str | None = None
        self._token_expires_at: float = 0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        result = await self._request("GET", "/v1.0/token?grant_type=1", signed_with_token=False)
        self._token = result["access_token"]
        self._token_expires_at = time.time() + result.get("expire_time", 7200)
        return self._token

    def _sign(self, method: str, path: str, body: str, token: str | None, t: str) -> str:
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        string_to_sign = "\n".join([method, content_hash, "", path])
        message = self._access_id + (token or "") + t + string_to_sign
        return hmac.new(
            self._access_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest().upper()

    async def _request(
        self, method: str, path: str, body: dict | None = None, signed_with_token: bool = True
    ) -> dict[str, Any]:
        body_str = "" if body is None else json.dumps(body, separators=(",", ":"))
        t = str(int(time.time() * 1000))
        token = await self._get_token() if signed_with_token else None
        sign = self._sign(method, path, body_str, token, t)
        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if token:
            headers["access_token"] = token

        async with self._session.request(
            method, self._base + path, headers=headers, data=body_str or None
        ) as resp:
            data = await resp.json()

        if not data.get("success"):
            code = data.get("code")
            msg = data.get("msg")
            if code in (1004, 1013, 1010):  # sign/token related
                raise TuyaCloudAuthError(f"{code}: {msg}")
            raise TuyaCloudApiError(f"{code}: {msg}")
        return data.get("result", {})

    async def validate(self) -> None:
        """Raise if credentials are bad. Used by config_flow to fail fast."""
        await self._get_token()

    async def get_user_devices(self, uid: str) -> list[dict[str, Any]]:
        """Return the linked app account's devices (id, name, product_id, local_key, category)."""
        result = await self._request("GET", f"/v1.0/users/{uid}/devices")
        # v1.0 result is a list directly
        devices = result if isinstance(result, list) else result.get("devices", [])
        return [
            {
                "device_id": d["id"],
                "name": d.get("name") or d["id"],
                "product_id": d.get("product_id"),
                "category": d.get("category"),
                "local_key": d.get("local_key"),
                "online": d.get("online", False),
            }
            for d in devices
        ]

    async def get_device_schema(self, device_id: str) -> list[dict[str, Any]]:
        """Return this device's real DP schema, normalized to a common
        shape regardless of which cloud endpoint(s) actually had it:

            {"code": str, "dp_id": int, "type": "bool"|"value"|"enum"|
             "bitmap"|"string"|"json"|"raw", "access": "rw"|"ro"|"wr",
             "values": dict}   # unit/min/max/scale/step or range, as given

        ALWAYS queries both the standard v1.1 "specification" endpoint AND
        the newer v2.0 "Thing Data Model" endpoint and merges them by
        dp_id (v1.1 wins on a genuine conflict, v2.0 fills in whatever
        v1.1 didn't have) - fixed after a real report: v1.1 can return
        `success: true` with a genuinely PARTIAL schema (observed on a
        real AC - v1.1 gave only 6 DPs, missing fan speed/sleep mode/swing/
        air quality/... entirely, while v2.0 for the SAME device had ~25).
        `success` was never a completeness guarantee; only trying v2.0 as
        a fallback-on-error (the previous behavior) silently missed
        real functionality whenever v1.1 "succeeded" but was incomplete -
        which is apparently not a rare edge case. Only raises if BOTH
        endpoints fail outright.
        """
        entries_by_dp: dict[int, dict[str, Any]] = {}
        errors: list[Exception] = []

        # BUG FIXED HERE: this used to catch only TuyaCloudApiError - but
        # _request() can also raise TuyaCloudAuthError (a separate class,
        # not a subclass), e.g. on a transient token race. That escaped
        # uncaught past this method's whole "try both endpoints, only fail
        # if both fail" resilience design, meaning a bad v1.1 token call
        # prevented v2.0 from ever being tried at all, even though a fresh
        # token fetch on the v2.0 attempt could well have succeeded.
        try:
            result = await self._request("GET", f"/v1.1/devices/{device_id}/specifications")
            for e in _normalize_v11_schema(result):
                entries_by_dp[e["dp_id"]] = e
        except (TuyaCloudApiError, TuyaCloudAuthError) as err:
            errors.append(err)

        try:
            result = await self._request("GET", f"/v2.0/cloud/thing/{device_id}/model")
            for e in _normalize_v20_schema(result):
                entries_by_dp.setdefault(e["dp_id"], e)
        except (TuyaCloudApiError, TuyaCloudAuthError) as err:
            errors.append(err)

        if not entries_by_dp and errors:
            raise errors[0]
        return list(entries_by_dp.values())


def _normalize_v11_schema(result: dict[str, Any]) -> list[dict[str, Any]]:
    functions = {f["code"]: f for f in result.get("functions", []) if f.get("dp_id") is not None}
    statuses = {s["code"]: s for s in result.get("status", []) if s.get("dp_id") is not None}
    entries: dict[str, dict[str, Any]] = {}
    for code, s in statuses.items():
        access = "rw" if code in functions else "ro"
        entries[code] = _entry(code, s["dp_id"], s["type"], access, s.get("values"))
    for code, f in functions.items():
        if code in entries:
            continue
        entries[code] = _entry(code, f["dp_id"], f["type"], "wr", f.get("values"))
    return list(entries.values())


def _normalize_v20_schema(result: dict[str, Any]) -> list[dict[str, Any]]:
    model = result.get("model")
    model = json.loads(model) if isinstance(model, str) else (model or {})
    entries = []
    for service in model.get("services", []):
        for prop in service.get("properties", []):
            type_spec = prop.get("typeSpec", {})
            entries.append(
                _entry(
                    prop["code"],
                    prop["abilityId"],
                    type_spec.get("type", "raw"),
                    prop.get("accessMode", "ro"),
                    type_spec,
                )
            )
    return entries


_TYPE_NORMALIZE = {
    "boolean": "bool",
    "bool": "bool",
    "integer": "value",
    "value": "value",
    "enum": "enum",
    "bitmap": "bitmap",
    "string": "string",
    "json": "json",
    "raw": "raw",
}


def _entry(code: str, dp_id: int, raw_type: str, access: str, values: Any) -> dict[str, Any]:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (ValueError, TypeError):
            values = {}
    return {
        "code": code,
        "dp_id": int(dp_id),
        "type": _TYPE_NORMALIZE.get(str(raw_type).lower(), "raw"),
        "access": access,
        "values": values or {},
    }
