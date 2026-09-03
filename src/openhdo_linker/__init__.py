"""Production boundary for vendor-specific OpenHDO Linker drivers."""

from .boundary import LinkerBoundary, MemoryCommandJournal
from .config import Credentials, DiscoveryConfig, LinkerConfig
from .driver import ConnectionSupervisor, VendorRgbDriver
from .models import DeviceDescriptor, DiscoveryCandidate, DriverHealth, LightState, Rgb
from .protocol import Envelope, ProtocolError
from .runtime_config import RuntimeConfig, RuntimeConfigError
from .server_client import LinkerServerClient
from .tuya import (
    TuyaConfigurationError,
    TuyaDeviceConfig,
    TuyaDiscoveryOptions,
    TuyaDpMapping,
    TuyaLocalDriver,
    TuyaPairingConfig,
    TuyaProtocolError,
    parse_dps,
    state_from_dps,
)

__all__ = [
    "ConnectionSupervisor",
    "Credentials",
    "DeviceDescriptor",
    "DiscoveryCandidate",
    "DiscoveryConfig",
    "DriverHealth",
    "Envelope",
    "LinkerBoundary",
    "LinkerConfig",
    "LightState",
    "MemoryCommandJournal",
    "ProtocolError",
    "Rgb",
    "RuntimeConfig",
    "RuntimeConfigError",
    "LinkerServerClient",
    "VendorRgbDriver",
    "TuyaConfigurationError",
    "TuyaDeviceConfig",
    "TuyaDiscoveryOptions",
    "TuyaDpMapping",
    "TuyaLocalDriver",
    "TuyaPairingConfig",
    "TuyaProtocolError",
    "parse_dps",
    "state_from_dps",
]
