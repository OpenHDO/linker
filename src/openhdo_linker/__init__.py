"""Production boundary for vendor-specific OpenHDO Linker drivers."""

from .boundary import LinkerBoundary, MemoryCommandJournal
from .config import Credentials, DiscoveryConfig, LinkerConfig
from .driver import ConnectionSupervisor, VendorRgbDriver
from .endpoint import JsonLineEndpoint
from .models import DeviceDescriptor, DriverHealth, LightState, Rgb
from .protocol import Envelope, ProtocolError

__all__ = [
    "ConnectionSupervisor",
    "Credentials",
    "DeviceDescriptor",
    "DiscoveryConfig",
    "DriverHealth",
    "Envelope",
    "LinkerBoundary",
    "LinkerConfig",
    "LightState",
    "MemoryCommandJournal",
    "JsonLineEndpoint",
    "ProtocolError",
    "Rgb",
    "VendorRgbDriver",
]
