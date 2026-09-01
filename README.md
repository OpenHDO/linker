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

The repository contains the production Python boundary for a vendor-specific
Wi-Fi RGB driver. It deliberately does not encode a device protocol until the
lamp model and protocol are known. See [ADR-0001](ADR-0001-python-driver-boundary.md)
and the [driver adapter boundary](src/openhdo_linker/drivers/README.md).

The message envelope follows the current `server/contracts/v1` rules: version
`1`, UUID `id`, ISO-8601 UTC `ts`, `source`, and object `payload`. JSON-line
transport is a responsibility of the hosting process; `Envelope.to_json()` and
`Envelope.from_json()` provide the line payload.

Run the contract test:

```powershell
cmake -S . -B build
ctest --test-dir build -C Debug --output-on-failure
```

See the [project architecture](https://github.com/OpenHDO/about/blob/main/ARCHITECTURE.md)
and [server Linker contracts](https://github.com/OpenHDO/server/tree/master/contracts/v1).
