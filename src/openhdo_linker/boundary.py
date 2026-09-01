"""OpenHDO message boundary around a concrete RGB driver."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from .config import Credentials, LinkerConfig
from .driver import VendorRgbDriver
from .models import DeviceDescriptor, LightState, Rgb
from .protocol import Envelope


class CommandJournal(Protocol):
    async def get(self, command_id: UUID) -> Envelope | None: ...

    async def put(self, command_id: UUID, result: Envelope) -> None: ...


class MemoryCommandJournal:
    """Process-local journal; inject durable storage when restart recovery is required."""

    def __init__(self) -> None:
        self._results: dict[UUID, Envelope] = {}

    async def get(self, command_id: UUID) -> Envelope | None:
        return self._results.get(command_id)

    async def put(self, command_id: UUID, result: Envelope) -> None:
        self._results.setdefault(command_id, result)


class LinkerBoundary:
    """Maps v1 envelopes to driver operations and driver states back to envelopes."""

    def __init__(
        self,
        config: LinkerConfig,
        credentials: Credentials,
        driver: VendorRgbDriver,
        journal: CommandJournal | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.driver = driver
        self.journal = journal or MemoryCommandJournal()
        self._command_lock = asyncio.Lock()

    def register(self) -> Envelope:
        return Envelope(
            type="link.register",
            source=self.config.id,
            payload={
                "id": self.config.id,
                "version": self.config.version,
                "name": self.config.name,
                "transports": [self.config.transport],
            },
        )

    async def discover(self) -> tuple[DeviceDescriptor, ...]:
        return tuple(await self.driver.discover(self.config.discovery, self.credentials))

    def state(self, state: LightState) -> Envelope:
        return Envelope(type="link.state", source=self.config.id, payload=state.to_payload())

    async def handle(self, command: Envelope) -> Envelope:
        if command.type != "command":
            raise ValueError(f"unsupported message type: {command.type}")

        # ponytail: one lock serializes commands; use per-command futures if throughput requires it.
        async with self._command_lock:
            cached = await self.journal.get(command.id)
            if cached is not None:
                return cached
            result = await self._execute(command)
            await self.journal.put(command.id, result)
            return result

    async def _execute(self, command: Envelope) -> Envelope:
        try:
            device_id = _required_string(command.payload, "device_id")
            action = _required_string(command.payload, "action")
            if action == "light.turn_on":
                rgb_value = command.payload.get("rgb")
                state = await self.driver.turn_on(
                    device_id,
                    None if rgb_value is None else Rgb.from_payload(rgb_value),
                    _optional_brightness(command.payload.get("brightness")),
                    _optional_brightness(command.payload.get("white")),
                    command.id,
                )
            elif action == "light.turn_off":
                state = await self.driver.turn_off(device_id, command.id)
            elif action == "light.set_brightness":
                brightness = _optional_brightness(command.payload.get("brightness"))
                if brightness is None:
                    raise ValueError("brightness is required")
                state = await self.driver.set_brightness(device_id, brightness, command.id)
            elif action == "light.set_rgb":
                state = await self.driver.set_rgb(device_id, Rgb.from_payload(command.payload.get("rgb")), command.id)
            elif action == "light.set_white":
                white = _optional_brightness(command.payload.get("white"))
                if white is None:
                    raise ValueError("white is required")
                state = await self.driver.set_white(device_id, white, command.id)
            else:
                raise ValueError(f"unsupported action: {action}")
            payload = {"status": "ok", "device_id": device_id, "state": state.to_payload()}
        except (KeyError, TypeError, ValueError) as error:
            payload = {"status": "error", "code": "invalid_command", "message": str(error)}
        except Exception:
            payload = {"status": "error", "code": "driver_error", "message": "driver operation failed"}
        return Envelope(
            type="command.result",
            source=self.config.id,
            correlation_id=command.id,
            payload=payload,
        )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_brightness(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("brightness must be an integer from 0 to 255")
    return value
