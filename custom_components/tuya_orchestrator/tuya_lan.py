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
from collections import deque
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
PREFIX_BYTES = b"\x00\x00\x55\xaa"  # used to resynchronize a desynced stream
HEADER_SIZE = 16  # prefix(4)+seq(4)+command(4)+length(4)
RETCODE_SIZE = 4  # receive-only, see tuya_lan.py's earlier v0.2.7 fix
FOOTER_SIZE = 8  # crc32(4)+suffix(4) - protocol 3.1/3.3
FOOTER_SIZE_HMAC = 36  # hmac-sha256(32)+suffix(4) - protocol 3.4 only
# Same sanity bound the reference's parse_header() uses - real Tuya packets
# top out somewhere around 300 bytes, so anything past this is corruption
# or a desynced stream, not a big legitimate frame.
MAX_PAYLOAD_LEN = 1000

CMD_CONTROL = 0x07
# The device's SPONTANEOUS state report. Distinct from CMD_STATUS (0x0a),
# which is what WE send to ask for state. Confirmed on real hardware: a
# watering pump emits one of these roughly every second while running.
CMD_STATUS_REPORT = 0x08
CMD_HEARTBEAT = 0x09
CMD_STATUS = 0x0A  # a.k.a. "DP_QUERY" in the reference - kept this name
# for the rest of this codebase, which predates this port
CMD_SESS_KEY_NEG_START = 0x03
CMD_SESS_KEY_NEG_RESP = 0x04
CMD_SESS_KEY_NEG_FINISH = 0x05
CMD_CONTROL_NEW = 0x0D  # protocol 3.4's CONTROL, and type_0d's DP_QUERY
CMD_DP_QUERY_NEW = 0x10  # protocol 3.4's DP_QUERY
CMD_UPDATEDPS = 0x12  # ask the device to refresh/re-report the given DPs

# Commands that do NOT get the 3.3/3.4 plaintext version header prepended.
_NO_HEADER_CMDS = frozenset(
    {
        CMD_STATUS,
        CMD_DP_QUERY_NEW,
        CMD_UPDATEDPS,
        CMD_HEARTBEAT,
        CMD_SESS_KEY_NEG_START,
        CMD_SESS_KEY_NEG_RESP,
        CMD_SESS_KEY_NEG_FINISH,
    }
)

# "Device type", exactly as the reference names it. Not a product
# category - it selects which DP_QUERY dialect the device speaks:
#
# - type_0a (default): answers a normal DP_QUERY (0x0A) with all its DPs.
# - type_0d: rejects DP_QUERY with a "data unvalid" payload and instead
#   requires CONTROL_NEW (0x0D) carrying an EXPLICIT list of the DPs being
#   asked for (`{"dps": {"1": null, "2": null, ...}}`). Such a device
#   simply never reports state under the type_0a path - which looks
#   exactly like a device that pairs fine but whose entities stay empty
#   forever. The reference detects this at runtime from the error payload
#   and transparently re-sends; ported here in `_decode_frame_payload`
#   (detection) and `status()` (the one-shot retry).
DEV_TYPE_0A = "type_0a"
DEV_TYPE_0D = "type_0d"

# DPs the reference considers safe to refresh via UPDATEDPS (0x12).
UPDATE_DPS_WHITELIST = (18, 19, 20)

_VERSION_HEADER_TAIL = b"\x00" * 12  # follows the "3.3"/"3.4" version bytes

