"""UDP broadcast discovery for Tuya LAN devices.

Tuya devices periodically broadcast their presence (gwId + ip + product
key + protocol version) on two fixed UDP ports:

- 6666: unencrypted, plain JSON.
- 6667: encrypted with a fixed, publicly-documented key (UDP_KEY_ENCRYPTED),
  same AES-ECB scheme as the LAN control protocol.

This lets us resolve "device_id -> current LAN IP" without the user typing
IPs by hand, and re-resolve automatically if a device's DHCP lease changes.

Real-world bug fixed here: on a host that already has something else
listening on 6666/6667 (LocalTuya, the official Tuya integration, a
previous instance of this one...) binding failed outright with "Address
already in use" (errno 98) and discovery silently found nothing, every
time - not a timing issue, a real port conflict. Fixed by building the
socket manually with SO_REUSEADDR (+ SO_REUSEPORT where the platform
supports it) BEFORE binding, so multiple listeners can coexist on the same
port - `asyncio.create_datagram_endpoint(local_addr=...)` does not set
these by default. Confirmed against a live HA report, not theoretical -
also verified locally that a second bind to an already-bound port succeeds
once both sides request SO_REUSEADDR/SO_REUSEPORT.

CAVEAT: on Linux, SO_REUSEPORT only allows coexistence if EVERY process
binding that port sets it - if whatever else is holding 6666/6667 does
NOT set SO_REUSEPORT itself, the bind can still fail here even with this
fix. If that happens, devices simply won't be auto-discovered (use the
manual IP entry path in config_flow instead); this integration will never
be the one preventing another Tuya integration from working, only
possibly the other way around.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
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


def _bind_udp_socket(port: int) -> socket.socket:
    """Bind a UDP socket on `port` with SO_REUSEADDR/SO_REUSEPORT set BEFORE
    bind(), so this integration can coexist with LocalTuya/the official Tuya
    integration/other listeners on the same well-known Tuya broadcast port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):  # not available on Windows
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass  # best-effort; SO_REUSEADDR alone still helps on most platforms
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    return sock


async def discover_devices(timeout: float = DISCOVERY_TIMEOUT) -> dict[str, DiscoveredDevice]:
    """Listen on both broadcast ports for `timeout` seconds, return devices found."""
    loop = asyncio.get_event_loop()
    results: dict[str, DiscoveredDevice] = {}

    transports = []
    for port, encrypted in ((UDP_PORT_UNENCRYPTED, False), (UDP_PORT_ENCRYPTED, True)):
        try:
            sock = _bind_udp_socket(port)
        except OSError as err:
            _LOGGER.warning(
                "Could not bind discovery port %s even with SO_REUSEADDR/SO_REUSEPORT "
                "(%s) - another process may be holding it exclusively",
                port,
                err,
            )
            continue
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda enc=encrypted: _DiscoveryProtocol(enc, results),
                sock=sock,
            )
        except OSError as err:
            _LOGGER.warning("Could not attach to discovery port %s: %s", port, err)
            sock.close()
            continue
        transports.append(transport)

    if not transports:
        _LOGGER.error(
            "Discovery could not bind ANY UDP port (6666/6667) - devices will never be "
            "found this way; use manual IP entry instead"
        )

    try:
        await asyncio.sleep(timeout)
    finally:
        for t in transports:
            t.close()

    return results
