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

The repository contains the production Python boundary and a native local
Tuya-compatible Wi-Fi driver. All vendor/model-specific details stay inside
the Linker. Server-facing descriptors, states, and commands are abstract
`light` values only; credentials and DP mappings never cross that boundary.

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

Install the runtime dependency and run the read-only smoke command against a
real device (replace every angle-bracket value with an actual value; do not
use a sample or fake device):

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
validation, and health reporting. It uses no Home Assistant runtime, gateway,
simulator, mock, or cloud service.

The message envelope follows the current `server/contracts/v1` rules: version
`1`, UUID `id`, ISO-8601 UTC `ts`, `source`, and object `payload`. JSON-line
transport is a responsibility of the hosting process; `Envelope.to_json()` and
`Envelope.from_json()` provide the line payload.

Run the pure validation/mapping tests:

```powershell
cmake -S . -B build
ctest --test-dir build -C Debug --output-on-failure
```

See the [project architecture](https://github.com/OpenHDO/about/blob/main/ARCHITECTURE.md)
and [server Linker contracts](https://github.com/OpenHDO/server/tree/master/contracts/v1).
