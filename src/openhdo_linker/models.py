"""Small typed values shared by the boundary and concrete drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Rgb:
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 0 <= value <= 255 for value in (self.r, self.g, self.b)):
            raise ValueError("RGB channels must be integers from 0 to 255")

    @classmethod
    def from_payload(cls, value: Any) -> "Rgb":
        if not isinstance(value, Mapping):
            raise ValueError("rgb must be an object")
        try:
            return cls(value["r"], value["g"], value["b"])
        except KeyError as error:
            raise ValueError("rgb requires r, g, and b") from error

    def to_payload(self) -> dict[str, int]:
        return {"r": self.r, "g": self.g, "b": self.b}


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    id: str
    name: str
    color_modes: tuple[str, ...] = ("RGB",)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^[a-z][a-z0-9._-]{1,63}$", self.id):
            raise ValueError("light id must be a lowercase OpenHDO identifier")
        if not self.name or len(self.name) > 128:
            raise ValueError("device descriptor fields are required")
        if not self.color_modes or any(mode not in {"RGB", "RGBW", "CCT"} for mode in self.color_modes):
            raise ValueError("color_modes must contain supported Light modes")
        if len(set(self.color_modes)) != len(self.color_modes):
            raise ValueError("color_modes must be unique")

    def to_payload(self) -> dict[str, Any]:
        capability: dict[str, Any] = {
            "kind": "light",
            "power": True,
            "brightness": {"min": 0, "max": 255},
        }
        if self.color_modes:
            capability["color_modes"] = list(self.color_modes)
        if {"RGB", "RGBW"} & set(self.color_modes):
            capability["rgb_channel_range"] = {"min": 0, "max": 255}
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": [capability],
        }


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """The vendor-neutral candidate sent to the server after LAN discovery."""

    candidate_id: str
    name: str
    capabilities: tuple[Mapping[str, Any], ...]
    transport: str = "wifi"
    requires_pairing: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^[a-z][a-z0-9._-]{1,63}$", self.candidate_id):
            raise ValueError("candidate_id must be a lowercase OpenHDO identifier")
        if not self.name or len(self.name) > 128:
            raise ValueError("candidate name must contain 1 to 128 characters")
        if self.transport != "wifi":
            raise ValueError("discovery transport must be wifi")
        if not self.capabilities:
            raise ValueError("discovery candidates require at least one capability")
        if type(self.requires_pairing) is not bool:
            raise ValueError("requires_pairing must be boolean")

    @classmethod
    def from_descriptor(cls, descriptor: DeviceDescriptor) -> "DiscoveryCandidate":
        payload = descriptor.to_payload()
        capabilities = payload["capabilities"]
        if not isinstance(capabilities, list):
            raise ValueError("device descriptor capabilities must be a list")
        return cls(descriptor.id, descriptor.name, tuple(capabilities))

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "transport": self.transport,
            "capabilities": [dict(capability) for capability in self.capabilities],
            "requires_pairing": self.requires_pairing,
        }


@dataclass(frozen=True, slots=True)
class LightState:
    device_id: str
    available: bool
    on: bool
    rgb: Rgb | None = None
    brightness: int | None = None
    observed_at: datetime | None = None
    white: int | None = None
    state_revision: int = 0

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("device_id is required")
        if self.brightness is not None and (type(self.brightness) is not int or not 0 <= self.brightness <= 255):
            raise ValueError("brightness must be an integer from 0 to 255")
        if self.white is not None and (type(self.white) is not int or not 0 <= self.white <= 255):
            raise ValueError("white must be an integer from 0 to 255")
        if type(self.state_revision) is not int or self.state_revision < 0:
            raise ValueError("state_revision must be a non-negative integer")

    def to_payload(self, *, light_id: str | None = None) -> dict[str, Any]:
        if self.brightness is None or self.rgb is None:
            raise ValueError("reported Light state requires brightness and rgb")
        result: dict[str, Any] = {
            "light_id": light_id or self.device_id,
            "power": self.on,
            "brightness": self.brightness,
            "rgb_color": self.rgb.to_payload(),
            "state_revision": self.state_revision,
        }
        return result


@dataclass(frozen=True, slots=True)
class DriverHealth:
    connected: bool
    last_error: str | None = None
    checked_at: datetime | None = None
