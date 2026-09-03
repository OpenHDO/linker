# Concrete driver adapters

Place vendor-specific network code under this directory. Each adapter owns its
protocol encoding, discovery, credentials, state polling or event subscription,
and command acknowledgement. It must implement `VendorRgbDriver` and record
the source provenance of any adapted reference in its own documentation.

The core boundary must remain independent of vendor SDKs and protocol details.

The native local Tuya-compatible implementation is `openhdo_linker.tuya`.
Its configuration and protocol handling are linker-internal. The only values
published toward the server are abstract light descriptors, ranges, states,
and commands; never local keys, vendor names, or DP mappings. Its UDP scan is
limited to discovery of already Wi-Fi-associated devices on the LAN. Pairing
only verifies and adopts a discovered device using Linker-local credentials;
EZ/AP provisioning, cloud onboarding, and local-key recovery remain outside
this process. See the root README for the real-device onboarding inputs and
LAN smoke command.
