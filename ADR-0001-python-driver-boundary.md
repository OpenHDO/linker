# ADR-0001: Python driver boundary

## Decision

The Python boundary remains vendor-neutral, while a concrete native local
Tuya-compatible Wi-Fi adapter is implemented behind `VendorRgbDriver`. It
requires operator-supplied device credentials, protocol version, and DP
mapping; it does not claim that the Sirius LED Smart C37 is Tuya-compatible.

The boundary owns:

- typed discovery configuration and opaque credentials;
- `DeviceDescriptor`, `LightState`, and RGB value validation;
- polling and event subscription hooks;
  - v1 OpenHDO envelopes: `link.register`, `light.state.reported`, the
  `light.command.*` messages, and `command.result`;
- command correlation through `correlation_id`;
- duplicate-safe handling through an injectable `CommandJournal`;
- reconnect with bounded exponential backoff and health visibility.

The concrete adapter owns network I/O, authentication details, protocol
encoding/decoding, discovery, and vendor-specific retry/acknowledgement rules.
No vendor-specific data is encoded in the server-facing core. The adapter owns
local Tuya framing, AES/HMAC transport, optional UDP discovery, and the
translation from configured DPs to abstract light capabilities, ranges, and
state. The process connects directly to the Python server runtime over its
WebSocket linker endpoint.

## Consequences

Pure mapping, parsing, and validation tests run without network access. The
real-device smoke command is intentionally separate and requires all actual
onboarding inputs. The default journal is process local; deployments that need
duplicate protection across restarts must inject a durable implementation.