# How often to ping the device to keep the TCP connection (and this
# project's whole "reactive, not polling" design - see coordinator.py -
# which depends on that connection staying open to receive unsolicited
# push updates) alive. Matches the reference's own HEARTBEAT_INTERVAL.
SEND_TIMEOUT = 10  # seconds to wait for a device reply before giving up
# How many recent frames each device keeps for the diagnostics platform
# (see diagnostics.py). Small and bounded - this is a rolling window meant
# to answer "what actually went over the wire just before it broke?",
# which is the one question a remote log at WARNING level cannot answer.
TRACE_SIZE = 60
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
        if protocol_version not in ("3.1", "3.2", "3.3", "3.4"):
            raise NotImplementedError(
                f"Tuya protocol {protocol_version} is not implemented "
                "(supported: 3.1, 3.2, 3.3, 3.4)."
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
        # Consecutive-failure backoff. GAP FIXED HERE, found on a live
        # instance: a battery-powered outdoor watering valve went 14+
        # minutes with EVERY reconnect attempt failing at the 3.4 handshake
        # (SESS_KEY_NEG_START sent, zero replies, ever) while ICMP ping to
        # the same host showed 0% loss - consistent with a weak/marginal
        # WiFi link that can lose a larger handshake reply while still
        # answering tiny ICMP echoes, or a device declining to renegotiate
        # too soon after a prior attempt. Either way, __init__.py has THREE
        # independent reconnect triggers (initial setup, the periodic
        # timer, every broadcast heard while disconnected) and none of them
        # backed off - a struggling device got hammered with a fresh
        # handshake attempt roughly every 10-15s, forever, with no
        # increasing gap to give a marginal link (or the device itself) any
        # room to recover. `next_retry_at()` below is consulted by both
        # reconnect paths before attempting.
        self._consecutive_failures = 0
        self._last_attempt_at = 0.0
        # See DEV_TYPE_0A/DEV_TYPE_0D. Starts optimistic (type_0a) and is
        # switched at runtime the first time the device answers a DP_QUERY
        # with "data unvalid", exactly as the reference does - there is no
        # way to know in advance, and no cloud metadata field for it.
        # GAP FIXED HERE: protocol 3.2 was rejected outright by the
        # constructor above, so a 3.2 device could not be used at all. The
        # reference supports it in `set_version()`: 3.2 frames exactly like
        # 3.3 but starts in the type_0d dialect rather than type_0a
        # ("3.2 behaves like 3.3 with type_0d" - its own comment).
        self.dev_type = DEV_TYPE_0D if protocol_version == "3.2" else DEV_TYPE_0A
        # {"1": None, "2": None, ...} - the explicit DP list a type_0d
        # device requires in its query. Populated via add_dps_to_request()
        # from the device's profile (the reference fills it from the
        # configured entity list, same idea).
        self.dps_to_request: dict[str, Any] = {}
        self._trace: deque = deque(maxlen=TRACE_SIZE)
        # 3.4 session-key negotiation state. The fixed nonce matches the
        # reference implementation exactly - security here rests on the
        # local_key's secrecy plus the HMAC exchange, not nonce randomness.
        self._local_nonce = b"0123456789abcdef"
        self._remote_nonce = b""

    def seconds_until_retry(self) -> float:
        """How much longer to wait before the next reconnect ATTEMPT should
        be made, given recent consecutive failures. 0 means "go ahead now".

        The first couple of failures get no delay at all - a device that
        just rebooted or had one bad packet should reconnect as fast as the
        existing triggers allow, which is the whole point of reacting to
        every broadcast. Backoff only kicks in once failures are clearly a
        pattern, not a blip, and is capped well under any single caller's
        own patience (RECONNECT_INTERVAL is 60s) so a struggling device is
        still retried, just not hammered.
        """
        if self._consecutive_failures < 3:
            return 0.0
        backoff = min(30.0 * (2 ** (self._consecutive_failures - 3)), 600.0)
        remaining = self._last_attempt_at + backoff - time.time()
        return max(0.0, remaining)

    # -- connection lifecycle -------------------------------------------------
    async def connect(self, timeout: float = 5.0, retries: int = 3) -> None:
        """Establish the LAN connection. Safe to call concurrently from any
        of the reconnect triggers - see `_connect_lock`'s comment. A caller
        that arrives while another is already connecting simply waits and
        then returns, having found the connection already up."""
        async with self._connect_lock:
            if self._is_closing or self.connected:
                return
            self._last_attempt_at = time.time()
            try:
                await self._connect_locked(timeout, retries)
            except Exception:
                self._consecutive_failures += 1
                raise
            else:
                self._consecutive_failures = 0

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
        # Matches the reference's `dispatcher.abort()` on close: release
        # everyone still waiting on a reply that can no longer arrive,
        # instead of leaving each to burn its full 10s timeout on a socket
        # already known to be gone.
        #
        # BUG FIXED HERE, and a real one: this used `fut.cancel()`, which
        # is semantically wrong for "the connection died" - cancellation
        # means the WAITER'S OWN caller asked to stop, not that the
        # answer will never come. `asyncio.CancelledError` inherits from
        # BaseException specifically so ordinary `except Exception`
        # handling never swallows it; a caller mid-3.4-handshake (the
        # common victim, since v0.13.1 made the read side notice a drop
        # immediately instead of dying silently) saw that CancelledError
        # escape uncaught past its own error handling, all the way out of
        # connect() as an opaque, unactionable exception - reproduced
        # live: a device that accepts the TCP connection, receives
        # SESS_KEY_NEG_START, then drops the connection (EOF) before
        # replying - plausibly the same weak/marginal link flagged in
        # v0.13.0 - turned into a bare CancelledError on every single
        # attempt, delete-and-re-add included, because THIS teardown path
        # produced it fresh every time, regardless of who was asking or
        # how long they were willing to wait. A real connection failure
        # instead of a cancellation.
        conn_lost = TuyaProtocolError("connection lost while awaiting a reply")
        for fut in (*self._pending.values(), *self._pending_cmd.values()):
            if not fut.done():
                fut.set_exception(conn_lost)
        self._pending.clear()
        self._pending_cmd.clear()
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
        # BUG FIXED HERE: this waited on the echoed SEQUENCE NUMBER like
        # every other command. The reference has an explicit hack for
        # exactly this case, with the reason in a comment:
        #     "Heartbeats on protocols < 3.3 respond with sequence number 0,
        #      so they can't be waited for like other messages."
        # (its HEARTBEAT_SEQNO sentinel, dispatched by COMMAND instead).
        # Without it, on a 3.1 device every heartbeat waited on a seqno the
        # device never echoes, timed out after 10s, and _heartbeat_loop
        # read that timeout as a dead connection and tore down a perfectly
        # healthy socket - every 10 seconds, forever. Wait by command, which
        # is correct for all versions, not just the broken one.
        await self._send_receive_raw(
            CMD_HEARTBEAT, self._build_payload(obj), wait_cmd=CMD_HEARTBEAT
        )

    async def _heartbeat_loop(self) -> None:
        reason = "unknown"
        # GAP FIXED HERE (found reviewing the reference's
        # TuyaProtocol.start_heartbeat()): without a periodic HEART_BEAT, a
        # real device can silently drop the TCP connection after a short
        # idle period - directly undermining this project's "reactive, not
        # polling" design (coordinator.py), since a dropped connection
        # misses whatever unsolicited push updates would have arrived on
        # it until the next lazy reconnect.
        try:
            while True:
                # Send first, THEN sleep - the reference's heartbeat_loop
                # does it in this order, so the connection is proven alive
                # immediately rather than after a full interval of silence.
                try:
                    await self.heartbeat()
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    reason = f"no heartbeat reply within {SEND_TIMEOUT}s"
                    break
                except Exception as err:  # noqa: BLE001
                    reason = f"{type(err).__name__}: {err}"
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
        # Detach ourselves first: _teardown() cancels _heartbeat_task, and
        # this code IS that task - cancelling the currently-running task
        # would throw CancelledError into our own remaining awaits.
        self._heartbeat_task = None
        # Idempotency guard: `_listen()`'s read side can independently
        # detect the same drop (see `_handle_connection_lost`'s docstring)
        # and may have already torn the connection down by the time this
        # runs - `_writer` is None once that's happened. Without this check
        # a near-simultaneous read+write failure would log the "connection
        # lost" warning and call `_on_disconnect` twice for one drop.
        if self._writer is not None:
            self._handle_connection_lost(reason)

    def _handle_connection_lost(self, reason: str) -> None:
        # GAP FIXED HERE, and a real one: this used to be inline in
        # _heartbeat_loop only. `_listen()`'s own read loop independently
        # catches ConnectionResetError/OSError from `_reader.read()` and,
        # until now, just returned - silently. The read task would be gone,
        # `connected` would still report True (the writer wasn't touched),
        # every pending reply would sit until its own SEND_TIMEOUT expired
        # one at a time, and NOTHING was logged or reported to the
        # coordinator until the next outgoing heartbeat's write/drain also
        # happened to fail - up to a full HEARTBEAT_INTERVAL (10s) later,
        # sometimes more. A real report of a device resetting the
        # connection on its own (`ConnectionResetError: Connection lost`)
        # traced back to exactly this: the WRITE side eventually noticed
        # and handled it correctly, but the READ side had already died
        # silently, doing nothing, for however long that gap was. Both
        # sides now report through this single path immediately.
        _LOGGER.warning(
            "%s: connection lost (%s) - waiting for discovery broadcast or periodic retry",
            self.device_id,
            reason,
        )
        self._teardown()
        if self._on_disconnect is not None:
            self._on_disconnect()

    def _trace_add(self, direction: str, seq: int, command: int, raw: bytes, note: str = "") -> None:
        """Record one frame for diagnostics.py. Only the first 48 bytes of
        the wire frame are kept: enough to see the header, retcode and the
        start of the payload, while making it impossible for a whole
        encrypted DP payload to end up in a diagnostics download."""
        self._trace.append(
            {
                "t": round(time.time(), 3),
                "dir": direction,
                "seq": seq,
                "cmd": f"0x{command:02x}",
                "bytes": len(raw),
                "head": binascii.hexlify(raw[:48]).decode(),
                "note": note,
            }
        )

    def trace(self) -> list[dict[str, Any]]:
        """Snapshot of the recent frame history, oldest first."""
        return list(self._trace)

    # -- public API -------------------------------------------------------------
    def add_dps_to_request(self, dp_ids) -> None:
        """Register which DPs to ask for explicitly. Only type_0d devices
        actually need this, but it is harmless to populate always - and it
        must be populated BEFORE the first status() in case this device
        turns out to be type_0d (the reference wires it the same way, from
        the configured entity list, at device-construction time)."""
        if isinstance(dp_ids, int):
            self.dps_to_request[str(dp_ids)] = None
        else:
            self.dps_to_request.update({str(i): None for i in dp_ids})

    async def status(self) -> dict[int, Any]:
        """Query current DP values, transparently handling a device that
        turns out to speak the type_0d dialect (see DEV_TYPE_0D)."""
        before = self.dev_type
        dps = await self._status_once()
        if self.dev_type != before:
            # _decode_frame_payload just detected "data unvalid" and
            # switched us to type_0d. The reference re-sends the same
            # command exactly once on a dev_type change; do the same, now
            # that _status_once() will use the CONTROL_NEW dialect.
            _LOGGER.debug(
                "%s: device type changed %s -> %s, re-sending status query",
                self.device_id,
                before,
                self.dev_type,
            )
            dps = await self._status_once()
        return dps

    async def _status_once(self) -> dict[int, Any]:
        if self.dev_type == DEV_TYPE_0D and self.protocol_version != "3.4":
            # type_0d: DP_QUERY is overridden to CONTROL_NEW and must carry
            # the explicit DP list. `dps_to_request` deliberately goes out
            # even if empty - matches the reference, and an empty list is
            # still a valid (if useless) query rather than a crash.
            obj = {
                "devId": self.device_id,
                "uid": self.device_id,
                "t": str(int(time.time())),
                "dps": self.dps_to_request,
            }
            reply = await self._send_receive_json(CMD_CONTROL_NEW, obj)
            return _extract_dps(reply)
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

        # GAP FIXED HERE: the reference adopts the device's sequence number
        # from this very message, with the reason in its own comment:
        # "for 3.4 devices, we get the starting seqno with the
        # SESS_KEY_NEG_RESP message" (`self.seqno = msg.seqno`). A 3.4
        # device numbers the session from ITS side, so continuing with our
        # own counter left every subsequent reply carrying a seqno we were
        # not waiting on - i.e. every command on a 3.4 device timing out,
        # which is exactly the "unknown state on all values" symptom seen
        # on the 3.4 bulbs. (`- 1` because our counter pre-increments where
        # the reference's post-increments; next send lands on reply.seq
        # either way.)
        if reply.seq > 0:
            self._seq = reply.seq - 1

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
        elif self.protocol_version in ("3.2", "3.3"):
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
            # else: NO encryption at all. BUG FIXED HERE - this branch used
            # to encrypt like 3.3 does. In the reference's _encode_message
            # the version chain is `if 3.4 / elif >= 3.2 / elif cmd ==
            # CONTROL` with NO trailing else, so on 3.1 every command that
            # is not CONTROL (DP_QUERY, HEART_BEAT...) goes out as PLAIN
            # JSON. Sending those encrypted means a 3.1 device receives a
            # query it cannot parse and simply never answers - which is not
            # a theory: probing this account's real 3.1-era devices from
            # the LAN with correct keys timed out on every single one,
            # while the 3.3/3.4 devices answered immediately.

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
        self._trace_add("tx", _seq, command, packet, "no reply expected")
        async with self._lock:
            self._writer.write(packet)
            await self._writer.drain()

    async def _send_receive_json(self, command: int, obj: dict[str, Any]) -> TuyaMessage:
        # For CONTROL/CONTROL_NEW, which DPs are being WRITTEN is exactly
        # the question when something is writing far more often than any
        # user plausibly clicked - preview it (plaintext, our own outgoing
        # data, no secrets in it) rather than only being able to see that a
        # write happened.
        preview = ""
        if command in (CMD_CONTROL, CMD_CONTROL_NEW):
            dps = obj.get("dps") or obj.get("data", {}).get("dps")
            if dps:
                preview = f" dps={dps}"
        return await self._send_receive_raw(command, self._build_payload(obj), extra_note=preview)

    async def _send_receive_raw(
        self, command: int, raw_payload: bytes, wait_cmd: int | None = None, extra_note: str = ""
    ) -> TuyaMessage:
        if not self.connected:
            await self.connect()
        packet, seq, _hmac_key = self._encode_message(command, raw_payload)
        self._trace_add(
            "tx", seq, command, packet,
            f"awaiting {'cmd 0x%02x' % wait_cmd if wait_cmd is not None else 'seq %d' % seq}{extra_note}",
        )

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        if wait_cmd is not None:
            self._pending_cmd[wait_cmd] = fut
        else:
            # Belt and braces after the rewind bug above: never silently
            # displace an in-flight waiter. The reference is loud about
            # this too (`wait_for`: `if seqno in self.listeners: raise`),
            # and silently overwriting is exactly what turned that bug
            # into unexplained 10s timeouts instead of a visible error.
            existing = self._pending.get(seq)
            if existing is not None and not existing.done():
                _LOGGER.warning(
                    "%s: sequence number %d is already awaiting a reply - "
                    "not displacing it (this indicates a counter desync)",
                    self.device_id,
                    seq,
                )
                existing.cancel()
            self._pending[seq] = fut

        async with self._lock:
            self._writer.write(packet)
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=SEND_TIMEOUT)
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
                    # BUG FIXED HERE: _try_parse() raises TuyaProtocolError
                    # on a bad prefix (and now on a corrupt length too), and
                    # that exception is NOT one of the types caught below -
                    # so a single malformed or desynced byte run killed this
                    # task outright, silently (an unretrieved task
                    # exception). The socket stayed open and `connected`
                    # stayed True, but nothing was ever read again: every
                    # command timed out and no push update arrived, with no
                    # error anywhere pointing at the cause. Resynchronize on
                    # the next frame boundary instead of dying.
                    try:
                        frame, consumed = _try_parse(
                            buf, hmac_framed=self.protocol_version == "3.4"
                        )
                    except TuyaProtocolError as err:
                        self._trace_add("rx", -1, 0, buf[:48], f"UNPARSEABLE: {err}")
                        nxt = buf.find(PREFIX_BYTES, 1)
                        _LOGGER.debug(
                            "%s: %s - %s",
                            self.device_id,
                            err,
                            "resyncing to next frame" if nxt > 0 else "dropping buffer",
                        )
                        buf = buf[nxt:] if nxt > 0 else b""
                        continue
                    if frame is None:
                        break
                    buf = buf[consumed:]
                    obj = self._decode_frame_payload(frame.payload)
                    parsed = TuyaMessage(frame.seq, frame.command, obj)
                    # Which waiter (if any) this frame lands on is the whole
                    # question when a command times out, so record it.
                    if self._pending_cmd.get(frame.command) is not None:
                        route = "-> cmd waiter"
                    elif self._pending.get(frame.seq) is not None:
                        route = "-> seq waiter"
                    elif obj:
                        route = "-> unsolicited push"
                    else:
                        route = "-> DROPPED (no waiter, undecodable)"
                    self._trace_add(
                        "rx", frame.seq, frame.command, frame.payload,
                        f"{route}; decoded={'yes' if obj else 'no'}",
                    )

                    # Command-sentinel waiters (3.4 handshake) take
                    # priority - matches the reference's own dispatch order
                    # for SESS_KEY_NEG_RESP.
                    cmd_fut = self._pending_cmd.get(frame.command)
                    if cmd_fut and not cmd_fut.done():
                        cmd_fut.set_result(TuyaMessage(frame.seq, frame.command, frame.payload))
                        continue

                    # BUG FIXED HERE: a spontaneous report could satisfy a
                    # waiter that was expecting the reply to OUR command.
                    # Because the counter resync (v0.9.0) deliberately
                    # mirrors the device's numbering, our sends land in the
                    # same number space the device uses for its own reports,
                    # so collisions are not rare - they are guaranteed on a
                    # chatty device. Seen in a live frame trace: our
                    # heartbeat went out as seq 40272 and the device's own
                    # report arrived as seq 40272 in the same window. A
                    # report matching a pending sequence number would then
                    # be handed back as "the reply", and the real reply,
                    # arriving with no waiter left, was dropped. Spontaneous
                    # reports are always treated as pushes unless we
                    # explicitly asked for that command.
                    is_spontaneous = (
                        frame.command == CMD_STATUS_REPORT
                        and CMD_STATUS_REPORT not in self._pending_cmd
                    )
                    fut = None if is_spontaneous else self._pending.get(frame.seq)
                    if fut and not fut.done():
                        fut.set_result(parsed)
                    elif obj and self._on_update:  # push (incl. spontaneous reports)
                        # GAP FIXED HERE: the reference resynchronizes its
                        # sequence counter to the DEVICE's on every
                        # unsolicited status frame (`_status_update`:
                        # `if msg.seqno > 0: self.seqno = msg.seqno + 1`).
                        # The device drives the numbering; without following
                        # it our counter drifts, replies come back carrying
                        # seqnos nobody is waiting on, and every command
                        # then times out - which this integration's
                        # heartbeat loop reads as a dead connection and
                        # tears down a healthy socket. (Our counter
                        # pre-increments where the reference's
                        # post-increments, so `_seq = seqno` here gives the
                        # same next-send value as its `seqno + 1`.)
                        # MUST only ever move FORWARD. BUG FIXED HERE, and
                        # it was introduced by this very resync in v0.9.0:
                        # a device numbers its own unsolicited pushes from
                        # its own low counter, so assigning it outright
                        # REWINDS ours. The next sends then reuse sequence
                        # numbers already in flight, a second request
                        # overwrites the first's entry in _pending, and the
                        # orphaned first request waits out its full timeout.
                        # Seen live: 569 "no heartbeat reply within 10s"
                        # warnings on one device, each one tearing down a
                        # perfectly healthy connection - the heartbeat was
                        # fine in isolation and only failed because the
                        # coordinator's concurrent status() poll kept
                        # colliding with it after a rewind.
                        if frame.seq > self._seq:
                            self._seq = frame.seq
                        dps = _extract_dps(parsed)
                        if dps:
                            self._on_update(dps)
        except asyncio.CancelledError:
            return
        except (ConnectionResetError, OSError) as err:
            # See _handle_connection_lost's docstring: this used to be a
            # silent `pass` here, leaving the connection looking healthy
            # (`connected` still True) with nobody reading it anymore until
            # the next outgoing heartbeat's write happened to fail too.
            if not self._is_closing and self._writer is not None:
                # Detach ourselves first, same reasoning as
                # _heartbeat_loop's detach: _teardown() cancels
                # _listen_task, and this code IS that task - cancelling the
                # currently-running task would throw CancelledError into
                # our own return below. _heartbeat_task is a DIFFERENT
                # task, so _teardown() cancelling that one is a normal,
                # safe cross-task cancellation.
                self._listen_task = None
                self._handle_connection_lost(f"{type(err).__name__}: {err}")
            return
        # A clean EOF (`chunk == b""` above) is also a real disconnect -
        # the device closed its end without resetting - and was silently
        # falling through to nothing for the same reason as the except
        # branch above.
        if not self._is_closing and self._writer is not None:
            self._listen_task = None
            self._handle_connection_lost("device closed the connection (EOF)")

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
            elif raw.startswith(b"3.1"):
                # "3.1" (3 bytes) + 16-byte MD5-hexdigest signature, then a
                # BASE64-encoded ciphertext.
                # BUG FIXED HERE: the base64 layer was missing entirely -
                # the reference's AESCipher.decrypt() takes `use_base64`
                # and this is the ONE call site that leaves it at its
                # default True (the 3.3/3.4 paths all pass False). Without
                # it a 3.1 device's every reply failed to decrypt and was
                # swallowed as an undecodable frame, so a 3.1 device could
                # never report state at all. Note the check is on the
                # PAYLOAD prefix, not on self.protocol_version: a device
                # configured as 3.3 can still answer in this shape.
                text = self._decrypt_raw(base64.b64decode(raw[19:])).decode("utf-8")
            else:  # 3.3 (or 3.1 non-CONTROL replies, same shape as 3.3)
                payload = raw
                # BUG FIXED HERE: this required the WHOLE 15-byte version
                # header to match - the version bytes plus twelve zero
                # bytes. The reference only tests the 3-byte version prefix
                # (`payload.startswith(self.version_bytes)`) and then strips
                # the full 15. A device that sends "3.3" followed by twelve
                # bytes that are not all zero therefore had its header left
                # in place here, leaving a payload that is not an AES block
                # multiple, so decryption failed and the frame was dropped.
                # Not theoretical: on a live instance both an air
                # conditioner and a bathroom device were dropping every
                # spontaneous 0x08 report this way - "rx 0x08 95B ->
                # DROPPED (undecodable)", and 95 is exactly 15 + 80. Since
                # spontaneous reports are how this integration is supposed
                # to learn about changes at all (see coordinator.py), those
                # devices were silently reduced to the 30-second fallback
                # poll.
                if payload.startswith(self.version_bytes):
                    payload = payload[len(self.version_header) :]
                elif self.dev_type == DEV_TYPE_0D and (len(payload) & 0x0F) != 0:
                    # type_0d heuristic, ported verbatim from the reference:
                    # these devices prepend the version header WITHOUT the
                    # version bytes matching, so the only tell is that the
                    # remaining length isn't an AES block multiple.
                    payload = payload[len(self.version_header) :]
                if payload[:1] == b"{" and payload[-1:] == b"}":
                    text = payload.decode("utf-8")  # already-plaintext ack/edge case
                else:
                    text = self._decrypt_raw(payload).decode("utf-8")
        except Exception:  # noqa: BLE001 - malformed/heartbeat-ack/undecodable frame
            return None

        # GAP FIXED HERE: the reference switches dev_type the moment a
        # device answers with this specific error - it is the ONLY signal
        # that this device speaks the type_0d dialect (see DEV_TYPE_0D).
        # Without this the reply was just unparseable JSON, discarded
        # silently, and the device reported state forever. status() sees
        # the changed dev_type and re-sends in the right dialect.
        if "data unvalid" in text:
            self.dev_type = DEV_TYPE_0D
            _LOGGER.debug(
                "%s: 'data unvalid' from device - switching to %s", self.device_id, DEV_TYPE_0D
            )
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
    # BUG FIXED HERE: the reference's parse_header() sanity-checks this and
    # raises ("Header claims the packet size is over 1000 bytes! It is most
    # likely corrupt"); this had no such check. A corrupt/desynced length
    # field made the branch below decide "not enough data yet" and wait for
    # bytes that are never coming - the receive buffer then NEVER yields
    # another frame, so every later reply queues up behind the bogus one
    # and every command times out, while the socket still looks perfectly
    # healthy. Raising here lets the caller resynchronize instead.
    if length > MAX_PAYLOAD_LEN:
        raise TuyaProtocolError(
            f"header claims a {length}-byte packet - almost certainly corrupt"
        )
    total = HEADER_SIZE + length
    if len(buf) < total:
        return None, 0
    footer_size = FOOTER_SIZE_HMAC if hmac_framed else FOOTER_SIZE
    payload_len = length - RETCODE_SIZE - footer_size
    payload_start = HEADER_SIZE + RETCODE_SIZE
    payload = buf[payload_start : payload_start + max(payload_len, 0)]

    # Checksum verification, matching the reference's unpack_message():
    # note it LOGS a mismatch but still returns the message rather than
    # discarding it, so this deliberately does the same - a wrong CRC has
    # never been the reason a frame was unusable in practice, and dropping
    # it would lose real data the reference would have kept.
    if not hmac_framed:
        body = buf[: total - FOOTER_SIZE]
        (want_crc, suffix) = struct.unpack(">II", buf[total - FOOTER_SIZE : total])
        if suffix != SUFFIX:
            _LOGGER.debug("Frame suffix wrong: %08X != %08X", suffix, SUFFIX)
        if want_crc != _crc32(body):
            _LOGGER.debug("Frame CRC wrong: %08X != %08X", _crc32(body), want_crc)

    return _RawFrame(seq, command, payload), total


def _extract_dps(msg: TuyaMessage) -> dict[int, Any]:
    if not msg.payload:
        return {}
    dps = msg.payload.get("dps")
    if not dps:
        return {}
    return {int(k): v for k, v in dps.items()}
