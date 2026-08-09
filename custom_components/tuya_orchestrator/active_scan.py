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

from .const import DEFAULT_PORT, DOMAIN
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


# Tried in this order for every candidate: 3.3 is still the most common
# generation in the wild, but plenty of newer devices (RGBCW bulbs among
# them, per a live report) are 3.4-only - guessing a single fixed version
# here would silently mismatch those and either fail to identify them at
# all, or - worse - "succeed" while actually talking the wrong protocol
# (which is exactly what produced an "always unknown" symptom before
# tuya_lan.py's 3.4 support was ported in).
#
# BUG FIXED HERE (found while probing a real account from the LAN): 3.1
# was missing from this tuple entirely, so a 3.1 device could never be
# identified by an active scan no matter how reachable it was. That is
# not a hypothetical generation - this account's older, short-device-id
# equipment (two air conditioners, a heater, a power strip) is exactly
# that vintage, and those are also the devices least likely to broadcast,
# i.e. the ones that depend on active scanning in the first place. 3.3 is
# still tried first as the most common generation.
_PROBE_VERSIONS = ("3.3", "3.4", "3.1")


async def _try_identify(ip: str, device_id: str, local_key: str) -> str | None:
    """Returns the protocol version that successfully identified the
    device, or None if no version worked against this host.

    CRITICAL BUG FIXED HERE: a wrong local_key/host/version combination
    does NOT reliably raise an exception. tuya_lan.py's status() swallows
    any undecryptable reply into an empty {} return (by design, for
    normal operation - a single garbled push shouldn't crash a running
    device), so the old check here ("did status() raise?") was true for
    ANY host that merely replied to a query at all - meaning literally any
    Tuya device open on port 6668 on the LAN could get "identified" as a
    match for whichever candidate device_id happened to be tried against
    it next, regardless of whether the key was actually right. Confirmed
    from a live report: phantom devices kept getting offered, and the
    real device's own IP got silently stolen by a wrong match, leaving it
    unable to connect. Fixed to require ACTUAL non-empty DPS data back -
    a real, meaningful identification, not just "no exception happened".
    """
    for version in _PROBE_VERSIONS:
        device = TuyaLocalDevice(device_id, ip, local_key, protocol_version=version)
        try:
            # retries=1: this is a quick probe against a possibly-wrong
            # host, not the real pairing connection - fail fast here
            # (connect()'s own retry/backoff, added after a live report,
            # is for the real connect once a device is actually being
            # paired, not for scanning).
            await device.connect(timeout=IDENTIFY_TIMEOUT, retries=1)
            dps = await asyncio.wait_for(device.status(), timeout=IDENTIFY_TIMEOUT)
            if dps:  # non-empty dict required - see the bug note above
                return version
        except Exception:  # noqa: BLE001 - wrong host/key/version/offline - just not a match yet
            continue
        finally:
            await device.close()
            # Brief cooldown before this host might get probed again (a
            # different protocol version, a different candidate key, or
            # the real pairing connect() shortly after a match) - cheap
            # embedded devices can need a moment to release a just-closed
            # connection (see connect()'s retry/backoff docstring).
            await asyncio.sleep(0.3)
    return None


async def active_scan(hass, candidates: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """`candidates`: list of {"device_id": ..., "local_key": ...} to look
    for. Returns {device_id: (ip, protocol_version)} for every match found -
    the version comes from whichever probe in `_PROBE_VERSIONS` actually
    worked, never assumed."""
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

    # MEASURED ON REAL HARDWARE: a Tuya device serves exactly ONE LAN
    # session at a time. A second client's TCP connect is ACCEPTED but the
    # device then never answers it - verified directly against a live
    # device here (connection A kept working and answering; a concurrent
    # connection B connected and then timed out on every query, while A
    # was unaffected). So probing a host whose session this integration
    # already holds can only ever time out - it is guaranteed dead time,
    # now multiplied by every protocol version in _PROBE_VERSIONS and
    # every candidate key. Skip those hosts outright: a device we are
    # already connected to is by definition not one we are looking for.
    configured_addresses = {
        entry.data["address"]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get("address")
    }
    if configured_addresses:
        skipped = [ip for ip in open_hosts if ip in configured_addresses]
        if skipped:
            _LOGGER.debug(
                "Active scan: skipping %d host(s) already configured as devices: %s",
                len(skipped),
                skipped,
            )
        open_hosts = [ip for ip in open_hosts if ip not in configured_addresses]

    matches: dict[str, tuple[str, str]] = {}
    for ip in open_hosts:
        still_needed = [c for c in candidates if c["device_id"] not in matches]
        if not still_needed:
            break
        for candidate in still_needed:
            version = await _try_identify(ip, candidate["device_id"], candidate["local_key"])
            if version is not None:
                matches[candidate["device_id"]] = (ip, version)
                _LOGGER.info(
                    "Active scan: identified device %s at %s (protocol %s)",
                    candidate["device_id"],
                    ip,
                    version,
                )
                break  # this host matched one device - move on to the next host

    return matches
