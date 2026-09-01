from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from openhdo_linker import (  # noqa: E402
    ConnectionSupervisor,
    Credentials,
    DeviceDescriptor,
    DriverHealth,
    Envelope,
    JsonLineEndpoint,
    LinkerBoundary,
    LinkerConfig,
    LightState,
    Rgb,
    VendorRgbDriver,
)
from openhdo_linker.driver import ReconnectPolicy  # noqa: E402


class ProbeDriver:
    def __init__(self) -> None:
        self.turn_on_calls: list[tuple[str, Rgb, UUID]] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def discover(self, config, credentials):
        return (DeviceDescriptor("lamp-1", "RGB lamp"),)

    async def connect(self):
        self.connect_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1

    async def poll_state(self, device_id):
        return LightState(device_id, True, False)

    async def subscribe_state(self, device_id, callback):
        async def unsubscribe():
            return None

        return unsubscribe

    async def turn_on(self, device_id, rgb, command_id):
        self.turn_on_calls.append((device_id, rgb, command_id))
        return LightState(device_id, True, True, rgb)

    async def turn_off(self, device_id, command_id):
        return LightState(device_id, True, False)

    async def health(self):
        return DriverHealth(True)


class ReconnectProbe(ProbeDriver):
    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls == 1:
            raise OSError("link unavailable")


class LinkerContractTests(unittest.TestCase):
    def setUp(self):
        self.driver = ProbeDriver()
        self.boundary = LinkerBoundary(
            LinkerConfig(), Credentials({"token": "kept-out-of-payload"}), self.driver
        )

    def test_register_and_state_use_v1_envelope(self):
        register = self.boundary.register()
        self.assertEqual(register.type, "link.register")
        self.assertEqual(register.version, 1)
        self.assertEqual(register.payload["transports"], ["wifi"])
        state = self.boundary.state(LightState("lamp-1", True, True, Rgb(1, 2, 3)))
        decoded = Envelope.from_json(state.to_json())
        self.assertEqual(decoded.payload["rgb"], {"r": 1, "g": 2, "b": 3})

    def test_command_is_correlated_and_duplicate_safe(self):
        command_id = uuid4()
        command = Envelope(
            type="command",
            source="server",
            id=command_id,
            timestamp=datetime.now(timezone.utc),
            payload={"device_id": "lamp-1", "action": "light.turn_on", "rgb": {"r": 10, "g": 20, "b": 30}},
        )
        first = asyncio.run(self.boundary.handle(command))
        second = asyncio.run(self.boundary.handle(command))
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.type, "command.result")
        self.assertEqual(first.correlation_id, command_id)
        self.assertEqual(len(self.driver.turn_on_calls), 1)
        self.assertEqual(self.driver.turn_on_calls[0][1], Rgb(10, 20, 30))

    def test_unknown_protocol_version_is_rejected(self):
        with self.assertRaises(ValueError):
            Envelope.from_dict({"v": 2})

    def test_json_line_endpoint_returns_result_line(self):
        command = Envelope(
            type="command",
            source="server",
            payload={"device_id": "lamp-1", "action": "light.turn_off"},
        )
        result = asyncio.run(JsonLineEndpoint(self.boundary).handle(command.to_json()))
        self.assertEqual(Envelope.from_json(result).type, "command.result")

    def test_reconnects_after_transient_connect_failure(self):
        driver = ReconnectProbe()
        supervisor = ConnectionSupervisor(driver, ReconnectPolicy(0.001, 0.001))

        async def run():
            stop = asyncio.Event()

            async def connected():
                stop.set()

            await supervisor.run(stop, connected)

        asyncio.run(run())
        self.assertEqual(driver.connect_calls, 2)
        self.assertEqual(driver.disconnect_calls, 1)
        self.assertIsNone(supervisor.last_error)


if __name__ == "__main__":
    unittest.main()
