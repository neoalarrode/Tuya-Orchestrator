# Changelog

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
