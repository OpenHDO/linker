"""Small typed values shared by the boundary and concrete drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    capabilities: tuple[str, ...] = ("light", "rgb", "brightness")
    ranges: Mapping[str, Mapping[str, int]] = field(
        default_factory=lambda: {"brightness": {"min": 0, "max": 255}}
    )

    def __post_init__(self) -> None:
        if not self.id or not self.name or not self.capabilities:
            raise ValueError("device descriptor fields are required")
        for capability, limits in self.ranges.items():
            if not isinstance(capability, str) or not isinstance(limits, Mapping):
                raise ValueError("descriptor ranges must be named integer ranges")
            if type(limits.get("min")) is not int or type(limits.get("max")) is not int:
                raise ValueError("descriptor ranges must contain integer min and max")
            if limits["min"] > limits["max"]:
                raise ValueError("descriptor range min must not exceed max")

    def to_payload(self) -> dict[str, Any]:
        return {
            "device_id": self.id,
            "name": self.name,
            "capabilities": list(self.capabilities),
            "ranges": {key: dict(value) for key, value in self.ranges.items()},
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

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("device_id is required")
        if self.brightness is not None and (type(self.brightness) is not int or not 0 <= self.brightness <= 255):
            raise ValueError("brightness must be an integer from 0 to 255")
        if self.white is not None and (type(self.white) is not int or not 0 <= self.white <= 255):
            raise ValueError("white must be an integer from 0 to 255")

    def to_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "device_id": self.device_id,
            "available": self.available,
            "on": self.on,
        }
        if self.rgb is not None:
            result["rgb"] = self.rgb.to_payload()
        if self.brightness is not None:
            result["brightness"] = self.brightness
        if self.white is not None:
            result["white"] = self.white
        return result


@dataclass(frozen=True, slots=True)
class DriverHealth:
    connected: bool
    last_error: str | None = None
    checked_at: datetime | None = None
