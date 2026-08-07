"""Active LAN scan - fallback for devices passive broadcast discovery
doesn't find.

`discovery.py`'s `PersistentDiscovery` (v0.3.0) only ever finds a device
that's ACTUALLY broadcasting. Real-world gap: simple/cheap Tuya devices
(a relay-based heater, an irrigation valve) commonly only broadcast for a
short window right after boot/network join and then go quiet, relying on
the controlling app to have cached their IP and connect directly from
then on - unlike something like an AC that tends to broadcast
continuously. No amount of passive listening finds a device that has
simply stopped announcing itself.

This mirrors tinytuya's own `scanner.py` brute-force fallback:

1. Fast sweep: try opening a TCP connection to port 6668 (this
   integration's/Tuya's standard LAN control port) against every host in
   the local /24 - most hosts won't have anything listening there at all,
   so this is cheap and quickly narrows candidates down to a handful.
2. For each host that DID have the port open, try to positively identify
   it: connect for real and attempt a status() query using each
   not-yet-found cloud device's own device_id/local_key in turn. A wrong
   key/host combination fails to decrypt into valid DPS JSON (caught,
   treated as no match); a correct one succeeds - strong enough evidence
   of a real match without needing to guess in a huge key space.

Deliberately NOT run on every 5-minute poll (see account.py) - a full
subnet sweep is real network noise and takes real wall-clock time even
with concurrency, so it only runs periodically for whatever devices
passive discovery still hasn't found after a while, not as the routine
path.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

from .const import DEFAULT_PORT
from .tuya_lan import TuyaLocalDevice

_LOGGER = logging.getLogger(__name__)

TCP_PROBE_TIMEOUT = 0.3  # seconds - just checking if the port is open at all
IDENTIFY_TIMEOUT = 3.0  # seconds - a real connect + status() round trip
SWEEP_CONCURRENCY = 50


def _guess_local_ip() -> str | None:
    """Blocking - run via executor. No packets are actually sent (UDP
    connect() just picks a local route/source address), this is the
    standard cheap trick for "what's my LAN IP" without extra permissions
    or dependencies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


async def _tcp_port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except (asyncio.TimeoutError, OSError):
        # NOTE: explicitly asyncio.TimeoutError, not the bare builtin -
        # they're distinct classes before Python 3.11 (bare TimeoutError
        # alone silently missed asyncio's own timeout here during testing).
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass
    return True


async def _sweep_open_hosts(subnet: ipaddress.IPv4Network, port: int) -> list[str]:
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
    found: list[str] = []

    async def _check(ip: str) -> None:
        async with sem:
            if await _tcp_port_open(ip, port, TCP_PROBE_TIMEOUT):
                found.append(ip)

    await asyncio.gather(*(_check(str(h)) for h in subnet.hosts()))
    return found


async def _try_identify(ip: str, device_id: str, local_key: str) -> bool:
    device = TuyaLocalDevice(device_id, ip, local_key, protocol_version="3.3")
    try:
        # retries=1: this is a quick probe against a possibly-wrong host,
        # not the real pairing connection - fail fast here (connect()'s
        # own retry/backoff, added after a live report, is for the real
        # connect once a device is actually being paired, not for scanning).
        await device.connect(timeout=IDENTIFY_TIMEOUT, retries=1)
        await asyncio.wait_for(device.status(), timeout=IDENTIFY_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001 - wrong host/key/offline/anything - just not a match
        return False
    finally:
        await device.close()
        # Brief cooldown before this host might get probed again with a
        # different candidate key, or before the real pairing connect()
        # happens shortly after a match - cheap embedded devices can need
        # a moment to release a just-closed connection (see connect()'s
        # retry/backoff docstring for the live report this came from).
        await asyncio.sleep(0.3)


async def active_scan(hass, candidates: list[dict[str, Any]]) -> dict[str, str]:
    """`candidates`: list of {"device_id": ..., "local_key": ...} to look
    for. Returns {device_id: ip} for every match found."""
    loop = asyncio.get_event_loop()
    local_ip = await loop.run_in_executor(None, _guess_local_ip)
    if not local_ip:
        _LOGGER.warning("Active scan: could not determine the local IP/subnet, aborting")
        return {}

    subnet = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    _LOGGER.debug(
        "Active scan: sweeping %s (port %s) for %d not-yet-found device(s)",
        subnet,
        DEFAULT_PORT,
        len(candidates),
    )
    open_hosts = await _sweep_open_hosts(subnet, DEFAULT_PORT)
    _LOGGER.debug("Active scan: %d host(s) with port %s open: %s", len(open_hosts), DEFAULT_PORT, open_hosts)

    matches: dict[str, str] = {}
    for ip in open_hosts:
        still_needed = [c for c in candidates if c["device_id"] not in matches]
        if not still_needed:
            break
        for candidate in still_needed:
            if await _try_identify(ip, candidate["device_id"], candidate["local_key"]):
                matches[candidate["device_id"]] = ip
                _LOGGER.info("Active scan: identified device %s at %s", candidate["device_id"], ip)
                break  # this host matched one device - move on to the next host

    return matches
