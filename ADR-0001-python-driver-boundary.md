# ADR-0001: Python driver boundary

## Decision

The first implementation is a dependency-free Python boundary. A concrete
vendor-specific Wi-Fi adapter is added behind `VendorRgbDriver` only after the
lamp model and protocol are confirmed.

The boundary owns:

- typed discovery configuration and opaque credentials;
- `DeviceDescriptor`, `LightState`, and RGB value validation;
- polling and event subscription hooks;
- v1 OpenHDO envelopes: `link.register`, `link.state`, `command`, and
  `command.result`;
- command correlation through `correlation_id`;
- duplicate-safe handling through an injectable `CommandJournal`;
- reconnect with bounded exponential backoff and health visibility.

The concrete adapter owns network I/O, authentication details, protocol
encoding/decoding, discovery, and vendor-specific retry/acknowledgement rules.
No unconfirmed device protocol is encoded in the core.

## Consequences

The package can be exercised without network access while keeping the runtime
boundary identical to the eventual driver. The default journal is process
local; deployments that need duplicate protection across restarts must inject a
durable implementation.
