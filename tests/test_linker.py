from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from openhdo_linker import (  # noqa: E402
    Credentials,
    DeviceDescriptor,
    DiscoveryCandidate,
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
    TuyaDiscoveryOptions,
    TuyaLocalDriver,
)
from openhdo_linker.cli import _inspection_payload, _parser, _resolve_inspect_local_key  # noqa: E402
from openhdo_linker.boundary import (  # noqa: E402
    DISCOVERY_DRIVER_RETURN_MARGIN_S,
    DISCOVERY_MIN_SCAN_S,
    DISCOVERY_PROTOCOL_MARGIN_S,
)
from openhdo_linker.server_client import LinkerServerClient  # noqa: E402
from openhdo_linker.tuya import (  # noqa: E402
    COMMAND_REQUEST_DEVICE_INFO,
    DISCOVERY_UDP_KEY,
    _aes_encrypt,
    _frame_6699,
    _frame_bytes,
    _parse_6699,
    _parse_discovery_datagram,
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


class DiscoveryDriver:
    def __init__(self, descriptors=(), error: Exception | None = None) -> None:
        self.descriptors = descriptors
        self.error = error
        self.config = None

    async def discover(self, config, credentials):
        self.config = config
        if self.error is not None:
            raise self.error
        return self.descriptors

    async def disconnect(self) -> None:
        return None


class BlockingDiscoveryDriver(DiscoveryDriver):
    async def discover(self, config, credentials):
        await asyncio.Event().wait()
        return ()


class ReturnAfterEffectiveTimeoutDriver(DiscoveryDriver):
    async def discover(self, config, credentials):
        self.config = config
        await asyncio.sleep(config.timeout_s)
        return ()


class WebSocketSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class ConfigTests(unittest.TestCase):
    def test_inspect_cli_requires_real_device_inputs(self) -> None:
        args = _parser().parse_args([
            "inspect", "--ip", "192.168.1.20", "--device-id", "tuya-device-1",
            "--protocol-version", "3.3",
        ])
        self.assertEqual(args.command, "inspect")
        self.assertIsNone(args.local_key)
        self.assertEqual(args.timeout, 3.0)
        explicit = _parser().parse_args([
            "inspect", "--ip", "192.168.1.20", "--device-id", "tuya-device-1",
            "--local-key", "0123456789abcdef", "--protocol-version", "3.3",
        ])
        self.assertEqual(explicit.local_key, "0123456789abcdef")
        with self.assertRaises(SystemExit):
            _parser().parse_args(["inspect", "--ip", "192.168.1.20"])
        with self.assertRaises(SystemExit):
            _parser().parse_args([
                "inspect", "--ip", "192.168.1.20", "--device-id", "tuya-device-1",
                "--local-key", "0123456789abcdef", "--protocol-version", "3.5",
            ])

        discover = _parser().parse_args(["discover", "--timeout", "7"])
        self.assertEqual(discover.command, "discover")
        self.assertEqual(discover.timeout, 7)
        with self.assertRaises(SystemExit):
            _parser().parse_args(["discover", "--timeout", "0"])

    def test_inspect_local_key_uses_environment_without_exposing_it(self) -> None:
        key = _resolve_inspect_local_key(None, {"OPENHDO_TUYA_LOCAL_KEY": "0123456789abcdef"})
        self.assertEqual(key, "0123456789abcdef")
        self.assertEqual(
            _resolve_inspect_local_key("fedcba9876543210", {"OPENHDO_TUYA_LOCAL_KEY": "0123456789abcdef"}),
            "fedcba9876543210",
        )
        for value in (None, ""):
            with self.assertRaisesRegex(RuntimeConfigError, "non-empty"):
                _resolve_inspect_local_key(None, {"OPENHDO_TUYA_LOCAL_KEY": value} if value is not None else {})

    def test_inspect_device_config_rejects_loopback_and_invalid_timeout(self) -> None:
        common = {
            "device_id": "tuya-device-1",
            "local_key": "0123456789abcdef",
            "protocol_version": "3.3",
            "dps": None,
        }
        with self.assertRaises(TuyaConfigurationError):
            TuyaDeviceConfig(ip="127.0.0.1", **common)
        with self.assertRaises(TuyaConfigurationError):
            TuyaDeviceConfig(ip="192.168.1.20", timeout_s=0, **common)

    def test_inspect_output_redacts_credentials_and_reports_dp_types(self) -> None:
        payload = _inspection_payload(
            "192.168.1.20", "tuya-device-1", "3.3",
            {2: 42, 1: True, 3: {"local_key": "0123456789abcdef", "value": "prefix-server-secret-suffix"}},
            sensitive=("0123456789abcdef", "server-secret"),
        )
        rendered = json.dumps(payload)
        self.assertNotIn("0123456789abcdef", rendered)
        self.assertNotIn("server-secret", rendered)
        self.assertEqual(payload["dps"][0], {"index": 1, "type": "bool", "value": True})
        self.assertEqual(payload["dps"][1], {"index": 2, "type": "int", "value": 42})

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

    def test_discovery_only_mode_needs_no_real_device_config(self) -> None:
        config = RuntimeConfig.from_env(environ={
            "OPENHDO_SERVER": "ws://127.0.0.1:8000",
            "OPENHDO_DISCOVERY_ONLY": "true",
        })
        self.assertTrue(config.discovery_only)
        self.assertTrue(config.discovery_enabled)
        self.assertIsNone(config.device)
        self.assertIsNone(config.light_id)
        boundary = LinkerBoundary(config.linker, Credentials(), TuyaLocalDriver(discovery=TuyaDiscoveryOptions(enabled=True)))
        self.assertNotIn("devices", boundary.register().payload)
        self.assertFalse(boundary.control_enabled)

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

    def test_discovery_ports_are_distinct(self) -> None:
        with self.assertRaises(TuyaConfigurationError):
            TuyaDiscoveryOptions(enabled=True, ports=(6666, 6666))


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


class DiscoveryEnvelopeTests(unittest.TestCase):
    def _request(self, *, timeout_s: object = 7, correlation: UUID | None = None) -> Envelope:
        message_id = UUID(int=100)
        return Envelope(
            type="discovery.start",
            source="openhdo-server",
            id=message_id,
            correlation_id=message_id if correlation is None else correlation,
            payload={"session_id": str(UUID(int=101)), "timeout_s": timeout_s},
        )

    def test_discovery_emits_one_sanitized_candidate_and_completion(self) -> None:
        async def check() -> None:
            descriptor = DeviceDescriptor("candidate.light", "Living room", ("RGB",))
            driver = DiscoveryDriver([descriptor])
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), driver)
            messages = await boundary.handle_discovery(self._request())
            self.assertEqual(len(messages), 2)
            candidate, completed = messages
            self.assertEqual(candidate.type, "discovery.candidate")
            self.assertEqual(candidate.correlation_id, UUID(int=100))
            self.assertEqual(
                set(candidate.payload),
                {"session_id", "candidate_id", "name", "transport", "capabilities", "requires_pairing"},
            )
            self.assertEqual(candidate.payload["candidate_id"], "candidate.light")
            self.assertEqual(candidate.payload["transport"], "wifi")
            self.assertTrue(candidate.payload["requires_pairing"])
            self.assertNotIn("ip", candidate.payload)
            self.assertNotIn("local_key", json.dumps(candidate.payload))
            self.assertEqual(completed.type, "discovery.completed")
            self.assertEqual(completed.correlation_id, UUID(int=100))
            self.assertEqual(completed.payload["status"], "completed")
            self.assertIsNone(completed.payload["error"])
            self.assertEqual(driver.config.timeout_s, 7.0 - DISCOVERY_PROTOCOL_MARGIN_S)

        asyncio.run(check())

    def test_discovery_timeout_floor_keeps_nonzero_scan_and_completion(self) -> None:
        async def check() -> None:
            driver = DiscoveryDriver()
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), driver)
            messages = await boundary.handle_discovery(self._request(timeout_s=1))
            self.assertEqual(driver.config.timeout_s, 1.0 - DISCOVERY_PROTOCOL_MARGIN_S)
            self.assertLess(driver.config.timeout_s, 1.0)
            self.assertGreaterEqual(driver.config.timeout_s, DISCOVERY_MIN_SCAN_S)
            self.assertEqual(messages[-1].type, "discovery.completed")
            self.assertEqual(messages[-1].payload["status"], "completed")

        asyncio.run(check())

    def test_discovery_driver_return_margin_keeps_completion_completed(self) -> None:
        async def check() -> None:
            driver = ReturnAfterEffectiveTimeoutDriver()
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), driver)
            messages = await boundary.handle_discovery(self._request(timeout_s=1))
            self.assertEqual(driver.config.timeout_s, 1.0 - DISCOVERY_PROTOCOL_MARGIN_S)
            self.assertEqual(
                messages[-1].type,
                "discovery.completed",
            )
            self.assertEqual(messages[-1].payload["status"], "completed")
            self.assertLess(driver.config.timeout_s + DISCOVERY_DRIVER_RETURN_MARGIN_S, 1.0)

        asyncio.run(check())

    def test_discovery_error_is_failed_without_driver_details(self) -> None:
        async def check() -> None:
            driver = DiscoveryDriver(error=RuntimeError("local_key=0123456789abcdef at 192.168.1.20"))
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials({"local_key": "0123456789abcdef"}), driver)
            messages = await boundary.handle_discovery(self._request())
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].type, "discovery.completed")
            self.assertEqual(messages[0].payload["status"], "failed")
            self.assertEqual(messages[0].payload["error"], "discovery failed")
            self.assertNotIn("0123456789abcdef", json.dumps(messages[0].to_dict()))
            self.assertNotIn("192.168.1.20", json.dumps(messages[0].to_dict()))

        asyncio.run(check())

    def test_server_client_sends_all_discovery_envelopes(self) -> None:
        async def check() -> None:
            driver = DiscoveryDriver([DeviceDescriptor("candidate.light", "LED lamp")])
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), driver)
            client = LinkerServerClient(boundary, "ws://127.0.0.1:8000")
            websocket = WebSocketSink()
            await client._receive(websocket, self._request().to_json())
            self.assertEqual([Envelope.from_json(value).type for value in websocket.messages], [
                "discovery.candidate", "discovery.completed"
            ])

        asyncio.run(check())

    def test_discovery_request_validates_correlation_and_timeout(self) -> None:
        async def check() -> None:
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), DiscoveryDriver())
            for request in (
                self._request(timeout_s=True),
                self._request(timeout_s=61),
                self._request(correlation=UUID(int=999)),
            ):
                with self.assertRaises(ValueError):
                    await boundary.handle_discovery(request)

        asyncio.run(check())

    def test_discovery_cancellation_propagates_for_session_cleanup(self) -> None:
        async def check() -> None:
            boundary = LinkerBoundary(LinkerConfig(id="openhdo.linker.rgb"), Credentials(), BlockingDiscoveryDriver())
            task = asyncio.create_task(boundary.handle_discovery(self._request()))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(check())

    def test_real_tuya_discovery_id_is_opaque_and_abstract(self) -> None:
        descriptor = TuyaLocalDriver._discovery_descriptor("actual-tuya-device-id")
        candidate = DiscoveryCandidate.from_descriptor(descriptor).to_payload()
        self.assertNotIn("actual-tuya-device-id", json.dumps(candidate))
        self.assertTrue(candidate["candidate_id"].startswith("light."))
        self.assertEqual(set(candidate), {"candidate_id", "name", "transport", "capabilities", "requires_pairing"})


