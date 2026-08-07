"""Minimal local (LAN) Tuya protocol client.

Implements the well-documented Tuya wire format directly (packet framing +
AES payload encryption) instead of depending on a third-party Tuya SDK, so
the whole DP <-> entity path stays inspectable end to end - no black box.

Wire format (protocol versions 3.1/3.3), all fields big-endian:

    0x000055AA | seq(4) | command(4) | length(4) | payload[...] | crc32(4) | 0x0000AA55

`length` counts payload + crc32 + suffix (8 bytes). The payload itself is
AES-128-ECB encrypted with the device's local_key (PKCS7 padded). Protocol
3.4/3.5 additionally require a session-key handshake (HMAC-SHA256) before
any DP exchange.

Verified against localtuya's real implementation
(custom_components/localtuya/pytuya/__init__.py) after live-device reports
surfaced two real bugs this fixed:

- Protocol 3.3 prepends a 15-byte plaintext header (`b"3.3" + 12 zero
  bytes`) to the CIPHERTEXT of most commands (CONTROL included) - but NOT
  DP_QUERY/HEART_BEAT. This was missing entirely on the send side (would
  have broken every set_dps() control command on a real 3.3 device) and
  unhandled on the receive side (an incoming push/reply carrying it would
  fail to decrypt). Both fixed, symmetric header-prepend/strip.
- DP_QUERY's payload needs FOUR fields (gwId/devId/uid/t), not two
  (gwId/devId) - a real AC closed the connection outright on the
  incomplete request ("Connection lost").

Known limitation, honestly narrower than it first looks: **protocol 3.1's
CONTROL command uses a DIFFERENT mechanism entirely** (an MD5-hexdigest
signature prefix instead of the plain 15-byte header 3.3 uses) which is
NOT implemented here - only 3.1's DP_QUERY (status reads) has been
verified correct-in-principle against the reference; sending commands
(turning something on/off) to a 3.1 device is unverified and likely
broken until that MD5 path is added. 3.3 is the one that's been checked
against a live device end to end. Protocol 3.4/3.5's session-key handshake
is NOT implemented at all yet - most Tuya devices manufactured before
~2022 use 3.1/3.3. Attempting to use protocol_version="3.4" raises
NotImplementedError with a clear message rather than failing silently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

_LOGGER = logging.getLogger(__name__)

PREFIX = 0x000055AA
SUFFIX = 0x0000AA55
HEADER_SIZE = 16  # prefix+seq+command+length
FOOTER_SIZE = 8  # crc32+suffix

CMD_STATUS = 0x0A  # DP_QUERY
CMD_CONTROL = 0x07  # CONTROL (set DPs)
CMD_HEARTBEAT = 0x09

# How often to ping the device to keep the TCP connection (and this
# project's whole "reactive, not polling" design - see coordinator.py -
# which depends on that connection staying open to receive unsolicited
# push updates) alive. Matches localtuya's own HEARTBEAT_INTERVAL exactly.
HEARTBEAT_INTERVAL = 10

# Protocol 3.2+/3.3 prepends this 15-byte header to the CIPHERTEXT of most
# commands (CONTROL included) - but NOT DP_QUERY/HEART_BEAT. See
# _send_receive()'s comment for how this was found (diffed against
# localtuya's real pytuya implementation) and why it matters.
_VERSION_33_HEADER = b"3.3" + b"\x00" * 12


def _crc32(data: bytes) -> int:
    import binascii

    return binascii.crc32(data) & 0xFFFFFFFF


class TuyaProtocolError(Exception):
    """Raised on malformed/undecryptable packets."""


@dataclass
class TuyaMessage:
    seq: int
    command: int
    payload: dict[str, Any] | None


class TuyaLocalDevice:
    """A single persistent LAN connection to one Tuya device."""

    def __init__(
        self,
        device_id: str,
        address: str,
        local_key: str,
        protocol_version: str = "3.3",
        port: int = 6668,
        on_update: Callable[[dict[int, Any]], None] | None = None,
    ) -> None:
        if protocol_version not in ("3.1", "3.3"):
            raise NotImplementedError(
                f"Tuya protocol {protocol_version} is not implemented yet "
                "(only 3.1 and 3.3 are supported in this version)."
            )
        self.device_id = device_id
        self.address = address
        self.local_key = local_key.encode("utf-8")
        self.protocol_version = protocol_version
        self.port = port
        self._on_update = on_update
        self._seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    # -- connection lifecycle -------------------------------------------------
    async def connect(self, timeout: float = 5.0, retries: int = 3) -> None:
        # Real report: a fresh connect() to a just-discovered device (an
        # irrigation valve, found seconds earlier by active_scan.py's own
        # identify step) failed outright with ConnectionResetError. Cheap
        # embedded Tuya devices commonly have a very limited TCP stack and
        # can reject/reset a new connection attempt for a short cooldown
        # right after a previous one closed - plausible here since
        # active_scan.py connects+closes its own probe connection to the
        # same device shortly before the real pairing connect happens.
        # Retrying with a short backoff is standard, defensive handling
        # for exactly this kind of flaky embedded-device behavior, not a
        # protocol bug to "fix" - there's nothing wrong to decode/encode
        # differently here.
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.address, self.port), timeout=timeout
                )
                break
            except (ConnectionResetError, OSError, asyncio.TimeoutError) as err:
                last_err = err
                if attempt == retries:
                    raise
                delay = 0.5 * attempt
                _LOGGER.debug(
                    "%s: connect attempt %d/%d failed (%s), retrying in %.1fs",
                    self.device_id,
                    attempt,
                    retries,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
        else:  # pragma: no cover - defensive, loop always breaks or raises
            raise last_err
        self._listen_task = asyncio.ensure_future(self._listen())
        # GAP FIXED HERE (found reviewing localtuya's pytuya/__init__.py's
        # TuyaProtocol.start_heartbeat()): without a periodic HEART_BEAT,
        # a real device can silently drop this TCP connection after a
        # short idle period - the CMD_HEARTBEAT constant existed but was
        # never actually sent anywhere. Reconnecting lazily on the next
        # status()/set_dps() call still works, but every idle-then-dropped
        # gap means missing whatever unsolicited push updates would have
        # arrived on the (now-closed) connection in between - directly
        # undermining this project's "reactive, not polling" design
        # (coordinator.py), silently degrading it to poll-only whenever
        # the device's own idle-timeout is shorter than the coordinator's
        # scan_interval (30s default).
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def close(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._writer:
            self._writer.close()

    async def heartbeat(self) -> None:
        """Send one HEART_BEAT - matches the reference's 2-field payload
        (gwId/devId only, unlike DP_QUERY's 4-field one)."""
        obj = {"gwId": self.device_id, "devId": self.device_id}
        await self._send_receive(CMD_HEARTBEAT, obj)

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await self.heartbeat()
                except asyncio.TimeoutError:
                    _LOGGER.debug("%s: heartbeat timed out, closing connection", self.device_id)
                    break
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("%s: heartbeat failed (%s), closing connection", self.device_id, err)
                    break
        except asyncio.CancelledError:
            return
        if self._writer:
            self._writer.close()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # -- public API -------------------------------------------------------------
    async def status(self) -> dict[int, Any]:
        """Query current DP values (command DP_QUERY, 0x0A)."""
        # BUG FIXED HERE (found by diffing against localtuya's real
        # pytuya/__init__.py `payload_dict`): DP_QUERY's expected payload
        # for the default ("type_0a") device profile is FOUR fields -
        # gwId, devId, uid, t (timestamp) - this only ever sent two
        # (gwId, devId). A device receiving an incomplete DP_QUERY request
        # can reject/close the connection outright rather than reply -
        # surfaced as "Could not reach device on LAN: Connection lost"
        # on a live AC. `uid` follows the same convention already used
        # (correctly) in set_dps() below: falls back to device_id.
        #
        # (A prior bug here - pre-encoding the payload to bytes before
        # calling _send_receive(), which double-JSON-encoded it - was
        # fixed separately; this is a second, independent bug in what
        # fields the payload actually needs.)
        obj = {
            "gwId": self.device_id,
            "devId": self.device_id,
            "uid": self.device_id,
            "t": str(int(time.time())),
        }
        reply = await self._send_receive(CMD_STATUS, obj)
        return _extract_dps(reply)

    async def set_dps(self, dps: dict[int, Any]) -> dict[int, Any]:
        """Set one or more datapoints (command CONTROL, 0x07)."""
        # BUG FIXED HERE (same diffing pass that found the heartbeat gap):
        # the reference's CONTROL payload template is exactly
        # devId/uid/t/dps - NOT gwId/devId/uid/t/dps. This sent an extra,
        # unexpected `gwId` field on every set_dps() call - never verified
        # against a live device actually accepting a control command (the
        # DP_QUERY bug blocked pairing before any control command was ever
        # tried for real), so this may well be why. Matching the reference
        # exactly now rather than leaving an unverified extra field in.
        payload = {
            "devId": self.device_id,
            "uid": self.device_id,
            "t": str(int(time.time())),
            "dps": {str(k): v for k, v in dps.items()},
        }
        reply = await self._send_receive(CMD_CONTROL, payload)
        return _extract_dps(reply)

    # -- wire-level helpers -------------------------------------------------------
    def _build_payload(self, obj: dict[str, Any]) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def _encrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.local_key, AES.MODE_ECB)
        return cipher.encrypt(pad(data, 16))

    def _decrypt(self, data: bytes) -> bytes:
        cipher = AES.new(self.local_key, AES.MODE_ECB)
        return unpad(cipher.decrypt(data), 16)

    def _frame(self, seq: int, command: int, payload: bytes) -> bytes:
        length = len(payload) + FOOTER_SIZE
        header = struct.pack(">IIII", PREFIX, seq, command, length)
        body = header + payload
        crc = _crc32(body)
        return body + struct.pack(">II", crc, SUFFIX)

    async def _send_receive(self, command: int, obj: dict[str, Any]) -> TuyaMessage:
        if not self.connected:
            await self.connect()
        self._seq += 1
        seq = self._seq
        raw = self._build_payload(obj)
        enc = self._encrypt(raw)
        # BUG FIXED HERE (found by diffing against localtuya's real
        # pytuya/__init__.py): protocol 3.2+/3.3 requires a 15-byte
        # version header (b"3.3" + 12 zero bytes) PREPENDED TO THE
        # CIPHERTEXT for most commands - but NOT for DP_QUERY/HEART_BEAT,
        # which go out as plain ciphertext. This was missing entirely, so
        # CONTROL (set_dps) commands on a real 3.3 device were malformed -
        # the device would very likely reject/ignore them (this integration
        # never got far enough to report that specific symptom yet, since
        # the DP_QUERY payload bug below blocked pairing before any
        # set_dps() call was ever attempted against a real device).
        if self.protocol_version == "3.3" and command not in (CMD_STATUS, CMD_HEARTBEAT):
            enc = _VERSION_33_HEADER + enc
        packet = self._frame(seq, command, enc)

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[seq] = fut
        async with self._lock:
            self._writer.write(packet)
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(seq, None)

    async def _listen(self) -> None:
        """Background reader - also delivers unsolicited status pushes."""
        buf = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    msg, consumed = _try_parse(buf)
                    if msg is None:
                        break
                    buf = buf[consumed:]
                    try:
                        raw = msg.payload
                        # Symmetric with _send_receive()'s header-prepend:
                        # an incoming 3.3 payload may start with the same
                        # plaintext "3.3"+12 zero bytes header (checked
                        # BEFORE decrypting, since the header itself isn't
                        # encrypted) - strip it if present.
                        if raw[: len(_VERSION_33_HEADER)] == _VERSION_33_HEADER:
                            raw = raw[len(_VERSION_33_HEADER):]
                        payload = self._decrypt(raw) if raw else b""
                        obj = json.loads(payload) if payload else None
                    except Exception:  # noqa: BLE001 - malformed/heartbeat frame
                        obj = None
                    parsed = TuyaMessage(msg.seq, msg.command, obj)
                    fut = self._pending.get(msg.seq)
                    if fut and not fut.done():
                        fut.set_result(parsed)
                    elif obj and self._on_update:
                        dps = _extract_dps(parsed)
                        if dps:
                            self._on_update(dps)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass


