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

Repository scaffold. The first implementation should be one process with one
reference transport and a reconnecting, authenticated server session.

See the [project architecture](https://github.com/OpenHDO/about/blob/main/ARCHITECTURE.md)
and [server Linker contracts](https://github.com/OpenHDO/server/tree/master/contracts/v1).
