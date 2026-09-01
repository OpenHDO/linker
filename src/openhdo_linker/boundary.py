"""OpenHDO v1 server/linker boundary around a concrete Light driver."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from .config import Credentials, LinkerConfig
from .driver import VendorRgbDriver
from .models import DeviceDescriptor, DiscoveryCandidate, LightState, Rgb
from .protocol import Envelope

DISCOVERY_PROTOCOL_MARGIN_S = 0.25
DISCOVERY_DRIVER_RETURN_MARGIN_S = 0.10
DISCOVERY_MIN_SCAN_S = 0.5


class CommandJournal(Protocol):
    async def get(self, idempotency_key: str) -> Mapping[str, object] | None: ...

    async def put(self, idempotency_key: str, result: Mapping[str, object]) -> None: ...


class MemoryCommandJournal:
    """Process-local idempotency journal for one running Linker process."""

    def __init__(self) -> None:
        self._results: dict[str, dict[str, object]] = {}

    async def get(self, idempotency_key: str) -> Mapping[str, object] | None:
        result = self._results.get(idempotency_key)
        return None if result is None else dict(result)

    async def put(self, idempotency_key: str, result: Mapping[str, object]) -> None:
        self._results.setdefault(idempotency_key, dict(result))


class LinkerBoundary:
    """Map canonical v1 Light commands to real driver operations."""

    _COMMAND_TYPES = {
        "light.command.power",
        "light.command.brightness",
        "light.command.rgb_color",
    }

    def __init__(
        self,
        config: LinkerConfig,
        credentials: Credentials,
        driver: VendorRgbDriver,
        *,
        light_id: str | None = None,
        device_id: str | None = None,
        descriptor: DeviceDescriptor | None = None,
        journal: CommandJournal | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.driver = driver
        self.light_id = light_id or (descriptor.id if descriptor is not None else None)
        self.device_id = device_id or self.light_id
        self.descriptor = descriptor
        self.journal = journal or MemoryCommandJournal()
        self._command_lock = asyncio.Lock()

    def register(self) -> Envelope:
        payload: dict[str, object] = {
            "id": self.config.id,
            "version": self.config.version,
            "name": self.config.name,
            "transports": [self.config.transport],
        }
        if self.descriptor is not None:
            payload["devices"] = [self.descriptor.to_payload()]
        return Envelope(type="link.register", source=self.config.id, payload=payload)

    async def discover(self) -> tuple[DeviceDescriptor, ...]:
        return tuple(await self.driver.discover(self.config.discovery, self.credentials))

    @property
    def control_enabled(self) -> bool:
        return self.light_id is not None and self.device_id is not None

    async def handle_discovery(self, start: Envelope) -> tuple[Envelope, ...]:
        session_id, timeout_s = _discovery_request(start)
        # Leave bounded time for candidate envelopes and the completion frame
        # to reach the server before its process-local session deadline.
        effective_timeout_s = max(DISCOVERY_MIN_SCAN_S, timeout_s - DISCOVERY_PROTOCOL_MARGIN_S)
        discovery_config = replace(self.config.discovery, timeout_s=effective_timeout_s)
        try:
            descriptors = await asyncio.wait_for(
                self.driver.discover(discovery_config, self.credentials),
                timeout=effective_timeout_s + DISCOVERY_DRIVER_RETURN_MARGIN_S,
            )
            candidates = tuple(DiscoveryCandidate.from_descriptor(descriptor) for descriptor in descriptors)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return (self._discovery_completed(start, session_id, "failed", "discovery timed out"),)
        except Exception:
            # Discovery errors may contain device addresses or driver credentials.
            return (self._discovery_completed(start, session_id, "failed", "discovery failed"),)

        messages = tuple(
            Envelope(
                type="discovery.candidate",
                source=self.config.id,
                correlation_id=start.id,
                payload={"session_id": str(session_id), **candidate.to_payload()},
            )
            for candidate in candidates
        )
        return messages + (self._discovery_completed(start, session_id, "completed", None),)

    def _discovery_completed(
        self, start: Envelope, session_id: UUID, status: str, error: str | None
    ) -> Envelope:
        return Envelope(
            type="discovery.completed",
            source=self.config.id,
            correlation_id=start.id,
            payload={"session_id": str(session_id), "status": status, "error": error},
        )

    def state(self, state: LightState) -> Envelope:
        return Envelope(
            type="light.state.reported",
            source=self.config.id,
            payload=state.to_payload(light_id=self.light_id),
        )

    async def handle(self, command: Envelope) -> Envelope | tuple[Envelope, ...]:
        if command.type == "discovery.start":
            return await self.handle_discovery(command)
        if command.type not in self._COMMAND_TYPES:
            raise ValueError(f"unsupported message type: {command.type}")
        if command.correlation_id is None:
            raise ValueError("light commands require correlation_id")

        payload = command.payload
        metadata_error: str | None = None
        try:
            light_id = _required_string(payload, "light_id")
        except (KeyError, TypeError, ValueError) as error:
            light_id = self.light_id or "invalid.light"
            metadata_error = str(error)
        try:
            command_id = _required_uuid(payload, "command_id")
        except ValueError as error:
            command_id = command.id
            metadata_error = metadata_error or str(error)
        try:
            idempotency_key = _required_string(payload, "idempotency_key")
        except (KeyError, TypeError, ValueError) as error:
            idempotency_key = f"invalid:{command.id}"
            metadata_error = metadata_error or str(error)
        if metadata_error is not None:
            return self._result(command, self._rejected(light_id, command_id, idempotency_key, "invalid_command", metadata_error))
        if self.light_id is None or light_id != self.light_id:
            return self._result(command, self._rejected(light_id, command_id, idempotency_key, "unknown_light", "light is not registered"))

        # ponytail: one lock serializes commands; use per-key futures only if throughput requires it.
        async with self._command_lock:
            cached = await self.journal.get(idempotency_key)
            if cached is not None:
                if cached.get("command_id") != str(command_id):
                    return self._result(command, self._rejected(light_id, command_id, idempotency_key, "idempotency_conflict", "idempotency key belongs to another command"))
                return self._result(command, cached)
            result = await self._execute(command, light_id, command_id, idempotency_key)
            await self.journal.put(idempotency_key, result.payload)
            return result

    async def _execute(
        self, command: Envelope, light_id: str, command_id: UUID, idempotency_key: str,
    ) -> Envelope:
        try:
            if command.type == "light.command.power":
                power = command.payload.get("power")
                if type(power) is not bool:
                    raise ValueError("power must be boolean")
                operation = lambda: self.driver.turn_on(self.device_id, None, None, None, command_id) if power else self.driver.turn_off(self.device_id, command_id)
            elif command.type == "light.command.brightness":
                brightness = _brightness(command.payload.get("brightness"))
                operation = lambda: self.driver.set_brightness(self.device_id, brightness, command_id)
            else:
                rgb = Rgb.from_payload(command.payload.get("rgb_color"))
                operation = lambda: self.driver.set_rgb(self.device_id, rgb, command_id)
        except (KeyError, TypeError, ValueError) as error:
            payload = self._rejected(light_id, command_id, idempotency_key, "invalid_command", str(error))
            return self._result(command, payload)
        try:
            state = await operation()
            payload = {
                "status": "applied",
                "light_id": light_id,
                "command_id": str(command_id),
                "idempotency_key": idempotency_key,
                "state": state.to_payload(light_id=light_id),
            }
        except Exception:
            # Driver operations only return applied after a real read-after-write confirmation.
            payload = {
                "status": "failed",
                "light_id": light_id,
                "command_id": str(command_id),
                "idempotency_key": idempotency_key,
                "error": {"code": "driver_error", "message": "physical device action was not confirmed"},
            }
        return self._result(command, payload)

    @staticmethod
    def _rejected(light_id: str, command_id: UUID, idempotency_key: str, code: str, message: str) -> dict[str, object]:
        return {
            "status": "rejected",
            "light_id": light_id,
            "command_id": str(command_id),
            "idempotency_key": idempotency_key,
            "error": {"code": code, "message": message},
        }

    def _result(self, command: Envelope, payload: Mapping[str, object]) -> Envelope:
        return Envelope(
            type="command.result",
            source=self.config.id,
            correlation_id=command.id,
            payload=dict(payload),
        )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_uuid(payload: Mapping[str, object], key: str) -> UUID:
    try:
        value = UUID(str(payload[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a UUID") from error
    return value


def _brightness(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("brightness must be an integer from 0 to 255")
    return value


def _discovery_request(start: Envelope) -> tuple[UUID, int]:
    if start.type != "discovery.start":
        raise ValueError("discovery request must use discovery.start")
    if start.correlation_id != start.id:
        raise ValueError("discovery.start correlation_id must equal envelope id")
    unknown = set(start.payload) - {"session_id", "timeout_s"}
    if unknown:
        raise ValueError(f"unknown discovery.start fields: {', '.join(sorted(unknown))}")
    session_id = _required_uuid(start.payload, "session_id")
    timeout = start.payload.get("timeout_s")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("timeout_s must be an integer from 1 to 60")
    return session_id, timeout
