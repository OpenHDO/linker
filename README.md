# OpenHDO Linker

`openhdo-linker` is the standalone hardware-access process. It runs near the
required radios, USB devices, serial buses, or local network and translates
device-specific protocols into OpenHDO messages.

## Owns

- transport and device drivers;
- LAN discovery of already-onboarded devices and local inventory;
- local hardware credentials;
- device state and event publication;
- validated command handling and health reporting.

## Does not own

The Linker does not own global orchestration, user policy, dashboard layout,
SQLite state, or flow execution. Those belong to the server. Multiple Linkers
may connect to one server.

## Status

The repository contains the production Python process, WebSocket server client,
and native local Tuya-compatible Wi-Fi driver. All vendor/model-specific
details stay inside the Linker. Server-facing descriptors, discovery candidates,
states, and commands are abstract `light` values only; credentials and DP
mappings never cross that boundary.

The supplied Sirius LED Smart C37 is not vendor-confirmed. Web evidence makes
Tuya/Smart Life plausible, but the driver does not infer that fact or ship a
Sirius mapping. See [ADR-0001](ADR-0001-python-driver-boundary.md), the
[driver adapter boundary](src/openhdo_linker/drivers/README.md), and
[NOTICE](NOTICE).

## Real-device onboarding

Before starting the driver, the operator must obtain and verify these values
for the actual lamp on the same LAN:

- the lamp's current private/link-local IP address;
- the device ID;
- the 16 ASCII-byte local key;
- the local protocol version (`3.1`, `3.2`, `3.3`, or `3.4`);
- the power, brightness, and color DP indexes;
- the brightness DP's device range (both minimum and maximum);
- the color DP encoding (`rgb_hex` or `hsv_hex`).

For RGBW operation, also provide the white-channel DP and its device range.

Those values are model/firmware-specific and must come from the user's
approved Tuya/Smart Life onboarding or device inspection. A missing key or
unconfirmed mapping is a configuration error, not permission to invent a
device. Protocol 3.5 is rejected explicitly until it has a verified native
implementation.

UDP discovery is disabled by default. When explicitly enabled with
`TuyaDiscoveryOptions(enabled=True)` (or
`OPENHDO_TUYA_DISCOVERY_ENABLED=true`), it sends a real Tuya LAN solicitation
to UDP/7000 and listens on the discovery ports. It discovers only devices that
are already connected to Wi-Fi and answer the local UDP protocol. This is not
EZ mode or AP mode Wi-Fi provisioning, pairing, cloud onboarding, or local-key
recovery: the Linker does not create an access point, configure SSIDs, or
promise a provisioning flow. It cannot recover a local key or DP mapping. The
implementation handles the real Tuya plaintext v3.1, legacy CRC/AES UDP, and
6699/AES-GCM discovery envelopes; local control still supports only the
explicitly implemented protocol versions above. The default destination is the
global IPv4 broadcast address, so the LAN and host firewall must allow local
broadcast traffic; no fallback or fake candidate is generated when it does not.
The
WebSocket `discovery.start` request runs this real scan and produces one
`discovery.candidate` for each validated descriptor, followed by
`discovery.completed`. Candidates contain only `candidate_id`, `name`,
`transport: "wifi"`, abstract Light capabilities, and `requires_pairing`; the
real Tuya ID is represented by a stable opaque linker-local identifier. No IP,
local key, vendor/model, or DP value crosses the boundary.

For an explicit LAN-only scan, use the direct CLI command. It does not load
server or device configuration and prints only the same sanitized candidate
shape:

```powershell
openhdo-linker discover --timeout 5
```

The long-running process can run without a configured device in discovery-only
mode. `OPENHDO_DISCOVERY_ONLY=true` requires only `OPENHDO_SERVER` (and linker
identity overrides if needed), registers an identity with no `devices`, stays
connected, and answers `discovery.start`. This mode enables LAN discovery by
default; set `OPENHDO_TUYA_DISCOVERY_ENABLED=false` to disable scans. Normal
control mode still requires the complete real-device and DP mapping listed
above. Discovery-only mode does not add EZ/AP provisioning or pairing.

To inspect the actual DPs before choosing a mapping, query the real device:

```powershell
py -m pip install -e .
$env:OPENHDO_TUYA_LOCAL_KEY = '<actual 16 ASCII-byte local key>'
openhdo-linker inspect `
  --ip '<actual private or link-local IP>' `
  --device-id '<actual device ID>' `
  --protocol-version '<3.1|3.2|3.3|3.4>' `
  --timeout 3
```

`--local-key` is still available as an explicit automation override, but it
puts the secret in process arguments; prefer the environment variable above.
`inspect` performs only a TCP connection and encrypted DP/status query; it
does not send control. Its sanitized JSON helps determine the DP mapping for
the linker configuration. Vendor details found locally by `inspect` never
enter the public OpenHDO server boundary, which receives only abstract light
descriptors and states.

Install the runtime and run the read-only smoke command against a real device
(replace every angle-bracket value with an actual value):

