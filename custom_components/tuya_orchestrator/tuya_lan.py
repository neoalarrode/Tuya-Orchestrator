"""Local (LAN) Tuya protocol client - based directly on localtuya's
`custom_components/localtuya/pytuya/__init__.py` (at the user's explicit
request, after a series of independent bugs kept surfacing from a
from-scratch reimplementation). Wire constants, framing, and the protocol
3.4 session-key handshake below are a deliberate, careful PORT of that
reference, not a re-derivation - adapted to this project's simpler single
`TuyaLocalDevice` class (the reference splits this across
`TuyaProtocol`/`MessageDispatcher`/`AESCipher`) so the rest of this
codebase (coordinator.py, active_scan.py) didn't need to change.

Wire format, all fields big-endian:

    0x000055AA | seq(4) | command(4) | length(4) | [retcode(4), receive-only]
        | payload[...] | crc32(4) or hmac-sha256(32) [3.4 only] | 0x0000AA55

Three protocol generations, real differences between them (not just a
version number):

- **3.1**: CONTROL commands get a bespoke MD5-signature-prefixed,
  base64-encoded payload instead of a plain header; DP_QUERY is plain.
- **3.3**: payload is AES-128-ECB encrypted (PKCS7 padded); most commands
  (CONTROL included) get a 15-byte plaintext version header ("3.3" + 12
  zero bytes) PREPENDED TO THE CIPHERTEXT - but NOT DP_QUERY/HEART_BEAT.
- **3.4**: requires a session-key handshake (HMAC-SHA256 nonce exchange,
  see `_negotiate_session_key`) before any real exchange; the derived
  session key replaces `local_key` for the rest of the connection. The
  version header is prepended to the PLAINTEXT (part of what gets
  encrypted, unlike 3.3), and message framing uses an HMAC-SHA256 (32
  bytes) instead of a CRC32 (4 bytes) for integrity. DP_QUERY/CONTROL are
  sent as DP_QUERY_NEW/CONTROL_NEW instead, with different payload shapes
  (CONTROL_NEW nests the DPs under `data.dps`).

Known limitation, honestly narrower than it first looks: 3.1's CONTROL
signature scheme is ported but has never been exercised against a real
3.1 device (all live reports so far have been 3.3/3.4 devices). 3.4 is a
careful, complete port of the reference's handshake and framing, verified
here with direct crypto/framing round-trip tests (mirroring exactly what
localtuya's own functions produce), but - like everything protocol-level
in this project - has not been confirmed end-to-end against a real 3.4
device from this sandbox (no live network access). Report back if a real
3.4 device still doesn't work; the discrepancy is narrowed to "this port
has a mistake" rather than "3.4 isn't attempted at all".
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire constants - names/values match localtuya's pytuya/__init__.py exactly
# ---------------------------------------------------------------------------
PREFIX = 0x000055AA
SUFFIX = 0x0000AA55
HEADER_SIZE = 16  # prefix(4)+seq(4)+command(4)+length(4)
RETCODE_SIZE = 4  # receive-only, see tuya_lan.py's earlier v0.2.7 fix
FOOTER_SIZE = 8  # crc32(4)+suffix(4) - protocol 3.1/3.3
FOOTER_SIZE_HMAC = 36  # hmac-sha256(32)+suffix(4) - protocol 3.4 only

CMD_CONTROL = 0x07
CMD_HEARTBEAT = 0x09
CMD_STATUS = 0x0A  # a.k.a. "DP_QUERY" in the reference - kept this name
# for the rest of this codebase, which predates this port
CMD_SESS_KEY_NEG_START = 0x03
CMD_SESS_KEY_NEG_RESP = 0x04
CMD_SESS_KEY_NEG_FINISH = 0x05
CMD_CONTROL_NEW = 0x0D  # protocol 3.4's CONTROL
CMD_DP_QUERY_NEW = 0x10  # protocol 3.4's DP_QUERY

# Commands that do NOT get the 3.3/3.4 plaintext version header prepended.
_NO_HEADER_CMDS = frozenset(
    {
        CMD_STATUS,
        CMD_DP_QUERY_NEW,
        CMD_HEARTBEAT,
        CMD_SESS_KEY_NEG_START,
        CMD_SESS_KEY_NEG_RESP,
        CMD_SESS_KEY_NEG_FINISH,
    }
)

_VERSION_HEADER_TAIL = b"\x00" * 12  # follows the "3.3"/"3.4" version bytes

# How often to ping the device to keep the TCP connection (and this
# project's whole "reactive, not polling" design - see coordinator.py -
# which depends on that connection staying open to receive unsolicited
# push updates) alive. Matches the reference's own HEARTBEAT_INTERVAL.
HEARTBEAT_INTERVAL = 10


def _crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


class TuyaProtocolError(Exception):
    """Raised on malformed/undecryptable packets, or a failed handshake."""


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
        if protocol_version not in ("3.1", "3.3", "3.4"):
            raise NotImplementedError(
                f"Tuya protocol {protocol_version} is not implemented "
                "(supported: 3.1, 3.3, 3.4)."
            )
        self.device_id = device_id
        self.address = address
        self.real_local_key = local_key.encode("utf-8")
        # For 3.4 this gets REPLACED by the negotiated session key once
        # connect() completes the handshake; for 3.1/3.3 it always equals
        # real_local_key. Every encrypt/decrypt uses whatever this
        # currently holds - see _cipher().
        self.local_key = self.real_local_key
        self.protocol_version = protocol_version
        self.version_bytes = protocol_version.encode("latin1")
        self.version_header = self.version_bytes + _VERSION_HEADER_TAIL
        self.port = port
        self._on_update = on_update
        self._seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        # Command-keyed (not seq-keyed) waiters - only used during the 3.4
        # handshake, where the reference itself doesn't trust seqno
        # matching yet (see _negotiate_session_key's docstring).
        self._pending_cmd: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        # Connection-lifecycle guards, ported from localtuya's
        # `TuyaDevice.async_connect()` (common.py), which refuses to start a
        # second connection with:
        #     if not self._is_closing and self._connect_task is None
        #        and not self._interface:
        # This integration now has THREE independent things that can ask for
        # a connection - initial entry setup, the RECONNECT_INTERVAL timer,
        # and the discovery-broadcast callback (all in __init__.py) - so
        # without a guard two of them can race, each opening its own socket
        # and its own listen task while only the last-assigned writer stays
        # reachable. The orphaned socket keeps consuming frames that then
        # never reach the coordinator, which looks exactly like a random
        # unexplained disconnection. The lock serializes callers and the
        # `connected` re-check makes the losers no-ops.
        self._connect_lock = asyncio.Lock()
        self._is_closing = False
        # Notified when an established connection drops on its own (failed
        # heartbeat), so the coordinator can mark entities unavailable
        # instead of leaving stale values on screen - localtuya does the
        # equivalent via its `disconnected()` callback dispatching None.
        self._on_disconnect: "Callable[[], None] | None" = None
        # 3.4 session-key negotiation state. The fixed nonce matches the
        # reference implementation exactly - security here rests on the
        # local_key's secrecy plus the HMAC exchange, not nonce randomness.
        self._local_nonce = b"0123456789abcdef"
        self._remote_nonce = b""

    # -- connection lifecycle -------------------------------------------------
    async def connect(self, timeout: float = 5.0, retries: int = 3) -> None:
        """Establish the LAN connection. Safe to call concurrently from any
        of the reconnect triggers - see `_connect_lock`'s comment. A caller
        that arrives while another is already connecting simply waits and
        then returns, having found the connection already up."""
        async with self._connect_lock:
            if self._is_closing or self.connected:
                return
            await self._connect_locked(timeout, retries)

    async def _connect_locked(self, timeout: float, retries: int) -> None:
        # Real report: a fresh connect() to a just-discovered device failed
        # outright with ConnectionResetError. Cheap embedded Tuya devices
        # commonly have a very limited TCP stack and can reject a new
        # connection for a short cooldown right after a previous one
        # closed. Retrying with a short backoff is standard, defensive
        # handling for this - not a protocol bug to "fix".
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

        self._seq = 0
        self._listen_task = asyncio.ensure_future(self._listen())

        if self.protocol_version == "3.4":
            ok = await self._negotiate_session_key()
            if not ok:
                # BUG FIXED HERE: this called self.close(), which is now
                # TERMINAL (it sets _is_closing, permanently refusing
                # further connects - correct for entry unload, wrong here).
                # A failed 3.4 handshake is a transient condition that must
                # stay retryable by the reconnect paths, so tear the socket
                # down without latching the device closed.
                self._teardown()
                raise TuyaProtocolError(f"{self.device_id}: 3.4 session key negotiation failed")

        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    def _teardown(self) -> None:
        """Drop the current socket/tasks but leave the device reconnectable."""
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._writer:
            self._writer.close()
            # BUG FIXED HERE: _writer was left pointing at the dead writer.
            # `connected` happened to still read False (a closed writer
            # reports is_closing()), but every other path had to keep
            # remembering that a non-None _writer may be garbage. Clearing
            # it makes "no writer" and "not connected" the same fact.
            self._writer = None
        self._reader = None
        self.local_key = self.real_local_key  # reset any negotiated 3.4 session key

    async def close(self) -> None:
        """Terminal close - the device will not reconnect after this.
        Called on config-entry unload; use _teardown() for a transient
        connection drop that should still be retried."""
        self._is_closing = True
        self._teardown()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def heartbeat(self) -> None:
        obj = {"gwId": self.device_id, "devId": self.device_id}
        await self._send_receive_json(CMD_HEARTBEAT, obj)

    async def _heartbeat_loop(self) -> None:
        # GAP FIXED HERE (found reviewing the reference's
        # TuyaProtocol.start_heartbeat()): without a periodic HEART_BEAT, a
        # real device can silently drop the TCP connection after a short
        # idle period - directly undermining this project's "reactive, not
        # polling" design (coordinator.py), since a dropped connection
        # misses whatever unsolicited push updates would have arrived on
        # it until the next lazy reconnect.
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
        # GAP FIXED HERE (vs localtuya's `disconnected()` callback): the old
        # code just closed the writer and said nothing. Entities kept
        # displaying their last known values as if live, indefinitely, until
        # some later poll happened to fail - so a dropped connection was
        # invisible in the UI. localtuya dispatches None to its entities at
        # exactly this point, marking them unavailable, and logs
        # "Disconnected - waiting for discovery broadcast" (which is also
        # literally what happens next here now: __init__.py reconnects on
        # the device's next broadcast, or on the RECONNECT_INTERVAL tick).
        _LOGGER.warning(
            "%s: connection lost - waiting for discovery broadcast or periodic retry",
            self.device_id,
        )
        # Detach ourselves first: _teardown() cancels _heartbeat_task, and
        # this code IS that task - cancelling the currently-running task
        # would throw CancelledError into our own remaining awaits.
        self._heartbeat_task = None
        self._teardown()
        if self._on_disconnect is not None:
            self._on_disconnect()

    # -- public API -------------------------------------------------------------
    async def status(self) -> dict[int, Any]:
        """Query current DP values."""
        if self.protocol_version == "3.4":
            # 3.4 uses DP_QUERY_NEW with a 3-field payload (no gwId) -
            # ported from the reference's "v3.4" payload_dict override.
            obj = {"devId": self.device_id, "uid": self.device_id, "t": int(time.time())}
            reply = await self._send_receive_json(CMD_DP_QUERY_NEW, obj)
        else:
            # BUG FIXED HERE (found by diffing against the reference's
            # payload_dict): the default device profile's DP_QUERY payload
            # needs FOUR fields - gwId, devId, uid, t - not two. A device
            # receiving an incomplete request can reject/close the
            # connection outright ("Connection lost" on a live AC).
            obj = {
                "gwId": self.device_id,
                "devId": self.device_id,
                "uid": self.device_id,
                "t": str(int(time.time())),
            }
            reply = await self._send_receive_json(CMD_STATUS, obj)
        return _extract_dps(reply)

    async def set_dps(self, dps: dict[int, Any]) -> dict[int, Any]:
        """Set one or more datapoints."""
        dps_str_keyed = {str(k): v for k, v in dps.items()}
        if self.protocol_version == "3.4":
            # 3.4 uses CONTROL_NEW: dps nested under "data", "t" as a real
            # int (not a string) - ported from the reference's "v3.4"
            # payload_dict override, distinct from 3.1/3.3's flat shape.
            obj: dict[str, Any] = {"protocol": 5, "t": int(time.time()), "data": {"dps": dps_str_keyed}}
            reply = await self._send_receive_json(CMD_CONTROL_NEW, obj)
        else:
            # BUG FIXED HERE (same diffing pass as the DP_QUERY fix): the
            # reference's CONTROL payload template is exactly
            # devId/uid/t/dps - NOT gwId/devId/uid/t/dps.
            obj = {
                "devId": self.device_id,
                "uid": self.device_id,
                "t": str(int(time.time())),
                "dps": dps_str_keyed,
            }
            reply = await self._send_receive_json(CMD_CONTROL, obj)
        return _extract_dps(reply)

    # -- 3.4 session-key handshake -----------------------------------------------
    async def _negotiate_session_key(self) -> bool:
        """Port of the reference's `_negotiate_session_key`. Waits for the
        SESS_KEY_NEG_RESP reply by COMMAND, not by echoed sequence number -
        matching the reference's own design (its comment: real 3.4 devices
        don't reliably echo the expected seqno for this specific exchange,
        so it deliberately doesn't rely on that here, unlike every other
        exchange)."""
        self.local_key = self.real_local_key

        try:
            reply = await self._send_receive_raw(
                CMD_SESS_KEY_NEG_START, self._local_nonce, wait_cmd=CMD_SESS_KEY_NEG_RESP
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("%s: 3.4 session key negotiation step 1 timed out", self.device_id)
            return False

        if not reply.payload or len(reply.payload) < 48:
            _LOGGER.debug("%s: 3.4 session key negotiation step 2 failed (short/no response)", self.device_id)
            return False

        try:
            decrypted = self._decrypt_raw(reply.payload)
        except (ValueError, KeyError) as err:
            _LOGGER.debug("%s: 3.4 session key negotiation step 2 decrypt failed: %s", self.device_id, err)
            return False

        if len(decrypted) < 48:
            _LOGGER.debug("%s: 3.4 session key negotiation step 2 response too short", self.device_id)
            return False

        self._remote_nonce = decrypted[:16]
        hmac_check = hmac.new(self.local_key, self._local_nonce, hashlib.sha256).digest()
        if hmac_check != decrypted[16:48]:
            # Non-fatal in the reference too (logged, not aborted) - the
            # HMAC-FINISH step below is the real integrity confirmation.
            _LOGGER.debug("%s: 3.4 session key negotiation HMAC check mismatch", self.device_id)

        rkey_hmac = hmac.new(self.local_key, self._remote_nonce, hashlib.sha256).digest()
        # FINISH is fire-and-forget in the reference (recv_retries=None ->
        # its wait loop never actually runs) - no reply expected/awaited.
        await self._send_only(CMD_SESS_KEY_NEG_FINISH, rkey_hmac)

        xored = bytes(a ^ b for a, b in zip(self._local_nonce, self._remote_nonce))
        # NOT padded (pad=False in the reference) - the XOR result is
        # already exactly 16 bytes, one AES block.
        self.local_key = self._encrypt_raw(xored, pad_data=False)
        _LOGGER.debug("%s: 3.4 session key negotiated successfully", self.device_id)
        return True

    # -- wire-level helpers -------------------------------------------------------
    def _cipher(self) -> AES:
        return AES.new(self.local_key, AES.MODE_ECB)

    def _encrypt_raw(self, data: bytes, pad_data: bool = True) -> bytes:
        cipher = self._cipher()
        return cipher.encrypt(pad(data, 16) if pad_data else data)

    def _decrypt_raw(self, data: bytes) -> bytes:
        cipher = self._cipher()
        return unpad(cipher.decrypt(data), 16)

    def _build_payload(self, obj: dict[str, Any]) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def _encode_message(self, command: int, raw_payload: bytes) -> tuple[bytes, int, bytes | None]:
        """Version-aware framing, ported from the reference's
        `_encode_message`. Returns (wire_bytes, seq, hmac_key_used)."""
        hmac_key: bytes | None = None
        payload = raw_payload

        if self.protocol_version == "3.4":
            hmac_key = self.local_key
            if command not in _NO_HEADER_CMDS:
                # 3.4: header goes into the PLAINTEXT (encrypted together
                # with the payload) - different from 3.3, where the header
                # is prepended to the already-encrypted ciphertext.
                payload = self.version_header + payload
            payload = self._encrypt_raw(payload, pad_data=True)
        elif self.protocol_version == "3.3":
            # BUG FIXED HERE (found by diffing against the reference):
            # this header was missing entirely on both send and receive
            # for a long time - every set_dps() control command would
            # have been malformed on a real 3.3 device.
            payload = self._encrypt_raw(payload, pad_data=True)
            if command not in _NO_HEADER_CMDS:
                payload = self.version_header + payload
        else:  # 3.1
            if command == CMD_CONTROL:
                enc = self._encrypt_raw(payload, pad_data=True)
                enc_b64 = base64.b64encode(enc)
                pre_md5 = b"data=" + enc_b64 + b"||lpv=3.1||" + self.real_local_key
                digest = hashlib.md5(pre_md5).hexdigest()
                payload = b"3.1" + digest[8:24].encode("latin1") + enc_b64
            else:
                payload = self._encrypt_raw(payload, pad_data=True)

        self._seq += 1
        seq = self._seq
        return self._pack(seq, command, payload, hmac_key), seq, hmac_key

    def _pack(self, seq: int, command: int, payload: bytes, hmac_key: bytes | None) -> bytes:
        footer_len = FOOTER_SIZE_HMAC if hmac_key else FOOTER_SIZE
        header = struct.pack(">IIII", PREFIX, seq, command, len(payload) + footer_len)
        body = header + payload
        if hmac_key:
            mac = hmac.new(hmac_key, body, hashlib.sha256).digest()
            return body + struct.pack(">32sI", mac, SUFFIX)
        crc = _crc32(body)
        return body + struct.pack(">II", crc, SUFFIX)

    async def _send_only(self, command: int, raw_payload: bytes) -> None:
        packet, _seq, _hmac_key = self._encode_message(command, raw_payload)
        async with self._lock:
            self._writer.write(packet)
            await self._writer.drain()

    async def _send_receive_json(self, command: int, obj: dict[str, Any]) -> TuyaMessage:
        return await self._send_receive_raw(command, self._build_payload(obj))

    async def _send_receive_raw(
        self, command: int, raw_payload: bytes, wait_cmd: int | None = None
    ) -> TuyaMessage:
        if not self.connected:
            await self.connect()
        packet, seq, _hmac_key = self._encode_message(command, raw_payload)

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        if wait_cmd is not None:
            self._pending_cmd[wait_cmd] = fut
        else:
            self._pending[seq] = fut

        async with self._lock:
            self._writer.write(packet)
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(seq, None)
            if wait_cmd is not None:
                self._pending_cmd.pop(wait_cmd, None)

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
                    frame, consumed = _try_parse(buf, hmac_framed=self.protocol_version == "3.4")
                    if frame is None:
                        break
                    buf = buf[consumed:]
                    obj = self._decode_frame_payload(frame.payload)
                    parsed = TuyaMessage(frame.seq, frame.command, obj)

                    # Command-sentinel waiters (3.4 handshake) take
                    # priority - matches the reference's own dispatch order
                    # for SESS_KEY_NEG_RESP.
                    cmd_fut = self._pending_cmd.get(frame.command)
                    if cmd_fut and not cmd_fut.done():
                        cmd_fut.set_result(TuyaMessage(frame.seq, frame.command, frame.payload))
                        continue

                    fut = self._pending.get(frame.seq)
                    if fut and not fut.done():
                        fut.set_result(parsed)
                    elif obj and self._on_update:
                        dps = _extract_dps(parsed)
                        if dps:
                            self._on_update(dps)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass

    def _decode_frame_payload(self, raw: bytes) -> dict | None:
        """Version-aware payload decode, ported from the reference's
        `_decode_payload`. For 3.4 the whole payload (header included) is
        encrypted together - decrypt FIRST, then strip the now-plaintext
        version header if present. For 3.1/3.3 the header (if any) is
        OUTSIDE the encryption - strip first, then decrypt."""
        if not raw:
            return None
        try:
            if self.protocol_version == "3.4":
                decrypted = self._decrypt_raw(raw)
                if decrypted.startswith(self.version_bytes):
                    decrypted = decrypted[len(self.version_header) :]
                text = decrypted.decode("utf-8")
            elif self.protocol_version == "3.1" and raw.startswith(b"3.1"):
                # skip "3.1" (3 bytes) + 16-byte MD5-hexdigest signature
                text = self._decrypt_raw(raw[19:]).decode("utf-8")
            else:  # 3.3 (or 3.1 non-CONTROL replies, same shape as 3.3)
                payload = raw
                if payload[: len(self.version_header)] == self.version_header:
                    payload = payload[len(self.version_header) :]
                if payload[:1] == b"{" and payload[-1:] == b"}":
                    text = payload.decode("utf-8")  # already-plaintext ack/edge case
                else:
                    text = self._decrypt_raw(payload).decode("utf-8")
        except Exception:  # noqa: BLE001 - malformed/heartbeat-ack/undecodable frame
            return None

        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
        # 3.4 CONTROL_NEW replies nest dps under "data" - unwrap so
        # _extract_dps doesn't need to know which protocol generation sent it.
        if isinstance(obj, dict) and "dps" not in obj and isinstance(obj.get("data"), dict):
            if "dps" in obj["data"]:
                obj["dps"] = obj["data"]["dps"]
        return obj


@dataclass
class _RawFrame:
    seq: int
    command: int
    payload: bytes


def _try_parse(buf: bytes, hmac_framed: bool) -> tuple[_RawFrame | None, int]:
    """Parse ONE incoming (device -> us) frame. `hmac_framed` selects the
    36-byte HMAC-SHA256 footer (protocol 3.4) vs the 8-byte CRC32 footer
    (3.1/3.3) - the two are NOT distinguishable from the header alone, the
    caller must know which protocol this connection is using.

    `length` (parsed from the header) counts retcode(4) + payload +
    footer - i.e. everything after the 16-byte header. The retcode field
    is present only on frames the DEVICE sends (see the v0.2.7 fix this
    generalizes) - our own outgoing frames never include one.
    """
    if len(buf) < HEADER_SIZE:
        return None, 0
    prefix, seq, command, length = struct.unpack(">IIII", buf[:HEADER_SIZE])
    if prefix != PREFIX:
        raise TuyaProtocolError("bad packet prefix")
    total = HEADER_SIZE + length
    if len(buf) < total:
        return None, 0
    footer_size = FOOTER_SIZE_HMAC if hmac_framed else FOOTER_SIZE
    payload_len = length - RETCODE_SIZE - footer_size
    payload_start = HEADER_SIZE + RETCODE_SIZE
    payload = buf[payload_start : payload_start + max(payload_len, 0)]
    return _RawFrame(seq, command, payload), total


def _extract_dps(msg: TuyaMessage) -> dict[int, Any]:
    if not msg.payload:
        return {}
    dps = msg.payload.get("dps")
    if not dps:
        return {}
    return {int(k): v for k, v in dps.items()}
