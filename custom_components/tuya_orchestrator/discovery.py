"""UDP broadcast discovery for Tuya LAN devices.

Tuya devices periodically broadcast their presence (gwId + ip + product
key + protocol version) on two fixed UDP ports:

- 6666: unencrypted, plain JSON.
- 6667: encrypted with a fixed, publicly-documented key (UDP_KEY_ENCRYPTED),
  same AES-ECB scheme as the LAN control protocol.

This lets us resolve "device_id -> current LAN IP" without the user typing
IPs by hand, and re-resolve automatically if a device's DHCP lease changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .const import DISCOVERY_TIMEOUT, UDP_KEY_ENCRYPTED, UDP_PORT_ENCRYPTED, UDP_PORT_UNENCRYPTED

_LOGGER = logging.getLogger(__name__)


@dataclass
class DiscoveredDevice:
    device_id: str
    ip: str
    product_key: str | None
    version: str | None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, encrypted: bool, results: dict[str, DiscoveredDevice]) -> None:
        self.encrypted = encrypted
        self.results = results

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        try:
            # Strip the same 16-byte header/4-byte footer framing used by
            # the control protocol (see tuya_lan.py PREFIX/SUFFIX/HEADER_SIZE).
            payload = data[20:-8] if len(data) > 28 else data
            if self.encrypted:
                cipher = AES.new(UDP_KEY_ENCRYPTED, AES.MODE_ECB)
                payload = unpad(cipher.decrypt(payload), 16)
            obj = json.loads(payload)
        except Exception:  # noqa: BLE001 - best-effort discovery, skip bad frames
            return
        gw_id = obj.get("gwId")
        if not gw_id:
            return
        self.results[gw_id] = DiscoveredDevice(
            device_id=gw_id,
            ip=obj.get("ip", addr[0]),
            product_key=obj.get("productKey"),
            version=obj.get("version"),
        )


async def discover_devices(timeout: float = DISCOVERY_TIMEOUT) -> dict[str, DiscoveredDevice]:
    """Listen on both broadcast ports for `timeout` seconds, return devices found."""
    loop = asyncio.get_event_loop()
    results: dict[str, DiscoveredDevice] = {}

    transports = []
    for port, encrypted in ((UDP_PORT_UNENCRYPTED, False), (UDP_PORT_ENCRYPTED, True)):
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda enc=encrypted: _DiscoveryProtocol(enc, results),
                local_addr=("0.0.0.0", port),
                allow_broadcast=True,
            )
            transports.append(transport)
        except OSError as err:
            _LOGGER.warning("Could not bind discovery port %s: %s", port, err)

    try:
        await asyncio.sleep(timeout)
    finally:
        for t in transports:
            t.close()

    return results
