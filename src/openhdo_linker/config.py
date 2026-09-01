"""Configuration and secret inputs for a concrete vendor driver."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Mapping

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True, slots=True)
class Credentials:
    """Opaque credentials passed to a driver; never included in envelopes."""

    values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.values.items()):
            raise ValueError("credentials must be string key/value pairs")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Driver-neutral discovery limits; vendor fields belong in the driver."""

    scope: str = "local"
    timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.scope or self.timeout_s <= 0:
            raise ValueError("discovery scope and timeout must be usable")


@dataclass(frozen=True, slots=True)
class LinkerConfig:
    id: str = "openhdo.linker.rgb"
    version: str = "0.1.0"
    name: str = "OpenHDO RGB Linker"
    transport: str = "wifi"
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 30.0

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("id must be a lowercase linker identifier")
        if not _SEMVER.fullmatch(self.version):
            raise ValueError("version must use semantic versioning")
        if not self.name or len(self.name) > 128:
            raise ValueError("name must contain 1 to 128 characters")
        if not _IDENTIFIER.fullmatch(self.transport):
            raise ValueError("transport must be a lowercase identifier")
        if self.reconnect_initial_s <= 0 or self.reconnect_max_s < self.reconnect_initial_s:
            raise ValueError("reconnect delays must be positive and ordered")
