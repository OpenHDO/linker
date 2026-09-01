# Concrete driver adapters

Place vendor-specific network code under this directory. Each adapter owns its
protocol encoding, discovery, credentials, state polling or event subscription,
and command acknowledgement. It must implement `VendorRgbDriver` and record
the source provenance of any adapted reference in its own documentation.

The core boundary must remain independent of vendor SDKs and protocol details.
