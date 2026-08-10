# Changelog

## v0.13.4 - the REAL cause of the permanently-stuck devices: my own v0.13.1 fix

v0.13.3 diagnosed the wrong scope. The user deleted and re-added one of
the stuck devices - a live action, not a Home Assistant restart - and it
failed the exact same way, which the bootstrap-timeout theory cannot
explain (bootstrap isn't running during a live re-add). Reproduced
standalone, outside Home Assistant entirely, with logging proof:

```
connection lost (device closed the connection (EOF)) - waiting for discovery broadcast or periodic retry
...
asyncio.exceptions.CancelledError
```

The device accepts the TCP connection, receives `SESS_KEY_NEG_START`,
then drops the connection (EOF) before replying - plausibly the same
weak/marginal link flagged in v0.13.0. v0.13.1 (shipped hours earlier in
this same session) made the read side notice that drop immediately
instead of dying silently, which was correct - but `_teardown()`'s
cleanup of in-flight waiters used `fut.cancel()`, which is semantically
wrong for "the connection died": cancellation means the waiter's own
caller asked to stop, not that the answer will never come. The 3.4
handshake, waiting on exactly such a future, saw a bare
`asyncio.CancelledError` escape - which v0.13.3 correctly stopped from
crashing the entry permanently, but the UNDERLYING problem was reproducing
on every single attempt, delete-and-re-add included, because this
teardown path manufactured a fresh CancelledError every time, regardless
of who was asking or how long they were willing to wait.

`_teardown()` now completes in-flight waiters with a proper
`TuyaProtocolError("connection lost while awaiting a reply")` instead of
cancelling them - a normal, catchable exception with a clear cause.
Verified against the live device: three consecutive attempts now each
fail with that clear error (the device is still dropping the connection -
a separate, real link/device issue - but the ERROR REPORTING is now
correct, which is what was actually broken).

## v0.13.3 - a busy Home Assistant startup was permanently killing device entries

Reported directly: two auto-discovered devices ("Pasillo abajo", "WiFi
Watering Pump 2") could never connect, stuck forever. Their traceback:

```
File ".../tuya_lan.py", line 765, in _send_receive_raw
    return await asyncio.wait_for(fut, timeout=SEND_TIMEOUT)
asyncio.exceptions.CancelledError
```

`asyncio.CancelledError` has inherited from `BaseException`, not
`Exception`, since Python 3.8 - specifically so a bare `except Exception`
can never accidentally swallow a real cancellation. This integration's
own `except Exception` around `device.connect()` therefore let it pass
straight through, uncaught, bypassing `ConfigEntryNotReady` entirely -
and Home Assistant's own entry-setup wrapper marks an entry that fails
this way `setup_error`, not the auto-retried `setup_retry`.
`setup_error` entries do not retry on their own; they sit dead until
someone manually reloads them or restarts Home Assistant outright.

Where the cancellation actually came from, confirmed from the SAME
moment in the log: Home Assistant's own bootstrap has a startup-phase
timeout (`"Something is blocking Home Assistant from wrapping up the
start up phase..."`, logged simultaneously and naming OTHER slow
integrations, not this one) that cancels whichever entry setups are
still in flight when it fires. A 3.4 device mid-handshake, waiting on
`SEND_TIMEOUT`, was exactly what was in flight on both affected entries.
This is not a device failing - it is Home Assistant's own startup taking
too long for unrelated reasons and asking every in-progress setup to
yield, and this integration was treating that cooperative request as a
fatal, unretried error.

Both the connect and first-refresh paths now catch `CancelledError`
specifically and convert it to `ConfigEntryNotReady`, so a device
cancelled by a busy startup retries on Home Assistant's own schedule
shortly after, instead of sitting dead until a full restart. Verified
with a test reproducing the exact cancellation shape.

This may also explain part of the separately-reported frequent Core
restarts: an entry stuck in `setup_error` does not self-heal, and a full
Home Assistant restart was likely the only thing that appeared to fix it
- which would explain restarts recurring on a system that is already
slow to boot.

## v0.13.2 - reconnect the instant a device disconnects

Reported directly: recovery needs to happen immediately, not eventually.

Until now, nothing tried reconnecting AT THE MOMENT a device dropped.
`coordinator.py`'s disconnect hook only marked entities unavailable;
actual recovery depended entirely on either the next broadcast heard
from that device (which some devices send rarely or not at all) or the
60s periodic sweep - so a disconnect landing right after that sweep just
ran could sit unavailable for up to a minute for no reason, when the
disconnect itself is already the strongest possible signal to try again
right now.

A second listener is now chained onto the same disconnect callback: the
coordinator's own hook still runs first (marks unavailable immediately),
then a reconnect is scheduled instantly - still gated by
`seconds_until_retry()` (v0.13.0), so a device already failing
repeatedly still backs off instead of being hammered on every drop.
Verified: a normal disconnect schedules a reconnect immediately; a
disconnect while backed off does not, but the coordinator is still
notified either way.

## v0.13.1 - the read side was dying silently on a real disconnect

Root cause of the "healthy heater suddenly resets" case left open in
v0.13.0.

`_listen()`'s read loop caught `ConnectionResetError`/`OSError` from
`_reader.read()` and just `pass`ed - the task quietly ended, but nothing
else happened: `_teardown()` was never called, `_on_disconnect` was never
notified, and `connected` kept reporting `True` (only the writer's state
is checked, and nothing had touched it). With nobody reading anymore,
every pending reply just sat there until its own 10s timeout, and no
push update could ever arrive again.

The only thing that eventually noticed was the heartbeat loop, on its
NEXT write - up to a full `HEARTBEAT_INTERVAL` (10s) later, sometimes
more. That is exactly the delay between "the device actually dropped the
connection" and "connection lost (ConnectionResetError: Connection lost)"
finally getting logged, seen on a live heater that had been working
normally for several minutes beforehand: the write side, not the read
side, was what the log ultimately blamed, when the read side had already
died first and said nothing.

A clean EOF (`read()` returning `b""`, i.e. the device closed its end
without resetting) fell through the same silent gap.

Both paths now report through a single `_handle_connection_lost()`,
called immediately from wherever the drop is actually detected - no more
waiting for the next heartbeat to notice a connection that already died.
Verified with a test: a read that raises `ConnectionResetError` now
tears the connection down and fires `_on_disconnect` immediately, where
before it left `connected` reporting `True` forever.

This explains why detection was slow and inconsistent; it does not by
itself explain why the device resets the connection in the first place,
which may simply be normal behavior for this class of device.

## v0.13.0 - back off after repeated reconnect failures

Diagnosed on a live instance: a battery-powered outdoor watering valve
(protocol 3.4) went 14+ minutes with EVERY reconnect attempt failing at
the session-key handshake - `SESS_KEY_NEG_START` sent, zero replies,
ever - while a plain ICMP ping to the same host showed 0% loss. Whatever
the exact cause (a marginal WiFi link losing a larger handshake reply
while still answering tiny ICMP echoes, or the device declining to
renegotiate too soon after a previous attempt), the integration was not
helping: it has THREE independent reconnect triggers (initial setup, the
60s periodic timer, every broadcast heard while disconnected), and NONE
of them backed off. A struggling device got a fresh handshake attempt
roughly every 10-15s, forever, with no increasing gap to give a marginal
link - or the device itself - any room to recover.

`TuyaLocalDevice` now tracks consecutive connection failures.
`seconds_until_retry()` returns 0 for the first two failures (a device
that just rebooted or hit one bad packet should still reconnect as fast
as possible - that's the whole point of reacting to every broadcast), and
from the third failure on, backs off exponentially (30s, 60s, 120s...
capped at 10 minutes) until a connection actually succeeds. Both
reconnect paths in `__init__.py` now consult it before attempting.

This does not by itself explain every disconnect - a separate live
capture showed a working, correctly-configured 3.3 heater get an outright
`ConnectionResetError` from the device itself after functioning normally
for several minutes, a different failure mode from the 3.4 handshake
timeout above. Both share the same root problem this release addresses
(no breathing room between attempts), but the underlying reason a healthy
connection resets on its own remains open.

## v0.12.3 - show WHAT is being written, not just that a write happened

Investigating a live report of many devices dropping connection led to a
genuinely alarming pattern in the frame trace of one AC: a continuous
stream of CONTROL writes (`0x07`), two back-to-back, every 100-300ms,
non-stop for over an hour - clearly not a person clicking a thermostat
that fast. The trace could show THAT something kept writing, but not
WHAT, which is the one thing needed to find the source (a runaway
automation, a UI feedback loop, or a bug in this integration itself).

`_send_receive_json()` now previews the DPs being written (plaintext -
this is our own outgoing data, not a secret) alongside CONTROL/
CONTROL_NEW frames in the trace. Next diagnostics download from an
affected device will show exactly what is being set on every write,
which turns "something is writing a lot" into "X keeps getting set to Y".

## v0.12.2 - stop dropping spontaneous reports; make an unreadable device say so

Two findings from reviewing live diagnostics for two misbehaving devices.

### Every spontaneous report was being dropped

Both devices' traces showed the same line:

```
rx seq=0  0x08  95B  -> DROPPED (no waiter, undecodable)
```

95 is exactly 15 + 80: a version header plus five AES blocks. The header
was not being stripped, so what remained was not a block multiple,
decryption failed, and the frame was discarded.

The cause is a porting mistake. The reference tests only the **3-byte
version prefix** (`payload.startswith(self.version_bytes)`) before
stripping the full 15-byte header; this required the entire 15 bytes to
match - version bytes plus twelve zero bytes. A device that sends "3.3"
followed by twelve bytes that are not all zero kept its header and had
every such frame thrown away.

Spontaneous reports are how this integration is meant to learn about
changes at all (see coordinator.py's "reactive, not polling" design), so
affected devices were silently reduced to the 30-second fallback poll.

### A device that cannot be decrypted now says so

One entry was connected, heartbeating happily, and had **zero
datapoints** - because an old active-scan false positive had given it
another device's IP, so it was talking to the wrong host with the right
key. Every reply came back undecryptable, `status()` returned `{}` as
designed, and nothing anywhere said a word.

After three consecutive polls with no usable data, the log now names the
device and address and says plainly that the device is replying and
cannot be read - typically a key that does not belong to whatever is
actually at that address.

## v0.12.1 - stop spontaneous reports impersonating command replies

From a live report of a device dropping its connection twice, and the
frame trace that explained it. The watering pump emits a spontaneous
state report (command `0x08`) roughly **once per second**, and the trace
caught this:

```
tx seq=40272 0x09   <- our heartbeat
rx seq=40272 0x08   <- the device's own report, SAME sequence number
rx seq=40273 0x09
```

The sequence-counter resync added in v0.9.0 deliberately mirrors the
device's numbering, which means our sends land in the same number space
the device uses for its own reports. On a chatty device, collisions are
not rare - they are guaranteed. A colliding report would then be handed
back as "the reply" to whatever command was waiting on that sequence
number, and the real reply, arriving with no waiter left, was dropped.

Command `0x08` was not even defined here (`CMD_STATUS = 0x0a` is what
*we* send to ASK for state; `0x08` is what the device sends
spontaneously). Now defined as `CMD_STATUS_REPORT`, and a spontaneous
report is always routed as a push unless that exact command was
explicitly requested.

Also fixes the blocking call Home Assistant was flagging on this
integration: `_builtin_profiles()` scans a directory and reads files, and
was being called straight from the event loop during the config flow
("Detected blocking call to scandir ... inside the event loop"). It now
runs in an executor.

## v0.12.0 - correct a wrong stored protocol version from the broadcast

The diagnostics platform paid for itself immediately. On the live
instance, three devices stuck in `setup_retry` all reported the same
thing:

```
192.168.1.43  v3.3  session_key=False  seq=2
  tx seq=1  0x0a  152B  awaiting seq 1     <- never answered
  tx seq=2  0x09  104B  awaiting cmd 0x09
```

Those three devices **broadcast `version: 3.4`** - the integration's own
discovery cache had them right - but their config entries stored `3.3`,
the config-flow default used whenever the discovery snapshot carried no
version. At 3.3 the code sends a plain `DP_QUERY` (0x0a) and never
negotiates a session key, and a 3.4 device simply never answers that. So
the entry failed forever, while the exact same code talking 3.4 to the
exact same devices worked first time.

A wrong stored IP was already corrected from the broadcast; a wrong
stored protocol version was not - and it is fatal in a way a wrong IP is
not. `_on_device_seen` now corrects both, on the same mechanism: update
the entry, let Home Assistant's update listener reload it. Devices
mis-stored this way repair themselves on the next broadcast, with no user
action.

Also: `SUPPORTED_PROTOCOL_VERSIONS` was still `["3.1", "3.3", "3.4"]`
even though 3.2 support landed in v0.10.0 - so a 3.2 broadcast would have
been rejected by this very check. Added.

## v0.11.1 - keep the frame trace when setup FAILS

v0.11.0's diagnostics had the wrong hole in it, found the moment it was
pointed at the live instance: an entry that fails to set up has no device
object in `hass.data`, so its diagnostics reported exactly
`{"loaded": false}` and nothing else - useless for precisely the entries
worth diagnosing, which are the failing ones.

The state snapshot and frame trace are now captured at the point of
failure (both the connect and the first-refresh path) and surfaced under
`last_setup_failure`: the error, address, protocol version, `dev_type`,
whether a 3.4 session key was negotiated, the sequence counter, the
outstanding waiters, and the frames that crossed the wire before it gave
up.

## v0.11.0 - diagnostics platform with a per-device frame trace

Home Assistant now offers a **Download diagnostics** button on every Tuya
Orchestrator config entry.

This exists to close a concrete gap. A device can complete its handshake
and then have every query time out, and the only thing that answers *why*
is the sequence of frames that actually crossed the wire. That detail is
logged at DEBUG - and DEBUG is unreachable from outside the instance:
recent Home Assistant no longer exposes `/api/error_log`, and the API
that remains (`system_log`) carries only WARNING and above. Diagnosing
the three protocol-3.4 devices currently stuck in `setup_retry` on a live
instance ran straight into this.

Each device entry's diagnostics include:

- connection state: address, protocol version, `dev_type`, whether a 3.4
  session key was negotiated, the current sequence counter, and which
  waiters are outstanding (by sequence number and by command) - the exact
  state needed to tell "no reply arrived" apart from "a reply arrived and
  landed on the wrong waiter";
- the coordinator's last update result, last exception and current DP
  values;
- the profile's DP map;
- a rolling trace of the last 60 frames, each recording direction,
  sequence number, command, size, and **which waiter it was routed to** -
  including frames that were dropped for having no waiter, and any that
  failed to parse.

Account entries and unloaded entries get the useful subset, plus the
domain-wide LAN discovery state (which devices have been heard
broadcasting, at which IP and protocol version) - usually the first
question for a device that will not connect.

**Redaction**: local keys, cloud credentials and the account UID never
appear. Frame traces keep only the first 48 bytes of each frame - enough
for the header, retcode and the start of the payload, not enough to carry
a whole encrypted DP payload out of the instance.

## v0.10.1 - fix the sequence-counter rewind introduced in v0.9.0

Diagnosed against the live Home Assistant instance (2026.8.1) rather than
from reports. Its log showed **569** occurrences of
`connection lost (no heartbeat reply within 10s)` on a single device -
a device that answers heartbeats perfectly when tested in isolation
(verified directly on the LAN: it replies to HEART_BEAT with seqno 0 and
an empty payload, exactly as expected).

The cause was the sequence-counter resync added in v0.9.0. Following the
device's numbering was right; assigning it outright was not. A device
numbers its own unsolicited pushes from its own low counter, so the
resync could REWIND ours. Subsequent sends then reused sequence numbers
still in flight, a second request silently overwrote the first's entry in
`_pending`, and the orphaned first request waited out its full 10s
timeout. The heartbeat only failed in Home Assistant - and not in
isolated testing - because there the coordinator's concurrent `status()`
poll is what collided with it after a rewind.

Two fixes:

1. The resync now only ever moves the counter **forward**
   (`if frame.seq > self._seq`), preserving the intent without ever
   reusing a number.
2. Registering a waiter no longer silently displaces an in-flight one.
   The reference is loud about this too (`wait_for`: `if seqno in
   self.listeners: raise`), and silently overwriting is exactly what
   turned this into unexplained timeouts instead of a visible error.

Also: the Spanish translation used Rioplatense forms ("asegurate",
"ingresá", "revisá", "elegí", "pegá") in strings shown in the config
flow. Corrected to Peninsular Spanish.

## v0.10.0 - locate legacy devices by MAC; protocol 3.2 support

Continued testing directly against the live account and LAN.

**Protocol 3.2 was rejected outright** by the constructor, so a 3.2
device could not be used at all. The reference supports it
(`set_version()`: *"3.2 behaves like 3.3 with type_0d"*) - it frames
exactly like 3.3 but starts in the type_0d dialect. Added.

**Protocol 3.1 was encrypting payloads it must send in the clear.** In
the reference's `_encode_message` the chain is `if 3.4 / elif >= 3.2 /
elif cmd == CONTROL` with NO trailing else, so on 3.1 every command that
is not CONTROL (DP_QUERY, HEART_BEAT) goes out as PLAIN JSON. Sending
those encrypted means a 3.1 device receives a query it cannot parse and
never answers.

**New: locate legacy devices by their MAC.** Tuya's older 20-character
device ids end in the device's own MAC - `03636268ec64c9d1cacc` is the
device at `ec:64:c9:d1:ca:cc`. Verified against three live devices, each
resolving to the correct host straight out of the ARP cache (one of them
at an address the port sweep had not even reported).

This matters because those legacy devices are precisely the ones that
stop broadcasting after boot, so passive discovery never sees them - and
brute force cannot help either, for a reason measured here directly: **a
Tuya device serves exactly ONE LAN session at a time.** A second client's
TCP connect is accepted and then simply never answered (verified:
connection A kept working and answering while a concurrent connection B
timed out on every query, A unaffected). Reading the ARP cache costs
nothing, is exact rather than a guess, and - unlike probing - touches no
device and cannot disturb an existing session. It now runs first, before
any sweep.

## v0.9.2 - findings from testing against the real account and LAN

First session run directly against the live account and network rather
than from reports. 18 cloud devices, 4 broadcasting (3x protocol 3.4,
1x 3.3), 9 hosts with the LAN port open.

**Confirmed working against real hardware for the first time:** protocol
3.4's session-key handshake negotiated successfully on all three 3.4
devices and returned real DP data, as did the 3.3 device. Until now 3.4
had only ever been verified against synthetic frames.

**Measured, and it changes how scanning must behave:** a Tuya device
serves exactly ONE LAN session at a time. A second client's TCP connect
is ACCEPTED but the device then never answers it - verified directly
(connection A kept working; a concurrent connection B connected and then
timed out on every query, while A was unaffected). This is why a device
already connected by Home Assistant looks unreachable to anything else
probing it, despite an open port.

**Two bugs fixed:**

1. `_PROBE_VERSIONS` was `("3.3", "3.4")` - **protocol 3.1 was never
   probed at all**, so a 3.1 device could not be identified by an active
   scan however reachable it was. Not hypothetical: this account's older,
   short-device-id equipment (two air conditioners, a heater, a power
   strip) is that vintage, and those are also the devices least likely to
   broadcast - i.e. exactly the ones that depend on active scanning.
2. The scan probed every open host including ones already configured as
   devices. Given the single-session behaviour above, those probes can
   only ever time out - guaranteed dead time, now multiplied by every
   protocol version and every candidate key. Hosts already configured are
   skipped: a device we are already connected to is by definition not one
   we are looking for.

## v0.9.1 - say WHY a connection dropped

From a real report: the `connection lost - waiting for discovery
broadcast or periodic retry` warning named the device but not the cause,
because the cause (heartbeat timeout vs a specific exception) was only
logged at debug level. A warning that tells you something broke without
telling you what is not much use. The reason is now part of the message
itself, and the reply timeout is a named constant (`SEND_TIMEOUT`) rather
than a literal buried in the send path.

## v0.9.0 - the rest of the protocol layer: sequence-number sync and stream robustness

v0.8.0's claim of a complete pass was overstated: three blocks of the
reference (`pack_message`/`unpack_message`/`parse_header`, the
`TuyaProtocol` transport callbacks, and `_encode_message`) had been taken
on trust from earlier partial reviews rather than actually re-read. Read
properly now. **Five more real differences, four of them bugs:**

1. **Our sequence counter never followed the device's.** The reference
   resynchronizes on every unsolicited status frame
   (`_status_update`: `if msg.seqno > 0: self.seqno = msg.seqno + 1`). The
   device drives the numbering; without following it our counter drifts,
   replies come back carrying seqnos nobody is waiting on, every command
   times out - and the heartbeat loop reads those timeouts as a dead
   connection and tears down a healthy socket. A direct cause of
   unexplained disconnections. Verified with a test: a device push with
   seqno 57 now moves our next send to 58.
2. **Protocol 3.4 never adopted the session's starting seqno.** The
   reference takes it from the handshake reply, with the reason in its own
   comment: *"for 3.4 devices, we get the starting seqno with the
   SESS_KEY_NEG_RESP message"*. Without it every post-handshake reply on a
   3.4 device carried an unexpected seqno - matching the "unknown state on
   every value" symptom seen on the 3.4 bulbs.
3. **A corrupt length field silently froze the connection.** The
   reference's `parse_header()` rejects packets claiming over 1000 bytes
   ("most likely corrupt"); this had no such check, so a desynced length
   made the parser wait for bytes that never come. The receive buffer then
   never yielded another frame: every later reply queued behind the bogus
   one and every command timed out, while the socket still looked healthy.
4. **A malformed byte run killed the listener outright.** `_try_parse()`
   raises on a bad prefix, and that exception type was not caught by the
   read loop - so the task died silently as an unretrieved task exception.
   The socket stayed open and `connected` stayed `True`, but nothing was
   ever read again, with no error anywhere pointing at the cause. Now
   resynchronizes to the next frame boundary. Verified with a test that
   feeds garbage followed by a valid frame.
5. **Pending waiters were not released on teardown** (the reference's
   `dispatcher.abort()`), so each burned its full 10s timeout on a socket
   already known to be gone.

Also aligned: CRC/suffix verification now logged on mismatch (the
reference logs but still returns the frame, so this deliberately does not
discard it either), and the heartbeat loop sends before sleeping rather
than after, proving the connection immediately instead of after a full
interval of silence.

## v0.8.0 - full line-by-line diff of the reference protocol layer

The remaining gap: previous versions diffed `discovery.py` and the
connection lifecycle, but `pytuya/__init__.py` (the protocol layer) had
only ever been checked in pieces, as individual bugs came up. This is the
complete pass over it, plus the device-layer features that were missing.

**Four real bugs found:**

1. **Heartbeat tore down healthy connections on protocol 3.1.** The
   reference carries an explicit hack here, with the reason in a comment:
   *"Heartbeats on protocols < 3.3 respond with sequence number 0, so they
   can't be waited for like other messages"* - it dispatches heartbeat
   replies by COMMAND via a `HEARTBEAT_SEQNO` sentinel. This code waited
   on the echoed sequence number like every other command, so on a 3.1
   device every heartbeat waited for a seqno the device never sends, timed
   out after 10s, and `_heartbeat_loop` read that timeout as a dead
   connection - killing a perfectly healthy socket every 10 seconds,
   forever. Now waits by command (correct on every version). Verified with
   a test that replies with seqno 0: the heartbeat resolves and the
   connection survives.
2. **`type_0d` devices were entirely unsupported.** Some devices reject a
   normal `DP_QUERY` with a `"data unvalid"` payload and require
   `CONTROL_NEW` (0x0D) carrying an EXPLICIT list of the DPs being asked
   for. There is no cloud metadata field for this - the reference detects
   it at runtime from that error payload, switches `dev_type`, and
   re-sends once. Without it such a device pairs perfectly and then
   reports **nothing, forever**. Ported in full: `dev_type`,
   `dps_to_request`, `add_dps_to_request()`, runtime detection in
   `_decode_frame_payload`, the one-shot re-send in `status()`, and the
   `(len(payload) & 0x0F) != 0` header-strip heuristic. `DeviceProfile`
   gained `all_dp_ids()` (including composite mappings' DPs, not just flat
   `dps:` entries - a bare vacuum/climate profile would otherwise register
   an empty request list) and `__init__.py` registers it before the first
   query.
3. **Protocol 3.1 replies never decoded.** The reference's
   `AESCipher.decrypt()` takes a `use_base64` flag; the 3.1 path is the
   one call site that leaves it at its default `True`. This code was
   missing the base64 layer entirely, so every 3.1 reply failed to decrypt
   and was swallowed as an undecodable frame - a 3.1 device could never
   report state at all. Also corrected the check to look at the payload
   prefix rather than the configured protocol version, matching the
   reference (a device configured as 3.3 can still answer in this shape).
4. **`local_key` was never refreshed.** Tuya rotates a device's local_key
   whenever it is re-paired from the phone app - routine user behavior.
   With a stale key the LAN handshake fails forever and the only fix was
   deleting and re-adding the device. The reference re-fetches it from the
   cloud and rewrites the entry (`update_local_key()`); ported as
   `_async_refresh_local_key()`, attempted on connection failure and
   reported through `ConfigEntryNotReady` so HA retries with the new key.

Also aligned: `UPDATEDPS` (0x12) added to the no-version-header command
set, matching the reference's `NO_PROTOCOL_HEADER_CMDS`.

**Confirmed equivalent** (checked, no change needed): payload templates
for every command, the space-stripped JSON encoding (`separators=(",",
":")` vs the reference's `.replace(" ", "")` - the device rejects payloads
with spaces), frame packing/parsing including the retcode field, the CRC32
vs HMAC-SHA256 footer split, the 3.4 session-key negotiation, the 3.4
`data.dps` unwrapping, and the unsolicited-status dispatch path.

## v0.7.0 - automatic IP re-resolution and reconnect, no more remove/re-add

Reported live: after a device's DHCP lease renewed with a new IP, the
integration kept dialing the old address forever - the only way to
recover was to delete and re-pair the device by hand. That's not how
`localtuya` behaves and it isn't how this integration is meant to behave
either, given `PersistentDiscovery` (running continuously since
`async_setup()`) already hears every broadcast a device sends, IP
included.

Ported localtuya's exact mechanism (`__init__.py`'s `_device_discovered` +
`_async_reconnect`), 1:1:

1. **Live IP re-resolution**: `PersistentDiscovery` now calls a callback
   on every broadcast it decodes, not just on new/first-seen devices (see
   `discovery.py`'s `_DiscoveryProtocol.datagram_received`). `__init__.py`
   registers `_on_device_seen`, which checks every configured device
   entry for an IP mismatch against what was just heard and, if it
   differs, updates the `ConfigEntry`'s `address` via
   `hass.config_entries.async_update_entry(...)`. HA's own
   `add_update_listener` (already wired per entry) reacts to that data
   change by reloading the entry, which reconnects with the fresh IP -
   no user action needed, exactly like a DHCP-based device is supposed to
   behave.
2. **Periodic reconnect for IP-unchanged drops**: a device that just lost
   its TCP connection (reboot, brief wifi loss, TCP reset) without
   actually changing IP doesn't trigger the path above. Added a
   `RECONNECT_INTERVAL` (60s, same value localtuya uses) timer in
   `async_setup()` that retries `device.connect()` for any configured
   device currently showing `connected == False`.

3. **Immediate reconnect on hearing from a disconnected device**: found
   on a follow-up line-by-line diff of localtuya's `_device_discovered`,
   which ends by calling `device.async_connect()` for any device it is
   not currently connected to - on EVERY broadcast, not only on an IP
   change. This matters for the common case of a device rebooting while
   keeping its DHCP lease: the IP-change branch never fires, so without
   this the device sat disconnected for up to a full 60s until the
   periodic retry noticed, even though its broadcast was already proof it
   was back. Now reconnects the moment it's heard.

4. **Connection re-entrancy guard** - and this one was a bug items 1-3
   actively made WORSE. There are now three independent triggers that can
   ask for a connection (entry setup, the 60s timer, the broadcast
   callback), and `connect()` had no guard at all: two of them racing each
   opened its own socket and its own listen task, while only the
   last-assigned writer stayed reachable. The orphaned socket kept
   consuming frames that then never reached the coordinator - which
   presents as a device that randomly stops updating for no visible
   reason. localtuya has always guarded this (`async_connect()`:
   `if not self._is_closing and self._connect_task is None and not
   self._interface`). Ported as a `_connect_lock` plus an
   already-connected re-check. Verified: 5 concurrent `connect()` calls
   now open exactly 1 socket (previously 5).
5. **`close()` was terminal but used for transient failures**: a failed
   3.4 handshake called `close()`, which now permanently latches the
   device closed. Split into `close()` (terminal, for entry unload) and
   `_teardown()` (drop the socket, stay reconnectable), with the
   handshake path using the latter. `_teardown()` also clears `_writer`/
   `_reader` instead of leaving them pointing at dead objects.
6. **Silent disconnections**: when the heartbeat failed, the old code
   closed the socket and told nobody - entities kept showing their last
   known values as though live, indefinitely, until some later poll
   happened to fail. localtuya dispatches None to its entities at exactly
   this point (`disconnected()`), marking them unavailable. Added an
   `_on_disconnect` callback wired to the coordinator's
   `async_set_update_error`, plus the equivalent "waiting for discovery
   broadcast" warning - which is now literally accurate, since item 3
   reconnects on the next broadcast heard.

Together these remove the last real case where "remove and re-add the
device" was the only fix - that should no longer ever be necessary for a
device that's genuinely reachable on the LAN.

### Verified differences vs localtuya's `discovery.py` (deliberate, documented)

A line-by-line diff of localtuya's discovery module against this one, so
the remaining differences are on record rather than assumed away:

- **Ports**: localtuya binds 6666 + 6667. This binds 6666 + 6667 + **7000**
  (tinytuya's `UDPPORTAPP`). Superset - strictly more devices found.
- **Frame parsing**: localtuya hardcodes `data[20:-8]` (16-byte header +
  4-byte retcode, 8-byte footer). This reads the length field from the
  header instead and slices accordingly - same result on a well-formed
  packet, but not silently wrong on an unusual one.
- **Cache freshness**: localtuya's `device_found` stores a device in
  `self.devices` **only the first time** it is seen (`if gwId not in
  self.devices`), so its cache holds a device's ORIGINAL IP forever; it
  relies entirely on the callback for IP changes. This overwrites the
  cache entry on every broadcast, so the cache itself stays current too -
  which matters here because `config_flow.py` and `account.py` both read
  that cache directly (v0.6.0's stale-`discovery_info` fix depends on it).
- **0x6699 frames**: localtuya would fail to parse these too (no special
  handling); here they're explicitly detected and debug-logged as a known
  unimplemented format instead of silently failing. Same capability,
  clearer diagnostics.
- **Crypto library**: `cryptography` (localtuya) vs `pycryptodome` (here) -
  same AES-128-ECB, same `MD5(b"yGAdlopoPVldABfn")` key.

Everything else in the discovery path - persistent listener started once
at domain setup, `reuse_port=True`, callback on every datagram, close on
HA stop - matches localtuya's behavior.

## v0.6.0 - full-project audit: 6 more real bugs, including the actual reason the LAN error persisted

Requested explicitly ("revisa todo el puñetero proyecto, conjunto") - a
systematic read-through of every module, not just the LAN protocol files
already diffed against localtuya repeatedly. Found and fixed:

1. **The actual reason "no encontrado en LAN" kept happening after
   v0.5.1's active-scan fix**: `discovery_info` (an HA discovery flow's
   data) is a SNAPSHOT captured when the "Discovered" card was first
   created - HA never refreshes it. Clicking Configure days later replays
   whatever IP was baked in at creation time, stale (DHCP change) or, for
   any card created before the v0.5.1 false-positive fix, outright WRONG
   from the start. The code only re-checked the IP when it was completely
   missing, silently trusting a present-but-possibly-bad one otherwise.
   Now always prefers the live, continuously-updated `PersistentDiscovery`
   cache over the stale snapshot, falling back to the snapshot only if the
   device isn't in the live cache. **Old "Discovered" cards from before
   this fix still carry bad data - dismiss/ignore them and let them
   regenerate, don't click Configure on a stale one.**
2. **Entity unique_id collision**: two `dps:` entries sharing the same
   `dp_id` with different `bit:` values (this AC's display-light/buzzer
   switches, both on dp 123) produced IDENTICAL unique_ids - HA silently
   drops one of the two colliding entities. Fixed to include the bit
   index in the unique_id.
3. **climate.py's hvac_mode fallback returned a hardcoded HVACMode.HEAT**
   even when HEAT isn't one of the specific device's declared
   `hvac_modes` (e.g. a cool-only unit, or an unrecognized raw mode
   value) - returning a mode the entity itself never declared as valid.
   Now falls back to whatever non-OFF mode the device actually supports.
4. **Composite mapping `*_dp` fields weren't type-coerced** the way plain
   `dps:` entries already were - a hand-edited profile with an
   accidentally-quoted dp id (`brightness_dp: "22"` instead of `22`)
   silently never matched `coordinator.data`'s int keys, with no error
   anywhere, just a permanently-unknown entity. Every composite field is
   now coerced through the same `int()` path `dps:` always used.
5. **`tuya_cloud.py`'s `get_device_schema()` only caught
   `TuyaCloudApiError`**, not the separate `TuyaCloudAuthError` class -
   a transient token race on the v1.1 call escaped past the method's own
   "try both endpoints, only fail if both fail" design, preventing v2.0
   from ever being tried even though a fresh token there could well have
   worked.
6. **`_async_setup_device_entry` let a connection failure propagate as a
   raw exception** instead of `ConfigEntryNotReady` - HA logs that as a
   scary "Error setting up entry" traceback (exactly what an earlier live
   report pasted for a `ConnectionResetError`) and doesn't treat it as
   the normal retry-able state Tuya devices being briefly unreachable
   actually is. Also closes the device's connection if the first
   coordinator refresh fails, instead of leaking the socket.

## v0.5.1 - fix: false-positive device matching in active scan, deprecated battery_level

**Critical fix**: `active_scan.py`'s device identification only checked
"did `status()` raise an exception?" - but a WRONG local_key/host/version
combination does NOT reliably raise. `tuya_lan.py`'s `status()`
deliberately swallows any undecryptable reply into an empty `{}` (by
design, so one garbled push doesn't crash a running device), so the old
check was true for ANY host that merely replied to a query at all -
meaning literally any Tuya device open on port 6668 on the LAN could get
"identified" as a match for whichever candidate device_id happened to be
tried against it next, real key or not. Live report matched this exactly:
phantom devices kept getting offered, and a real device's own IP got
silently stolen by a wrong match, leaving it unable to connect ("dice que
no está en LAN" for a device that plainly was). Fixed to require actual
non-empty DPS data back - a real, meaningful identification.

Also fixed a deprecation warning against HA 2026.8: `vacuum.py` exposed
battery via the deprecated `StateVacuumEntity.battery_level` property +
`VacuumEntityFeature.BATTERY`. Replaced with the modern pattern: a plain
companion `sensor.*` entity (`device_class: battery`) on the same device
(`sensor.py`'s new `TuyaVacuumBatterySensor`), which HA's vacuum
more-info dialog picks up automatically via the shared device link - no
feature flag needed at all.

## v0.5.0 - protocol 3.4 support, ported directly from localtuya

At the user's explicit request ("tienes que basar el código en
localtuya"/"trabajar basando el código en localtuya") after RGBCW lights
kept showing every DP as unknown even after all prior fixes: `tuya_lan.py`
is now a careful, direct PORT of localtuya's real
`custom_components/localtuya/pytuya/__init__.py`, not a from-scratch
reimplementation being incrementally patched. Rewrote the whole module
around the reference's actual wire logic rather than re-deriving it.

**Real, working reason this matters**: protocol 3.4 was previously not
implemented at all (`NotImplementedError`) - discovery would either abort
outright (passive path, since v0.2.4) or, worse, active_scan.py
(v0.4.0) **hardcoded a 3.3 probe unconditionally**, meaning a 3.4 device
sitting behind a matching TCP port could still get "identified" and
paired under a wrong protocol assumption depending on how the device
responded to malformed 3.3-shaped traffic - explaining "connects but
every value is unknown" with no visible error, exactly the RGBCW light
symptom. Many modern Tuya RGBCW bulbs are 3.4-only.

**What's newly implemented, ported faithfully from the reference:**
- Full 3.4 session-key handshake (`_negotiate_session_key`): nonce
  exchange + HMAC-SHA256 verification + XOR + AES-derive the session key
  that replaces `local_key` for the rest of the connection.
- 3.4's distinct message framing: the plaintext version header is baked
  INTO the encryption (unlike 3.3, where it's prepended to the
  ciphertext afterward), and the message footer is an HMAC-SHA256 (32
  bytes) instead of a CRC32 (4 bytes).
- 3.4's different command set: DP_QUERY_NEW/CONTROL_NEW replace
  DP_QUERY/CONTROL, with CONTROL_NEW nesting the actual dps under a
  `data` key and using a real int timestamp instead of a string.
- `active_scan.py` now probes both 3.3 and 3.4 for every not-yet-found
  device (previously hardcoded 3.3 only) and reports back whichever
  version actually identified it - `account.py` uses that real detected
  version instead of assuming one.
- `config_flow.py`'s discovery guard no longer aborts on a 3.4-reported
  broadcast version - only 3.5 (a different, still genuinely unimplemented
  device family) does now.

**Verified without a live 3.4 device** (none reachable from this
sandbox) via a full simulated handshake: a fake "device" endpoint
computing its side of the nonce/HMAC exchange independently confirms the
client derives the exact same session key, then a simulated
DP_QUERY_NEW/CONTROL_NEW round trip using that session key decodes
correctly - plus the pre-existing 3.3 send/receive framing tests, rerun
against the rewritten module to confirm the port didn't regress anything
already working. Still the same honest caveat as everywhere else in this
project: simulated crypto/framing correctness is not the same as a
confirmed live device exchange - report back if a real 3.4 device still
doesn't work, this at least narrows it to "the port has a mistake"
rather than "3.4 isn't attempted at all". 3.1's CONTROL signature scheme
remains ported-but-unverified against any real 3.1 device, as before.

## v0.4.1 - fix: ConnectionResetError on a fresh connect to a just-discovered device

Live report: pairing "WiFi Watering Pump 2" (found via v0.4.0's active
scan) failed at `device.connect()` with
`ConnectionResetError: [Errno 104] Connect call failed`. Unlike the
earlier "Connection lost" bugs (all real protocol/framing mistakes, fixed
in v0.2.4-v0.3.2), this is a fresh TCP connect being reset outright by
the remote before any protocol exchange even starts - not a decode/field
bug, a connection-establishment one. Cheap embedded Tuya devices commonly
have a very limited TCP stack and can reject a new connection for a
short cooldown right after a previous one closed - plausible here since
active_scan.py's own identify probe connects+closes a connection to the
SAME device to positively identify it, shortly before the real pairing
connect happens.

Fixed with what's the standard, defensive answer to exactly this class of
flaky embedded-device behavior (not a protocol mistake to "solve" -
there's nothing to decode differently): `TuyaLocalDevice.connect()` now
retries up to 3 times with a short increasing backoff (0.5s, 1.0s) on
`ConnectionResetError`/`OSError`/timeout before giving up. `active_scan.py`'s
own probe connect uses `retries=1` (a quick fail-fast check against a
possibly-wrong host, not the real pairing connection) and now waits
0.3s after closing each probe before moving on, reducing the chance of
tripping this in the first place. Verified with a direct test: first
connect attempt raises `ConnectionResetError`, second succeeds, `connect()`
transparently retries and returns normally.

## v0.4.0 - active LAN scan fallback (for devices that don't broadcast)

Requested after confirming specific device categories (relay-based
heaters, an irrigation valve) still weren't discovered even with the
persistent passive listener (v0.3.0) running correctly. Real, structural
gap distinct from every protocol bug fixed so far: **some simple/cheap
Tuya devices only broadcast for a short window right after boot/network
join and then go quiet**, relying on the controlling app to have cached
their IP - unlike something continuously-reporting like an AC. No amount
of passive listening, however correct, finds a device that has simply
stopped announcing itself. Confirmed the cloud API's own `ip` field is
NOT useful here either - it's the device's public/WAN IP as seen by
Tuya's servers (the home router's internet-facing address, same for every
device behind it), not its LAN-local IP; the cloud has no visibility into
private LAN topology at all.

New `active_scan.py`, mirroring tinytuya's own brute-force fallback:

1. Fast sweep: try opening a TCP connection to port 6668 (the LAN control
   port) against every host in the local /24 - most hosts have nothing
   listening there, so this narrows candidates down cheaply and quickly.
2. For each host that DID have the port open, try to positively identify
   it: a real connect + `status()` attempt using each not-yet-found cloud
   device's own device_id/local_key in turn. Wrong key/host fails to
   decrypt into valid DPS JSON (caught, not a match); the right one
   succeeds - strong enough evidence without brute-forcing the actual
   16-byte key space.

Wired into `account.py` as a separate, much less frequent task
(`ACTIVE_SCAN_INTERVAL`, 30 min - vs. the passive poll's 5 min) since a
full subnet sweep is real network noise and takes real wall-clock time,
unlike reading the always-on passive cache. Runs once at startup too (not
just on its own timer) so a reload gives faster feedback while testing.
A match found this way gets offered as a normal "Discovered" card exactly
like a passive find, just with `version` assumed "3.3" (identified via a
3.3-protocol probe).

Local subnet is guessed from Home Assistant's own outbound-routing IP
(no packets actually sent, the standard UDP-connect trick) assuming a
/24 - correct for the overwhelming majority of home networks; a
non-standard subnet size isn't detected.

Caught and fixed a real bug in this new code during testing: `except
TimeoutError` doesn't catch `asyncio.TimeoutError` on Python <3.11 (they
are distinct classes before that version) - fixed to catch
`asyncio.TimeoutError` explicitly. Verified all three pieces directly: IP
guessing, TCP port-open detection (both true and false cases against a
real local test server), and the subnet sweep itself.

## v0.3.2 - fix: CONTROL command sent an extra, unexpected field

Continued review ("sigue revisando") of `common.py` and `_generate_payload`'s
`payload_dict` in localtuya's reference. Confirmed the `dps_to_request`/
`type_0d` mechanism seen there is a narrower device-family quirk, not
something the default device profile needs (its DP_QUERY template has no
explicit DP list, matching what v0.2.4 already fixed) - not a new gap.

Did find one: **the reference's CONTROL payload template is exactly
`devId`/`uid`/`t`/`dps` - no `gwId`.** `set_dps()` sent `gwId` too, an
extra field the real template doesn't have. This was never verified
against a live device actually accepting a control command - the earlier
DP_QUERY bug (v0.2.4) blocked pairing before any `set_dps()` call was
ever tried for real, so an unverified extra field sitting in the control
payload this whole time is a real possibility for at least part of "puedo
conectar pero no puedo controlar" style symptoms. Now matches the
reference exactly. Verified directly that the payload no longer includes
`gwId` while keeping the three required fields plus `dps`.

## v0.3.1 - fix: LAN control connection never sent a heartbeat, silently degrading the "reactive" design

Continued review ("sigue revisando") into the LAN control protocol's
connection lifecycle after the discovery-side architectural fix (v0.3.0).
Diffed `tuya_lan.py`'s connection handling against localtuya's
`TuyaProtocol.start_heartbeat()`/`heartbeat_loop()` and found a real gap:
**this integration never sent a single HEART_BEAT, ever** - the
`CMD_HEARTBEAT` constant existed but nothing in the codebase used it.

Real Tuya devices commonly drop an idle TCP connection after a short
timeout if they don't see a periodic heartbeat. Reconnecting lazily on
the next `status()`/`set_dps()` call still works (this integration
already does that), but every idle-then-dropped gap means missing
whatever unsolicited push updates would have arrived on the now-closed
connection in the meantime - directly undermining this project's stated
"reactive, not polling" design (`coordinator.py`'s whole premise) any
time a device's own idle timeout is shorter than the coordinator's
`scan_interval` (30s default). This wouldn't show up as an error, just as
occasionally-stale state and delayed reactions to real device changes -
plausibly part of what's still felt "not fully working" after the more
obviously-broken bugs (parsing, payload fields, discovery) were fixed.

Fixed to match the reference: a background loop sends `HEART_BEAT` every
`HEARTBEAT_INTERVAL` (10s, same value localtuya uses) for as long as the
connection is open, closing the connection (letting the existing lazy
reconnect handle the rest) if a heartbeat ever fails/times out - same
failure behavior as the reference. `heartbeat()`'s payload is 2 fields
(gwId/devId), NOT DP_QUERY's 4 - verified directly against the reference's
`payload_dict`. Verified with two direct tests: the payload shape is
exactly right, and the loop genuinely fires repeatedly over real elapsed
time (not just structurally present but never actually running).

## v0.3.0 - persistent discovery listener (architectural fix, not another protocol bug)

Requested explicitly ("revisa el código entero de localtuya") after
v0.2.9's port/framing fixes still didn't resolve a live "not discovering"
report. Reviewing localtuya's `__init__.py` end to end (not just
`discovery.py`, which had already been diffed twice) found the real gap:
**localtuya starts exactly ONE persistent broadcast listener at
integration setup and keeps it open for the entire HA session**,
continuously accumulating whatever it hears into a live cache - it does
NOT open a fresh listener for a few seconds each time it needs an answer.

This integration's `discover_devices()` did the opposite: an ephemeral
`DISCOVERY_TIMEOUT` (8s) listen-and-close window, invoked fresh on every
5-minute poll. A device broadcasting on a longer or irregular interval -
or one that simply doesn't happen to transmit inside whichever few-second
window a particular poll opened - could be missed by every single
scheduled poll indefinitely, with completely correct decoding but simply
never listening at the right moment. This was a real, independent gap
from the protocol-level bugs fixed in v0.2.2-v0.2.9 (all real, none of
them sufficient on their own).

Fixed to match localtuya's architecture:

- New `discovery.PersistentDiscovery`: binds all three ports once, keeps
  the sockets open, and accumulates every device it ever hears into a
  live `devices` dict - never closed until Home Assistant itself shuts
  down.
- New `async_setup()` in `__init__.py` - called ONCE per HA startup,
  before any ConfigEntry - starts this listener and stores it at
  `hass.data[DOMAIN][DISCOVERY_DATA_KEY]`, closing it on
  `EVENT_HOMEASSISTANT_STOP` (mirrors localtuya's own shutdown handling).
- `account.py`'s poller now reads directly from this always-on cache
  instead of opening a fresh short-window listener every cycle.
- `config_flow.py`'s per-device fallback (when a discovered device's IP
  wasn't in the poller's last snapshot) now checks this same cache first
  - instant, no extra listening - before falling back to a one-off short
  listen as a last resort.
- The original `discover_devices()` one-shot function is kept only as a
  defensive fallback for the (shouldn't-normally-happen) case where the
  persistent listener isn't present.

Verified with a direct test: bind all 3 ports, simulate a real broadcast
packet arriving on the running listener, confirm it lands in the live
cache, then close cleanly.

## v0.2.9 - fix: missing third discovery port (7000), only listened on 2 of 3

The user's own insight ("antes hablaba con dispositivos... hay dos partes
del protocolo, solo has implementado uno") pointed at the real gap after
v0.2.8's diff against localtuya found nothing wrong: **there's a THIRD
Tuya broadcast port, 7000 ("Tuya app" port), never listened on at all**.
Confirmed against tinytuya's `scanner.py`/`core/const.py`
(`UDPPORTAPP = 7000`) - newer/app-paired devices commonly broadcast here
instead of, or in addition to, 6666/6667. Devices that only ever showed
up on 7000 were invisible to this integration's discovery no matter how
correct the 6666/6667 handling was.

While adding it, also generalized how a packet's format is decoded:
**which framing a broadcast uses is determined by a prefix INSIDE the
packet, not by which port it arrived on** (confirmed against both
localtuya and tinytuya - tinytuya's own decoder is portless for exactly
this reason). `_decode_broadcast()` now checks the prefix explicitly:

- `0x000055AA` (the classic frame, same one `tuya_lan.py` uses): decoded
  with the SAME retcode-aware logic as that module's v0.2.7 fix - this
  bug applied here too and is fixed the same way.
- `0x00006699`: a newer, HMAC-based frame used by protocol 3.4+ devices'
  broadcasts. Genuinely NOT implemented (matches the control protocol's
  existing, explicit 3.4/3.5 scope gap) - now detected and logged at
  debug level instead of silently vanishing into the same catch-all as a
  malformed packet, so a report about this specific gap is diagnosable
  rather than indistinguishable from "nothing received at all".
- anything else: last-resort fallback, decrypt the whole raw datagram
  directly (matches tinytuya's own fallback path for legacy shapes).

Verified with direct decode tests: an encrypted 0x55AA broadcast, a
plaintext-JSON 0x55AA broadcast, and a 0x6699 broadcast (confirmed
recognized and cleanly skipped, not crashing/misparsed as garbage).

## v0.2.8 - bit-packed DP support (display light/buzzer), discovery diagnostics

- **New: `DPMapping.bit` for bit-packed DPs.** Some Tuya devices (this
  AC included) pack several unrelated booleans into ONE hex-encoded
  multi-byte string DP instead of giving each its own (Tuya's own field
  description for this AC's `boolCode`, dp 123: byte 0 bit3 = display
  light, bit4 = buzzer, among others). `platform: switch`/`binary_sensor`
  entries can now set `bit: N` (a flat bit index across the byte array)
  instead of treating the DP as a plain bool. Writing does a real
  read-modify-write against the DP's CURRENT raw value so unrelated bits
  in the same field are never clobbered - verified with a direct test:
  toggling one bit on, then another, then the first back off, correctly
  leaves the untouched bit alone throughout. `tuya_ac_basic.yaml` now
  exposes "Display light" and "Buzzer" as two real switches from this
  mechanism, documented as a worked example for adding more (eco mode,
  health mode, etc. are the same bitfield, just unmapped).
- **Discovery diagnostics.** After a report that devices known to be on
  the LAN weren't appearing as "Discovered" cards, re-diffed
  `discovery.py` against localtuya's real implementation line-by-line -
  found no discrepancy this time (framing, key, and binding all already
  matched). Since the mechanism itself checks out, added debug logging to
  `account.py`'s poller (cloud device count, LAN-found count + IDs,
  already-configured/ignored count, and a specific skip reason per
  device) so the next report has real data instead of more guessing -
  the two most likely explanations that AREN'T a code bug are (a) the
  device already has a real or ignored ConfigEntry from earlier testing
  this session, or (b) Home Assistant running in a container without
  host networking (Docker bridge mode) never receives LAN broadcast
  traffic at all - a very common, well-known gotcha for any broadcast-
  based discovery (mDNS/SSDP/Tuya alike), not specific to this integration.

## v0.2.7 - fix: FUNDAMENTAL receive-parsing bug, every device reply was corrupt

The real root cause behind "no puedo setear la temperatura" and "sigue
sin consultar el estado real" persisting after v0.2.3-v0.2.6's fixes -
this one is more foundational than any of those.

**Every message the device sends back (DP_QUERY replies AND unsolicited
push updates alike) carries a 4-byte `retcode` field between the header
and the encrypted payload - present only on what the DEVICE sends, not on
what we send.** `_try_parse()` used the same 16-byte, retcode-less layout
for parsing INCOMING frames as for our own outgoing ones, so every decrypt
attempt started 4 bytes too early (into the retcode, not the ciphertext)
and ran 4 bytes too long. This was silently caught by `_listen()`'s broad
except-and-skip and treated as an unparseable frame.

Net effect: `status()` never raised or timed out (the sequence number
still matched what we sent, so the waiting future still resolved) - it
just always resolved to an empty dps dict, forever. No error, nothing to
report - exactly the symptom described ("todo aparece vacío" with no
visible error). This affected BOTH directions of the "bilateral
communication" the user asked for: reading current DP state (`status()`)
AND the replies to `set_dps()` commands - independent of, and more
fundamental than, the DP_QUERY payload-field fix (v0.2.4) or the
coordinator merge fix (v0.2.5), which were both real but couldn't matter
if the bytes being parsed were wrong to begin with.

Found by diffing against localtuya's real `unpack_message()` (same
technique that already caught the v0.2.2/v0.2.4 bugs). Verified with a
full round-trip test simulating a realistically-shaped device reply
(header + retcode + encrypted payload + crc + suffix, exactly as a real
device sends it) through the fixed parser - decrypts correctly now.

## v0.2.6 - fix: enum options showing as bare digits ("0"/"1"/"2"...)

Reported as "swing mode incomplete, shows only numbers" - the AC's
`up_down_sweep` DP has 4 real states (`0`-`3`, per Tuya's own schema
description: none/up-down/up-only/down-only), but no semantic NAME
anywhere in the normalized schema this integration reads, so `_humanize()`
- a no-op on a purely numeric string - left them as bare "0"/"1"/"2"/"3"
in the swing-mode dropdown. New `_label_for_enum_value()` labels a
numeric-only raw value as "<Field name> Position N" instead (e.g. "Up
Down Sweep Position 1") - still never invents what a position actually
MEANS (no guessing "0 = Off"), just makes each option identifiable
instead of an ambiguous bare digit. Applied everywhere an enum map is
built: climate fan/preset/swing, vacuum fan speed, and the generic
select/sensor fallback.

Verified live against the Tuya Cloud API that `up_down_sweep` genuinely
has 4 options, not 3 - a device already paired before this fix (or before
v0.2.0-v0.2.5's other schema fixes) keeps whatever profile was generated
at pairing time; re-adding or editing its profile via Options is needed
to pick up everything fixed this session at once.

## v0.2.5 - fix: heat_cool setpoint invisible, coordinator wiping known DP values

Two more from live testing on the AC:

- **"No puedo setear la temperatura" - HA requires `target_temperature_low`/
  `target_temperature_high` (and `ClimateEntityFeature.TARGET_TEMPERATURE_RANGE`)
  instead of a plain `target_temperature` whenever the entity's current
  `hvac_mode` is `HEAT_COOL`** (this AC's "Auto" mode, since `auto` maps to
  `heat_cool` per the Matter-standard convention this project already
  follows). Only declaring plain `TARGET_TEMPERATURE` meant the setpoint
  control didn't render/work at all while in Auto - same real HA
  requirement already solved in Climate Orchestrator's dual-setpoint
  zones, applied here. Since this device has only ONE physical setpoint
  DP regardless of mode, both `target_temperature_low`/`_high` mirror the
  same value/DP - an honest simplification (documented in code), not a
  real independent range.
- **"No se consultan los valores actuales" - `_async_update_data()` was
  REPLACING `self.data` wholesale with each poll's `status()` result.**
  Real Tuya devices aren't guaranteed to include every DP in every
  DP_QUERY reply (some report a subset, or an initial near-empty ack
  before real values arrive as separate push frames). Every periodic poll
  could silently wipe out previously-known DP values the fresh reply
  didn't happen to repeat. Now merges onto existing data instead of
  replacing, exactly like the push-handler path already (correctly) did -
  plus added debug logging of each raw DP_QUERY reply for future
  diagnosis without guessing.

## v0.2.4 - fix: "Connection lost" on real device, missing 3.3 control header, duplicate °F control

From an AC pairing attempt: "Could not reach device on LAN: Connection
lost". Diffed `tuya_lan.py` against localtuya's real
`pytuya/__init__.py` (same technique that already caught the discovery.py
bugs in v0.2.2) and found two more real, independent protocol bugs:

- **DP_QUERY payload was missing two required fields.** The reference's
  payload template for DP_QUERY (status query, the very first thing
  tried against any device) is FOUR fields - `gwId`, `devId`, `uid`, `t`
  (timestamp) - this only ever sent two (`gwId`, `devId`). A real AC
  closed the TCP connection outright on the incomplete request, matching
  the reported "Connection lost" exactly. Fixed to send all four.
- **Protocol 3.3's required 15-byte version header was missing
  entirely**, both directions. 3.3 prepends `b"3.3" + 12 zero bytes` in
  PLAINTEXT to the ciphertext of most commands (CONTROL included) - but
  NOT DP_QUERY/HEART_BEAT. This integration never added it on send (every
  `set_dps()` control command - turning something on/off, changing a
  setpoint - would have been malformed on a real 3.3 device) and didn't
  strip it on receive either (an incoming frame carrying it would fail to
  decrypt). Both fixed, verified with direct payload-building tests
  (encrypt+header-prepend on send, header-strip+decrypt round-trip on
  receive) since a live 3.3 device isn't available in this sandbox.
- Corrected the module docstring, which had claimed 3.1 was equivalently
  supported - it isn't: 3.1's CONTROL command uses a completely different
  mechanism (MD5-hexdigest signature, not the plain header 3.3 uses),
  still not implemented. Only 3.3 has been checked end-to-end against a
  real device's actual DP_QUERY failure/fix cycle.

Also, a second report from the same AC: the auto-generated profile still
listed a redundant Fahrenheit setpoint control (`temp_set_f`) alongside
the climate entity's Celsius `target_temperature` - same physical value,
two disconnected controls. `build_profile_from_schema` now hides any
`_f`-suffixed DP whose Celsius twin was already consumed into a
composite entity (climate/light/vacuum), with an explicit warning saying
so - nothing is silently dropped, and a device where the twin ISN'T
already exposed elsewhere still gets it as a normal `dps:` entry.

## v0.2.3 - fix: LAN status query crashed every device, plus sharper AC warning

Two more issues from the first real end-to-end pairing attempt (an
"arenero"/litter box, first device to ever actually reach the LAN status
call in this project):

- **Critical, blocked every device**: `TuyaLocalDevice.status()` pre-built
  its payload into bytes via `_build_payload()`, then passed those bytes
  into `_send_receive()`, which calls `_build_payload()` on it AGAIN
  internally (it expects a plain dict, exactly like `set_dps()` already
  correctly passes). `json.dumps()` on an already-bytes object raised
  `TypeError: Object of type bytes is not JSON serializable`, surfaced to
  the user as "Could not reach device on LAN: ...". This hit the coordinator's
  very first refresh for EVERY device, not just this one - no device could
  ever have connected until this was fixed. Verified with a direct
  payload-building test (bypassing the real socket) that the path no
  longer raises.
- The AC's implausible `target_temp_max: 88.0` (known Tuya cloud metadata
  bug, see v0.2.0/README) was still slipping past unnoticed in the
  auto-generated YAML - the generic boilerplate warning at the top wasn't
  enough for the user to catch it before saving. `build_profile_from_schema`
  now returns `(profile, warnings)`; a climate setpoint max above a sane
  50°C ceiling gets a warning naming the exact field/DP/value, prepended
  right above the generated YAML instead of buried in generic text. Still
  never auto-corrects the number - only makes it impossible to miss.
- **Same AC report also flagged missing functions** (fan speed, and
  everything else beyond the 6 basic DPs): root cause was
  `get_device_schema()` only falling back to the v2.0 endpoint when v1.1
  failed outright - but v1.1 "succeeded" for this AC with a genuinely
  PARTIAL schema (6 of the device's real ~30 DPs), so v2.0 was never even
  tried. Fixed to always query and merge BOTH endpoints (v1.1 wins on a
  genuine dp_id conflict, v2.0 fills the rest) - verified live: the same
  AC now returns all 30 DPs. `ClimateMapping` gained `swing_dp`/`swing_map`
  to go with the already-existing `fan_dp`/`preset_dp` (reused from the
  heater's High/Low work) - fan speed (`windspeed`), sleep mode (`sleep`,
  as a preset), and swing (`up_down_sweep`) are now auto-detected straight
  into the climate entity itself, not left as disconnected loose entities.
  Everything else the AC exposes (energy counters, air quality, dirty
  filter, the second swing axis...) now correctly shows up as generic
  auto-typed sensor/select/number/switch entities instead of silently
  vanishing when v1.1's partial response was all that got read.

## v0.2.2 - fix: encrypted broadcast (port 6667) decryption was completely broken

Found by diffing against localtuya's actual `discovery.py` (`master`
branch), at the user's request, after the "not seen on LAN" report
persisted post-v0.1.1 (which only fixed the port-bind conflict, not this):

- **Wrong AES key, two stacked bugs on the same constant.** The real key
  for decrypting port 6667's encrypted broadcast is `MD5(b"yGAdlopoPVldABfn")`
  - a derived 16-byte digest. This module used the 16-character seed
  string's raw bytes directly AS the key (missing the MD5 step
  entirely), AND that seed string had a one-character typo ("PVLd"
  instead of the correct "PVld"). Net effect: every encrypted-broadcast
  packet failed to decrypt, silently (caught by the existing broad
  except-and-skip in `datagram_received`) - a device broadcasting only on
  6667 (common) was never discovered, full stop, independent of the port-
  bind fix. Verified with a round-trip encrypt/decrypt test against the
  corrected key.
- Also simplified port binding to match localtuya's proven approach:
  `asyncio.create_datagram_endpoint(local_addr=..., reuse_port=True)`
  directly, replacing the v0.1.1 manual-socket-with-SO_REUSEADDR approach.
  `reuse_port=True` already sets SO_REUSEPORT before bind - same result,
  less custom code, and now matches how the thing most likely to already
  hold this port (localtuya itself) actually behaves.

## v0.2.1 - fix: don't offer devices not actually seen on the LAN

- `account.py`'s poller was offering every device the CLOUD knows about as
  a "Discovered" card, even ones never seen on this local network (`found
  is None` was still passed through with `ip: None`, relying on the
  `discovery_ip` fallback step to catch it later). Fixed to `continue`
  (skip entirely) when the LAN broadcast pass didn't see the device -
  discovery now means "present on this network right now", matching what
  HomeKit/Tapo-style discovery actually implies, not "exists somewhere on
  your Tuya account". A cloud-known device that isn't on this LAN can
  still be added via the "manual" (no-cloud) flow with a hand-entered IP.
- The `discovery_ip` step in config_flow.py stays as a narrow safety net
  (device goes offline between the poll and the user clicking Configure a
  moment later), no longer the routine path.

## v0.2.0 - account-based discovery (Configure/Ignore UX, like HomeKit/Tapo)

**Config flow rearchitected**, user-requested after the port-conflict
report exposed how clunky the old "pick one device from a list, per +Add
integration click" wizard was:

- First "+ Add integration" now sets up an **account** entry (Tuya Cloud
  credentials only, no device) instead of walking through a device pick
  immediately.
- The account entry runs a background poller (`account.py`,
  `DISCOVERY_POLL_INTERVAL=300s`, plus one pass right at startup) that
  cross-references the account's full cloud device list against LAN
  broadcast + already-configured/ignored devices, and triggers a native HA
  discovery flow (`SOURCE_INTEGRATION_DISCOVERY`) for every genuinely new
  one - shown on the Integrations page as a "Discovered" card with
  Configure/Ignore buttons, same pattern as HomeKit Controller/Tapo/Hue.
  "Ignore" needs no custom code - HA's `ConfigFlow` base class handles it
  generically for any discovery-sourced flow.
- Clicking "Configure" resumes straight into the same profile-review step
  as before (auto-detected from the device's real DP schema, always
  editable) - no functional change there, just how you get to it.
- Dedup relies on `async_set_unique_id()`'s own built-in
  already-in-progress abort - polling every 5 minutes does not spam
  duplicate cards for a device the user hasn't acted on yet.
- New `discovery_ip` step: if a cloud-known device isn't currently seen on
  the LAN broadcast (offline, different VLAN, whatever), the discovery
  flow now asks for a static IP instead of just failing - previously this
  was a hard error with no recovery path in the flow itself.
- New guard: a discovered device broadcasting protocol 3.4/3.5 now aborts
  cleanly (`unsupported_protocol_version`) instead of silently creating a
  ConfigEntry that's guaranteed to fail at setup (protocol 3.4/3.5 isn't
  implemented - see known limitations).
- The "manual" (no-cloud, single device) path is unchanged and still
  available from the initial menu, for devices you don't want to route
  through cloud discovery.
- Also fixed while touching this: LAN broadcast's own reported protocol
  version (`3.1`/`3.3`) is now actually used for discovered devices
  instead of always defaulting to `3.3`.

## v0.1.1 - first live bug report, discovery port conflict

- **Fixed**: `discover_devices()` failed to bind UDP ports 6666/6667
  ("Address already in use", errno 98) on any host that already has
  another Tuya listener running (LocalTuya, the official Tuya integration,
  a previous instance of this one) - devices were never found via LAN
  broadcast, every time, not intermittently. Root cause:
  `asyncio.create_datagram_endpoint(local_addr=...)` doesn't set
  `SO_REUSEADDR`/`SO_REUSEPORT` before binding. Fixed by building the
  socket manually with both set (where the platform supports
  `SO_REUSEPORT`) before `bind()`, then handing that socket to
  `create_datagram_endpoint(sock=...)`. Verified locally by simulating the
  exact conflict (two competing binds to the same port). Caveat
  documented in `discovery.py`: on Linux, `SO_REUSEPORT` only works if
  *every* process sharing the port sets it - if the other Tuya integration
  doesn't, the bind can still fail; manual IP entry remains the fallback.
- First confirmed report from an actual live Home Assistant install -
  this is the first change made in response to real runtime behavior
  rather than review-only.

## v0.1.0 - initial scaffold

- Custom component `tuya_orchestrator`, one ConfigEntry per device.
- Config flow: Tuya Cloud login (local_key extraction only, official
  documented OpenAPI + HMAC-SHA256 signing) or fully manual entry, LAN
  broadcast discovery to resolve current IP, then a profile step (built-in
  YAML profile or paste a custom one).
- LAN protocol client implemented directly (no third-party Tuya SDK):
  packet framing + AES-128-ECB, protocol versions **3.1 and 3.3 only**.
  **3.4/3.5 (HMAC session-key handshake) not implemented yet** - raises a
  clear `NotImplementedError` rather than failing silently.
- Declarative device profiles (`profile.py`): YAML maps DP id -> HA entity
  (switch/sensor/number/binary_sensor/select) with scale/invert/map
  transforms. No templating engine, no cross-DP formulas - deliberately
  simple and inspectable, matching the project's no-black-box philosophy.
- Reactive updates: the LAN socket delivers unsolicited DP-change pushes,
  the coordinator applies them instantly; periodic polling
  (`scan_interval`, default 30s) is only a slow fallback/reconnect check.
- Two generic example profiles (single switch/plug, plug with energy
  monitoring) plus **10 profiles built from a real Tuya account's actual
  device schemas** (18 devices, 7 categories: irrigation, plugs w/wo energy
  monitoring, AC, 2 heater models, RGBCW lights, litter box, 2 vacuum
  models) - see README's coverage table.
- `light` platform added (on/off + brightness + color temperature, no RGB
  yet) - not in the original scaffold, added once real device data showed
  5 of 18 devices are RGBCW lights.
- 2 heater devices + 1 vacuum model had no schema available via the
  standard Tuya Cloud `specification`/`functions`/`status` endpoints,
  recovered via the newer v2.0 "Thing Data Model" endpoint
  (`/v2.0/cloud/thing/{id}/model`) instead - documented in README as the
  fallback to try for any future device that hits the same wall.

- **Composite entity platforms added: `climate`, `vacuum`, and full color
  on `light`.** A device whose DPs form a recognizable shape (power +
  setpoint + current temp -> thermostat; power + brightness/color ->
  bulb; start + battery/status -> vacuum) now gets ONE proper `climate.*`/
  `vacuum.*`/`light.*` entity instead of a pile of disconnected
  switch/number/select entities for the same handful of DPs - a real
  thermostat/vacuum card, not four separate controls. Light color support
  (HS via the JSON `colour_data_v2`-style DP, with a `work_mode` switch
  between white/colour) verified against a real device's live values, not
  guessed.
- **Automatic profile generation (`auto_profile.py`) - the core fix for
  "DP ids differ per device, don't hardcode them".** At pairing time,
  after fetching the local_key, the config flow also fetches the device's
  real DP schema (`TuyaCloudApi.get_device_schema`: semantic `code` +
  numeric `dp_id` + type, per DP - normalized from whichever cloud
  endpoint actually served it, v1.1 or the v2.0 fallback) and builds the
  profile by matching each `code` to a known role, gated by the device's
  own `category` to avoid cross-category ambiguity. Anything unrecognized
  is still auto-typed generically from its Tuya type
  (Boolean->switch/binary_sensor, Integer->number/sensor,
  Enum->select/sensor...). No product_id-keyed hardcoding required for a
  new device to work - validated end-to-end against 8 real devices across
  all 7 categories in this account, correctly detecting all 3 composite
  shapes plus generic fallback for plugs/litter box. The 12 hand-authored
  `profiles/*.yaml` remain available as a manual fallback/reference (also
  used for devices onboarded without cloud access), no longer the primary
  mechanism.
- The auto-generated profile is always shown editable in the config flow
  before the entry is created, with an explicit warning header - Tuya's
  own cloud metadata can be wrong (real example hit during testing: an
  AC's declared max temperature was 88°C, clearly copy-pasted from its
  Fahrenheit DP's range) and this integration does not attempt to
  auto-correct such values, only to map DP structure correctly.

**Known limitations, not yet done:**
- Protocol 3.4/3.5 devices (most Tuya devices manufactured since ~2022)
  are not supported yet.
- `cover`/`humidifier`/`fan` composite platforms not implemented yet - add
  when a real device profile needs them (same pattern as
  `climate`/`vacuum`/`light`).
- Light RGB color's LOCAL (LAN) wire format is unverified - the cloud API
  returns the color DP as a JSON-encoded string, some device firmware may
  send the local equivalent as a nested object instead; the decoder
  accepts both defensively but this hasn't been confirmed against a live
  LAN session.
- Tuya Cloud's v1.1 `specifications` endpoint can return `success: true`
  with a genuinely INCOMPLETE schema (observed on the S1099 vacuum: fewer
  DPs than the same device's v2.0 "Thing Data Model" response) - `success`
  is not a completeness guarantee. `get_device_schema` doesn't currently
  merge both sources, only tries v2.0 as a fallback when v1.1 raises an
  error outright; a device with a silently-partial v1.1 response won't
  get the extra v2.0 DPs. Worth revisiting if profiles keep coming out
  thinner than expected for a given device.
- **Never tested against a live Home Assistant instance or a real Tuya
  device** - this is a from-scratch scaffold, verified only by
  `py_compile`/manual review against documented HA and Tuya APIs, plus
  the DP-schema-to-profile pipeline end-to-end against real Tuya Cloud
  responses (not a live LAN device). Please report any runtime error with
  the exact traceback.