```powershell
py -m pip install -e .
$env:OPENHDO_TUYA_LOCAL_KEY = '<actual 16 ASCII-byte local key>'
py -m openhdo_linker.smoke `
  --ip '<actual LAN IP>' `
  --device-id '<actual device ID>' `
  --protocol-version '<3.1|3.2|3.3|3.4>' `
  --dp-power <actual power DP> `
  --dp-brightness <actual brightness DP> `
  --dp-color <actual color DP> `
  --color-format '<rgb_hex|hsv_hex>' `
  --brightness-min <actual device minimum> `
  --brightness-max <actual device maximum>
```

For an RGBW lamp, append `--dp-white`, `--white-min`, and `--white-max` with
the actual white DP and range.

The smoke command performs real TCP connect, encrypted local poll, response
validation, and health reporting.

## Discovery HTTP contract

The server owns process-local discovery session state. Authenticated clients
start a session with `POST /api/v1/discovery/sessions` and body
`{"linker_id":"<linker_id>","timeout_s":5}`; the server returns `202` with a
`session_id`. `GET /api/v1/discovery/sessions/{session_id}` returns
`session_id`, `linker_id`, `status` (`running`, `completed`, or `failed`),
`candidates`, and `error`. The server sends the corresponding v1
`discovery.start` envelope to the connected Linker and correlates all returned
`discovery.candidate` and `discovery.completed` envelopes to that request.
Timeout, cancellation, disconnect, and scan errors are terminal session
failures; the Linker never reports secret or device-network details in them.
The Linker reserves `DISCOVERY_PROTOCOL_MARGIN_S = 0.25` seconds from the
requested budget and gives the outer wait an additional bounded
`DISCOVERY_DRIVER_RETURN_MARGIN_S = 0.10` seconds for `to_thread` return and
cleanup. The driver receives `max(0.5, timeout_s - 0.25)` and the outer wait is
that value plus `0.10`; for every valid `timeout_s` from 1 through 60, the
outer wait remains strictly below the server budget. At the 1-second lower
bound this means a 0.75-second LAN scan and a 0.85-second outer wait, never a
zero-length scan.

## Run the Linker process

The process connects to the Python server runtime at
`ws://<server>/api/v1/linkers/<linker_id>`, registers one abstract LED Light,
publishes `light.state.reported`, receives the v1 `light.command.*` messages,
and sends `command.result` only after the real device action is confirmed by a
read-after-write poll. Server-facing messages follow
[`server/contracts/v1`](https://github.com/OpenHDO/server/tree/master/contracts/v1).

Configuration may come from JSON with environment-variable overrides. For a
direct environment-based launch, every value below must be replaced with the
actual onboarding value; `OPENHDO_TUYA_LOCAL_KEY` and all DP mapping values are
mandatory:

```powershell
py -m pip install -e .
$env:OPENHDO_SERVER = 'ws://<server>:<port>'
$env:OPENHDO_LINKER_ID = '<linker_id>'
$env:OPENHDO_LIGHT_ID = '<lowercase_light_id>'
$env:OPENHDO_TUYA_IP = '<actual LAN IP>'
$env:OPENHDO_TUYA_DEVICE_ID = '<actual device ID>'
$env:OPENHDO_TUYA_LOCAL_KEY = '<actual 16 ASCII-byte local_key>'
$env:OPENHDO_TUYA_PROTOCOL = '<3.1|3.2|3.3|3.4>'
$env:OPENHDO_TUYA_DP_POWER = '<actual power DP>'
$env:OPENHDO_TUYA_DP_BRIGHTNESS = '<actual brightness DP>'
$env:OPENHDO_TUYA_DP_COLOR = '<actual color DP>'
$env:OPENHDO_TUYA_COLOR_FORMAT = '<rgb_hex|hsv_hex>'
$env:OPENHDO_TUYA_BRIGHTNESS_MIN = '<actual brightness minimum>'
$env:OPENHDO_TUYA_BRIGHTNESS_MAX = '<actual brightness maximum>'
$env:OPENHDO_TUYA_DISCOVERY_ENABLED = 'true' # optional; discovery.start is otherwise disabled
openhdo-linker --validate
openhdo-linker
```

For RGBW, also set `OPENHDO_TUYA_DP_WHITE`, `OPENHDO_TUYA_WHITE_MIN`, and
`OPENHDO_TUYA_WHITE_MAX`. The local key is never included in registration,
state, command, result, or log output.

For a non-local server, use `wss://` and set the optional bearer token as
`OPENHDO_SERVER_TOKEN`; the Linker sends it only as the WebSocket
`Authorization: Bearer` header and never logs it. A JSON config may provide the
same value as `server_api_token` (or `server_config.api_token`).

Run the pure validation/mapping tests:

```powershell
cmake -S . -B build
ctest --test-dir build -C Debug --output-on-failure
```

See the [project architecture](https://github.com/OpenHDO/about/blob/main/ARCHITECTURE.md)
and [server Linker contracts](https://github.com/OpenHDO/server/tree/master/contracts/v1).
