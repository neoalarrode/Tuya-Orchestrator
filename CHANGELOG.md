# Changelog

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
