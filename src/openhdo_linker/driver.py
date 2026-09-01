"""Typed driver contract and reconnect supervisor.

No vendor protocol is assumed here. A concrete adapter owns discovery,
credentials, encoding, decoding, and the actual network implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .config import Credentials, DiscoveryConfig
from .models import DeviceDescriptor, DriverHealth, LightState, Rgb

StateCallback = Callable[[LightState], Awaitable[None] | None]
Unsubscribe = Callable[[], Awaitable[None] | None]


class VendorRgbDriver(Protocol):
    """The only runtime contract a vendor-specific Wi-Fi adapter must provide."""

    async def discover(self, config: DiscoveryConfig, credentials: Credentials) -> Sequence[DeviceDescriptor]: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def poll_state(self, device_id: str) -> LightState: ...

    async def subscribe_state(self, device_id: str, callback: StateCallback) -> Unsubscribe: ...

    async def turn_on(self, device_id: str, rgb: Rgb, command_id: UUID) -> LightState: ...

    async def turn_off(self, device_id: str, command_id: UUID) -> LightState: ...

    async def health(self) -> DriverHealth: ...


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_s: float = 1.0
    maximum_s: float = 30.0


class ConnectionSupervisor:
    """Reconnects a driver with bounded exponential backoff until stopped."""

    def __init__(self, driver: VendorRgbDriver, policy: ReconnectPolicy) -> None:
        self._driver = driver
        self._policy = policy
        self._last_error: str | None = None
        self._attempts = 0

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def reconnect_attempts(self) -> int:
        return self._attempts

    async def run(self, stop: asyncio.Event, on_connected: Callable[[], Awaitable[None]]) -> None:
        delay = self._policy.initial_s
        while not stop.is_set():
            try:
                await self._driver.connect()
                self._last_error = None
                self._attempts = 0
                await on_connected()
                await stop.wait()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_error = str(error)
                self._attempts += 1
                try:
                    await self._driver.disconnect()
                finally:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                delay = min(delay * 2, self._policy.maximum_s)
