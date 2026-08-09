"""UDP broadcast discovery for Tuya LAN devices.

Tuya devices periodically broadcast their presence (gwId + ip + product
key + protocol version) on THREE fixed UDP ports:

- 6666: unencrypted, plain JSON (protocol 3.1 era).
- 6667: encrypted with a fixed, publicly-documented key, same AES-ECB
  scheme as the LAN control protocol (protocol 3.3 era).
- 7000: "Tuya app" broadcast port - found missing entirely after a real
  report ("dejaron de ofrecerse... hay dos partes del protocolo, solo
  implementaste uno"), confirmed against tinytuya's scanner.py
  (`UDPPORTAPP = 7000`) which listens on all three. Newer/app-paired
  devices commonly broadcast here.

This lets us resolve "device_id -> current LAN IP" without the user typing
IPs by hand, and re-resolve automatically if a device's DHCP lease changes.

Cross-checked directly against localtuya's discovery.py AND tinytuya's
core/udp_helper.py (both `master`/current). Two prior real bugs fixed here
(v0.2.2): the AES key needed an MD5 derivation step it was missing, plus a
one-character typo in the seed string.

**Which framing a packet uses is determined by a prefix INSIDE the packet,
not by which port it arrived on** (tinytuya's own decoder is portless
for exactly this reason - a given device's protocol generation decides its
format, independent of port):

- prefix `0x000055AA`: the classic frame this integration's LAN control
  protocol also uses (see tuya_lan.py) - AES-ECB encrypted (or, on 6666,
  sometimes already-plaintext JSON) payload, WITH the same 4-byte retcode
  field between header and payload that tuya_lan.py's receive path needed
  fixing for (see that module's v0.2.7 fix - applies here identically).
- prefix `0x00006699`: a newer, HMAC-based frame used by protocol 3.4+
  devices' broadcasts. Genuinely NOT implemented here (matches this
  project's existing, explicit 3.4/3.5 scope gap for the control
  protocol) - detected and logged at debug level so a device using it
  shows up as a clear "unsupported format" line instead of vanishing
  silently into the same catch-all as a malformed/unrelated packet.
- anything else (no valid 16-byte Tuya header at all): last-resort
  fallback, try decrypting the ENTIRE raw datagram directly (matches
  tinytuya's own fallback for legacy/non-standard broadcast shapes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import md5

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .const import (
    DISCOVERY_TIMEOUT,
    UDP_KEY_SEED,
    UDP_PORT_APP,
    UDP_PORT_ENCRYPTED,
    UDP_PORT_UNENCRYPTED,
)

_LOGGER = logging.getLogger(__name__)

UDP_KEY_ENCRYPTED = md5(UDP_KEY_SEED).digest()

PREFIX_55AA = 0x000055AA
PREFIX_6699 = 0x00006699
_HEADER_SIZE = 16  # prefix+seq+command+length (same as tuya_lan.py)
_RETCODE_SIZE = 4  # only present on device->us frames, see tuya_lan.py's fix
_FOOTER_SIZE = 8  # crc32+suffix


@dataclass
class DiscoveredDevice:
    device_id: str
    ip: str
    product_key: str | None
    version: str | None


def _decrypt_55aa(data: bytes) -> bytes | None:
    """Extract + decrypt a classic 0x55AA-framed broadcast payload,
    accounting for the retcode field (same fix as tuya_lan.py v0.2.7)."""
    if len(data) < _HEADER_SIZE:
        return None
    _prefix, _seq, _cmd, length = struct.unpack(">IIII", data[:_HEADER_SIZE])
    payload_start = _HEADER_SIZE + _RETCODE_SIZE
    payload_len = length - _RETCODE_SIZE - _FOOTER_SIZE
    payload = data[payload_start : payload_start + max(payload_len, 0)]
    if not payload:
        return None
    if payload[:1] == b"{" and payload[-1:] == b"}":
        return payload  # already-plaintext JSON (common on port 6666)
    try:
        cipher = AES.new(UDP_KEY_ENCRYPTED, AES.MODE_ECB)
        return unpad(cipher.decrypt(payload), 16)
    except (ValueError, KeyError):
        return None


def _decode_broadcast(data: bytes) -> dict | None:
    """Decode one broadcast datagram regardless of which port it arrived
    on - see module docstring for the three cases handled."""
    if len(data) >= 4:
        (prefix,) = struct.unpack(">I", data[:4])
        if prefix == PREFIX_55AA:
            payload = _decrypt_55aa(data)
            if payload is None:
                return None
            try:
                return json.loads(payload)
            except (ValueError, TypeError):
                return None
        if prefix == PREFIX_6699:
            _LOGGER.debug(
                "Discovery: received a 0x6699-framed (protocol 3.4+ app) broadcast - "
                "this format is not implemented yet, skipping"
            )
            return None
    # Fallback: no recognizable header at all - try decrypting the whole
    # raw datagram directly (matches tinytuya's own last-resort path).
    try:
        cipher = AES.new(UDP_KEY_ENCRYPTED, AES.MODE_ECB)
        payload = unpad(cipher.decrypt(data), 16)
        return json.loads(payload)
    except Exception:  # noqa: BLE001 - best-effort discovery, skip bad frames
        return None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        results: dict[str, DiscoveredDevice],
        on_device: "Callable[[DiscoveredDevice], None] | None" = None,
    ) -> None:
        self.results = results
        self._on_device = on_device

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        obj = _decode_broadcast(data)
        if not obj:
            return
        gw_id = obj.get("gwId")
        if not gw_id:
            return
        device = DiscoveredDevice(
            device_id=gw_id,
            ip=obj.get("ip", addr[0]),
            product_key=obj.get("productKey"),
            version=obj.get("version"),
        )
        self.results[gw_id] = device
        # BUG FIXED HERE (v0.7.0): the cache was updated but nothing was ever
        # told about it - a configured device's IP changing (a normal DHCP
        # lease renewal, not an error) silently went stale in the cache while
        # the actual running connection kept using the OLD address forever,
        # forcing the device to be removed and re-added by hand to pick up
        # the new IP. localtuya's own `__init__.py` calls its discovery
        # callback (`_device_discovered`) on EVERY broadcast heard, precisely
        # so it can react live to an IP change - see __init__.py's
        # `_on_device_seen` for the update_entry side of this fix.
        if self._on_device is not None:
            self._on_device(device)


async def _bind_all_ports(
    results: dict[str, DiscoveredDevice],
    on_device: "Callable[[DiscoveredDevice], None] | None" = None,
) -> list:
    loop = asyncio.get_event_loop()
    transports = []
    for port in (UDP_PORT_UNENCRYPTED, UDP_PORT_ENCRYPTED, UDP_PORT_APP):
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(results, on_device),
                local_addr=("0.0.0.0", port),
                reuse_port=True,
            )
        except OSError as err:
            _LOGGER.warning(
                "Could not bind discovery port %s even with reuse_port=True (%s) - "
                "another process may be holding it exclusively",
                port,
                err,
            )
            continue
        transports.append(transport)

    if not transports:
        _LOGGER.error(
            "Discovery could not bind ANY UDP port (6666/6667/7000) - devices will "
            "never be found this way; use manual IP entry instead"
        )
    return transports


class PersistentDiscovery:
    """Long-lived broadcast listener, kept open for the WHOLE Home Assistant
    session (started once in async_setup(), closed on HA shutdown) - not a
    short listen-and-close window.

    Real gap fixed here, found reviewing localtuya's __init__.py end to end
    after the ported/generalized discovery.py fixes (v0.2.2/v0.2.9) alone
    didn't resolve a live "still not discovering" report: localtuya starts
    exactly ONE persistent `TuyaDiscovery` listener at integration setup and
    keeps it running for the entire HA runtime, continuously accumulating
    whatever it hears into a live cache - it does NOT open a fresh listener
    for a few seconds each time it needs an answer, the way this module's
    `discover_devices()` (an on-demand, `DISCOVERY_TIMEOUT`-second window)
    did until now. A device that broadcasts on a longer or irregular
    interval - or just doesn't happen to transmit inside whatever few-second
    window a particular poll opened - could be missed by every single
    scheduled poll indefinitely, with correct decoding but simply never
    listening at the right moment. A listener open ~100% of the time doesn't
    have that problem. `account.py`'s poller now reads from this instead of
    calling `discover_devices()` fresh each cycle.
    """

    def __init__(self, on_device: "Callable[[DiscoveredDevice], None] | None" = None) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}
        self._transports: list = []
        self._on_device = on_device

    async def start(self) -> None:
        self._transports = await _bind_all_ports(self.devices, self._on_device)

    def close(self) -> None:
        for t in self._transports:
            t.close()
        self._transports = []


async def discover_devices(timeout: float = DISCOVERY_TIMEOUT) -> dict[str, DiscoveredDevice]:
    """One-shot, short-window listen - kept as a fallback for contexts
    without a running `PersistentDiscovery` (shouldn't normally happen once
    `async_setup()` has run, see that function in __init__.py) and for
    forcing an extra immediate check on top of whatever the persistent
    listener has already accumulated."""
    results: dict[str, DiscoveredDevice] = {}
    transports = await _bind_all_ports(results)
    try:
        await asyncio.sleep(timeout)
    finally:
        for t in transports:
            t.close()
    return results
