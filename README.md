# OpenHDO Linker

`openhdo-linker` is the standalone hardware-access process. It runs near the
required radios, USB devices, serial buses, or local network and translates
device-specific protocols into OpenHDO messages.

## Owns

- transport and device drivers;
- discovery, pairing, and local inventory;
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
details stay inside the Linker. Server-facing descriptors, states, and commands
are abstract `light` values only; credentials and DP mappings never cross that
boundary.

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
`TuyaDiscoveryOptions(enabled=True)`, it sends a LAN broadcast and listens on
the Tuya discovery ports to learn address/ID metadata only; it cannot recover a
local key or DP mapping. Discovery results are exposed as abstract `LED lamp`
descriptors with `light`, `rgb`, `brightness`, and brightness range `0..255`;
an explicitly configured white DP adds abstract `white` and its `0..255`
range.

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
