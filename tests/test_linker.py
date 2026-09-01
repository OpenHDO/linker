from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from openhdo_linker import (  # noqa: E402
    Credentials,
    DeviceDescriptor,
    Envelope,
    LightState,
    LinkerBoundary,
    LinkerConfig,
    Rgb,
    RuntimeConfig,
    RuntimeConfigError,
    TuyaConfigurationError,
    TuyaDeviceConfig,
    TuyaDpMapping,
    TuyaLocalDriver,
)


def env(**overrides: str) -> dict[str, str]:
    values = {
        "OPENHDO_SERVER": "ws://127.0.0.1:8000",
        "OPENHDO_LINKER_ID": "openhdo.linker.rgb",
        "OPENHDO_LIGHT_ID": "living-room-lamp",
        "OPENHDO_TUYA_IP": "192.168.1.20",
        "OPENHDO_TUYA_DEVICE_ID": "tuya-device-1",
        "OPENHDO_TUYA_LOCAL_KEY": "0123456789abcdef",
        "OPENHDO_TUYA_PROTOCOL": "3.3",
        "OPENHDO_TUYA_DP_POWER": "1",
        "OPENHDO_TUYA_DP_BRIGHTNESS": "2",
        "OPENHDO_TUYA_DP_COLOR": "5",
        "OPENHDO_TUYA_COLOR_FORMAT": "rgb_hex",
        "OPENHDO_TUYA_BRIGHTNESS_MIN": "10",
        "OPENHDO_TUYA_BRIGHTNESS_MAX": "1000",
    }
    values.update(overrides)
    return values


class ConfigTests(unittest.TestCase):
    def test_env_builds_real_device_config_and_exact_websocket_path(self) -> None:
        config = RuntimeConfig.from_env(environ=env())
        self.assertEqual(config.websocket_url, "ws://127.0.0.1:8000/api/v1/linkers/openhdo.linker.rgb")
        self.assertEqual(config.device.device_id, "tuya-device-1")
        self.assertEqual(config.device.dps.brightness_max, 1000)
        self.assertNotIn("0123456789abcdef", repr(config))

    def test_local_key_and_all_dp_mapping_are_required(self) -> None:
        missing_key = env()
        del missing_key["OPENHDO_TUYA_LOCAL_KEY"]
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env(environ=missing_key)
        missing_dp = env()
        del missing_dp["OPENHDO_TUYA_DP_COLOR"]
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env(environ=missing_dp)

    def test_server_token_is_optional_locally_but_required_for_non_local_wss(self) -> None:
        local = RuntimeConfig.from_env(environ=env(OPENHDO_SERVER_TOKEN="server-secret"))
        self.assertEqual(local.server_api_token, "server-secret")
        self.assertNotIn("server-secret", repr(local))
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env(environ=env(OPENHDO_SERVER="ws://192.168.1.5:8000"))
        remote = RuntimeConfig.from_env(environ=env(OPENHDO_SERVER="wss://server.example", OPENHDO_SERVER_TOKEN="server-secret"))
        self.assertEqual(remote.websocket_url, "wss://server.example/api/v1/linkers/openhdo.linker.rgb")

    def test_tuya_mapping_rejects_implicit_or_invalid_values(self) -> None:
        with self.assertRaises(TuyaConfigurationError):
            TuyaDpMapping(1, 1, 5, "rgb_hex", 0, 255)
        with self.assertRaises(TuyaConfigurationError):
            TuyaDpMapping(1, 2, 5, "unknown", 0, 255)


class EnvelopeMappingTests(unittest.TestCase):
    def test_register_contains_only_abstract_light_capability(self) -> None:
        descriptor = DeviceDescriptor("living-room-lamp", "LED lamp", ("RGB",))
        payload = descriptor.to_payload()
        self.assertEqual(payload["id"], "living-room-lamp")
        self.assertEqual(payload["capabilities"][0]["kind"], "light")
        self.assertEqual(payload["capabilities"][0]["brightness"], {"min": 0, "max": 255})
        self.assertNotIn("local_key", payload)
        self.assertNotIn("protocol", payload["capabilities"][0])
        self.assertNotIn("dp_mapping", payload["capabilities"][0])

    def test_reported_state_uses_canonical_light_v1_names(self) -> None:
        state = LightState(
            "tuya-device-1", True, True, Rgb(255, 96, 32), 255,
            datetime.now(timezone.utc), state_revision=41,
        )
        self.assertEqual(
            state.to_payload(light_id="living-room-lamp"),
            {
                "light_id": "living-room-lamp",
                "power": True,
                "brightness": 255,
                "rgb_color": {"r": 255, "g": 96, "b": 32},
                "state_revision": 41,
            },
        )

    def test_command_envelope_preserves_required_correlation_metadata(self) -> None:
        command_id = UUID("00000000-0000-4000-8000-000000000103")
        envelope = Envelope.from_dict({
            "v": 1,
            "id": "00000000-0000-4000-8000-000000000101",
            "type": "light.command.power",
            "ts": "2026-01-01T00:00:00Z",
            "source": "openhdo-server",
            "correlation_id": "00000000-0000-4000-8000-000000000102",
            "payload": {
                "light_id": "living-room-lamp",
                "command_id": str(command_id),
                "idempotency_key": "living-room-lamp-power-001",
                "power": True,
            },
        })
        self.assertEqual(envelope.type, "light.command.power")
        self.assertEqual(envelope.payload["command_id"], str(command_id))
        self.assertIsNotNone(envelope.correlation_id)

    def test_invalid_command_is_rejected_with_all_correlation_metadata(self) -> None:
        async def check() -> None:
            dps = TuyaDpMapping(1, 2, 5, "rgb_hex", 10, 1000)
            device = TuyaDeviceConfig("192.168.1.20", "tuya-device-1", "0123456789abcdef", "3.3", dps)
            driver = TuyaLocalDriver(device)
            boundary = LinkerBoundary(
                LinkerConfig(id="openhdo.linker.rgb"),
                Credentials(),
                driver,
                light_id="living-room-lamp",
                device_id=device.device_id,
                descriptor=driver.descriptor("living-room-lamp"),
            )
            command = Envelope(
                type="light.command.brightness",
                source="openhdo-server",
                correlation_id=UUID(int=2),
                payload={
                    "light_id": "living-room-lamp",
                    "command_id": str(UUID(int=3)),
                    "idempotency_key": "cmd-3",
                    "brightness": 256,
                },
            )
            result = await boundary.handle(command)
            self.assertEqual(result.type, "command.result")
            self.assertEqual(result.correlation_id, command.id)
            self.assertEqual(result.payload["status"], "rejected")
            self.assertEqual(result.payload["light_id"], "living-room-lamp")
            self.assertEqual(result.payload["command_id"], str(UUID(int=3)))
            self.assertEqual(result.payload["idempotency_key"], "cmd-3")
            self.assertIn("error", result.payload)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
