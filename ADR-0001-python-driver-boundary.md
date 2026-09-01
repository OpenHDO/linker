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
- v1 OpenHDO envelopes: `link.register`, `discovery.candidate`,
  `discovery.completed`, `light.state.reported`, the `light.command.*`
  messages, and `command.result`;
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

## Discovery integration

`discovery.start` is accepted only with a UUID `session_id`, an integer
`timeout_s` from 1 through 60, and `correlation_id` equal to the start envelope
ID. The boundary passes that timeout to the concrete driver, emits one
`discovery.candidate` per descriptor, and always emits a correlated
`discovery.completed` for a successful or failed scan. A cancelled scan is
cancelled with the WebSocket session; a timeout or driver failure is converted
to a secret-free failed completion. Candidate IDs are opaque identifiers and
candidate payloads contain only the abstract Light capability plus
`requires_pairing`; local Tuya address, credentials, protocol, model, and DP
mapping remain linker-local. Pairing and cloud discovery are deliberately not
implemented.

The explicit `OPENHDO_DISCOVERY_ONLY=true` runtime mode constructs the driver
without a `TuyaDeviceConfig`, registers the Linker identity with no devices,
and keeps the WebSocket session alive for discovery messages. The default
control mode continues to reject incomplete IP, device ID, local key, protocol,
and DP mapping configuration. The standalone `openhdo-linker discover` command
uses the same real UDP scan and prints only sanitized abstract candidates.

The UDP scan is LAN discovery of Wi-Fi-associated devices that respond to the
Tuya local discovery protocol. It is deliberately separate from EZ/AP Wi-Fi
provisioning, pairing, cloud onboarding, and local-key recovery; none of those
flows are implemented or implied by `discovery.start`.

The boundary reserves `DISCOVERY_PROTOCOL_MARGIN_S = 0.25` seconds for
outbound discovery envelopes. The concrete driver receives
`max(1.0, timeout_s - 0.25)`, so a valid request always retains at least one
second of effective LAN scan time while completion has protocol headroom.

## Consequences

Pure mapping, parsing, and validation tests run without network access. The
real-device smoke command is intentionally separate and requires all actual
onboarding inputs. The default journal is process local; deployments that need
duplicate protection across restarts must inject a durable implementation.
