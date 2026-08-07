# Changelog

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