class TuyaUdpDiscoveryTests(unittest.TestCase):
    def test_real_udp_formats_verify_integrity_and_require_tuya_identity(self) -> None:
        device_id = "tuya-device-123"
        response = json.dumps({
            "gwId": device_id,
            "version": "3.1",
            "localKey": "0123456789abcdef",
            "productKey": "vendor-detail-stays-local",
        }, separators=(",", ":")).encode()

        plaintext = _parse_discovery_datagram(response, "192.168.1.20")
        self.assertEqual(plaintext, {"id": device_id, "ip": "192.168.1.20"})

        legacy = _frame_bytes(
            1,
            COMMAND_REQUEST_DEVICE_INFO,
            _aes_encrypt(response, DISCOVERY_UDP_KEY),
            protocol_version="3.1",
            integrity_key=b"",
        )
        self.assertEqual(_parse_discovery_datagram(legacy, "192.168.1.20"), plaintext)
        legacy_with_retcode = _frame_bytes(
            1,
            COMMAND_REQUEST_DEVICE_INFO,
            b"\0\0\0\0" + _aes_encrypt(response, DISCOVERY_UDP_KEY),
            protocol_version="3.1",
            integrity_key=b"",
        )
        self.assertEqual(_parse_discovery_datagram(legacy_with_retcode, "192.168.1.20"), plaintext)
        legacy_bad = bytearray(legacy)
        legacy_bad[-5] ^= 1
        self.assertIsNone(_parse_discovery_datagram(bytes(legacy_bad), "192.168.1.20"))

        modern = _frame_6699(2, COMMAND_REQUEST_DEVICE_INFO, response, DISCOVERY_UDP_KEY)
        self.assertEqual(_parse_discovery_datagram(modern, "192.168.1.20"), plaintext)
        modern_bad = bytearray(modern)
        modern_bad[30] ^= 1
        self.assertIsNone(_parse_discovery_datagram(bytes(modern_bad), "192.168.1.20"))

        self.assertIsNone(_parse_discovery_datagram(b'{"id":"arbitrary-json"}', "192.168.1.20"))
        self.assertNotIn("localKey", json.dumps(plaintext))
        self.assertNotIn("productKey", json.dumps(plaintext))

    def test_discovery_uses_fixed_listeners_and_one_authenticated_7000_request(self) -> None:
        class RecordingSocket:
            instances: list["RecordingSocket"] = []

            def __init__(self, *_args: object) -> None:
                self.bind_address = None
                self.options: list[tuple[object, object, object]] = []
                self.sent: list[tuple[bytes, tuple[str, int]]] = []
                self.closed = False
                type(self).instances.append(self)

            def setsockopt(self, level: object, option: object, value: object) -> None:
                self.options.append((level, option, value))

            def bind(self, address: tuple[str, int]) -> None:
                self.bind_address = address

            def setblocking(self, _value: bool) -> None:
                return None

            def sendto(self, data: bytes, address: tuple[str, int]) -> None:
                self.sent.append((data, address))

            def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
                raise BlockingIOError

            def close(self) -> None:
                self.closed = True

        RecordingSocket.instances = []
        driver = TuyaLocalDriver(discovery=TuyaDiscoveryOptions(enabled=True))
        with (
            patch("openhdo_linker.tuya.socket.socket", side_effect=RecordingSocket),
            patch("openhdo_linker.tuya._discovery_local_ip", return_value="192.168.1.10"),
            patch("openhdo_linker.tuya.select.select", return_value=([], [], [])),
            patch("openhdo_linker.tuya.time.monotonic", side_effect=(0.0, 0.0, 0.0, 2.0)),
        ):
            self.assertEqual(driver._discover_sync(1), [])

        listeners = RecordingSocket.instances[:3]
        sender = RecordingSocket.instances[3]
        self.assertEqual([item.bind_address for item in listeners], [("", 6666), ("", 6667), ("", 7000)])
        self.assertEqual(sender.bind_address, ("", 0))
        self.assertEqual(len(sender.sent), 1)
        request, target = sender.sent[0]
        self.assertEqual(target, ("255.255.255.255", 7000))
        frame = _parse_6699(request, DISCOVERY_UDP_KEY)
        self.assertEqual(frame.command, COMMAND_REQUEST_DEVICE_INFO)
        self.assertEqual(json.loads(frame.payload), {"from": "app", "ip": "192.168.1.10"})
        self.assertTrue(all(item.closed for item in RecordingSocket.instances))


if __name__ == "__main__":
    unittest.main()