@dataclass
class _RawFrame:
    seq: int
    command: int
    payload: bytes


def _try_parse(buf: bytes) -> tuple[_RawFrame | None, int]:
    """Parse ONE incoming (device -> us) frame.

    FUNDAMENTAL BUG FIXED HERE (found by diffing against localtuya's real
    `unpack_message()`): a message the DEVICE sends carries a 4-byte
    `retcode` field between the header and the encrypted payload - present
    on every real reply/push, but absent from what WE send (our own
    `_frame()`/send-side header is correctly retcode-less, matching the
    reference's send-side `MESSAGE_HEADER_FMT`). This function used the
    same 16-byte header with NO retcode skip for parsing INCOMING frames
    too, so every decrypt attempt started 4 bytes too early (into the
    retcode, not the ciphertext) and ran 4 bytes too long - silently
    caught by _listen()'s broad except and discarded as an unparseable
    frame. Net effect: DP_QUERY replies and ALL push updates were corrupt
    from the very first byte, forever - status() never raised or timed
    out (the sequence number still matched, so the waiting future still
    resolved), it just always resolved to an EMPTY dps dict. This is the
    real reason every entity showed no current value with no visible
    error, independent of (and more fundamental than) the DP_QUERY
    payload-field and coordinator-merge fixes from the same investigation.

    `length` (parsed from the header) counts retcode(4) + payload + crc/
    suffix(8) - i.e. everything after the 16-byte header.
    """
    if len(buf) < HEADER_SIZE:
        return None, 0
    prefix, seq, command, length = struct.unpack(">IIII", buf[:HEADER_SIZE])
    if prefix != PREFIX:
        raise TuyaProtocolError("bad packet prefix")
    total = HEADER_SIZE + length
    if len(buf) < total:
        return None, 0
    retcode_size = 4
    payload_len = length - retcode_size - FOOTER_SIZE
    payload_start = HEADER_SIZE + retcode_size
    payload = buf[payload_start : payload_start + payload_len]
    return _RawFrame(seq, command, payload), total


def _extract_dps(msg: TuyaMessage) -> dict[int, Any]:
    if not msg.payload:
        return {}
    dps = msg.payload.get("dps", {})
    return {int(k): v for k, v in dps.items()}
