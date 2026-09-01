from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from openhdo_linker import (  # noqa: E402
    DeviceDescriptor,
    Rgb,
    TuyaConfigurationError,
    TuyaDeviceConfig,
    TuyaDpMapping,
    TuyaProtocolError,
    parse_dps,
    state_from_dps,
)
from openhdo_linker.tuya import (  # noqa: E402
    COMMAND_CONTROL,
    _frame_bytes,
    _payload_for_command,
    decode_payload,
    parse_frame,
)


def mapping(**overrides: object) -> TuyaDpMapping:
    values: dict[str, object] = {
        "power": 1,
        "brightness": 2,
        "color": 5,
        "color_format": "rgb_hex",
        "brightness_min": 10,
        "brightness_max": 1000,
    }
    values.update(overrides)
    return TuyaDpMapping(**values)


class TuyaMappingTests(unittest.TestCase):
    def test_brightness_maps_between_abstract_and_configured_range(self) -> None:
        dps = mapping()
        self.assertEqual(dps.brightness_to_dp(0), 10)
        self.assertEqual(dps.brightness_to_dp(255), 1000)
        self.assertEqual(dps.brightness_from_dp(10), 0)
        self.assertEqual(dps.brightness_from_dp(1000), 255)

    def test_optional_white_channel_maps_only_when_configured(self) -> None:
        dps = mapping(white=6, white_min=0, white_max=1000)
        self.assertEqual(dps.white_to_dp(255), 1000)
        self.assertEqual(dps.white_from_dp(0), 0)

    def test_rgb_hex_mapping_is_explicit(self) -> None:
        dps = mapping()
        self.assertEqual(dps.encode_color(Rgb(1, 2, 255)), "0102ff")
        self.assertEqual(dps.decode_color("0102ff"), Rgb(1, 2, 255))

    def test_hsv_mapping_is_round_trippable(self) -> None:
        dps = mapping(color_format="hsv_hex")
        self.assertEqual(dps.decode_color(dps.encode_color(Rgb(255, 0, 0))), Rgb(255, 0, 0))

    def test_dps_parser_accepts_direct_and_nested_shapes(self) -> None:
        self.assertEqual(parse_dps(b'{"dps":{"1":true,"2":500}}'), {1: True, 2: 500})
        self.assertEqual(parse_dps({"data": {"dps": {"5": "0102ff"}}}), {5: "0102ff"})

    def test_encrypted_frame_round_trip_is_pure_and_authenticated(self) -> None:
        key = b"0123456789abcdef"
        payload = _payload_for_command("3.3", COMMAND_CONTROL, {"dps": {"1": True}}, key, None)
        frame = parse_frame(
            _frame_bytes(7, COMMAND_CONTROL, payload, protocol_version="3.3", integrity_key=key),
            protocol_version="3.3",
            integrity_key=key,
        )
        self.assertEqual(decode_payload(frame, protocol_version="3.3", key=key), b'{"dps":{"1":true}}')

    def test_protocol_34_encrypts_version_header_and_uses_hmac(self) -> None:
        key = b"0123456789abcdef"
        payload = _payload_for_command("3.4", COMMAND_CONTROL, {"dps": {"1": True}}, key, None)
        frame = parse_frame(
            _frame_bytes(7, COMMAND_CONTROL, payload, protocol_version="3.4", integrity_key=key),
            protocol_version="3.4",
            integrity_key=key,
        )
        self.assertEqual(decode_payload(frame, protocol_version="3.4", key=key), b'{"dps":{"1":true}}')

    def test_state_mapping_is_abstract_and_uses_observation_time(self) -> None:
        observed = datetime.now(timezone.utc)
        state = state_from_dps("device-1", {1: True, 2: 1000, 5: "0102ff"}, mapping(), observed_at=observed)
        self.assertEqual(state.device_id, "device-1")
        self.assertTrue(state.on)
        self.assertEqual(state.brightness, 255)
        self.assertEqual(state.rgb, Rgb(1, 2, 255))
        self.assertEqual(state.observed_at, observed)

    def test_rgbw_state_exposes_only_abstract_white_value(self) -> None:
        observed = datetime.now(timezone.utc)
        state = state_from_dps(
            "device-1",
            {1: True, 2: 1000, 5: "0102ff", 6: 500},
            mapping(white=6, white_min=0, white_max=1000),
            observed_at=observed,
        )
        self.assertEqual(state.white, 128)
        self.assertEqual(state.to_payload()["white"], 128)


class TuyaValidationTests(unittest.TestCase):
    def test_mapping_rejects_implicit_or_invalid_configuration(self) -> None:
        with self.assertRaises(TuyaConfigurationError):
            mapping(power=1, brightness=1)
        with self.assertRaises(TuyaConfigurationError):
            mapping(color_format="unknown")

    def test_device_config_requires_real_credentials_and_supported_protocol(self) -> None:
        with self.assertRaises(TuyaConfigurationError):
            TuyaDeviceConfig("192.168.1.20", "device-1", "not-a-key", "3.3", mapping())
        with self.assertRaises(TuyaConfigurationError):
            TuyaDeviceConfig("192.168.1.20", "device-1", "0123456789abcdef", "3.5", mapping())

    def test_state_mapping_rejects_missing_or_malformed_configured_dps(self) -> None:
        with self.assertRaises(TuyaProtocolError):
            state_from_dps("device-1", {2: 500, 5: "0102ff"}, mapping())
        with self.assertRaises(TuyaProtocolError):
            state_from_dps("device-1", {1: "on", 2: 500, 5: "0102ff"}, mapping())

    def test_public_descriptor_contains_only_abstract_light_metadata(self) -> None:
        descriptor = DeviceDescriptor("device-1", "LED lamp")
        payload = descriptor.to_payload()
        self.assertEqual(payload["capabilities"], ["light", "rgb", "brightness"])
        self.assertEqual(payload["ranges"], {"brightness": {"min": 0, "max": 255}})
        self.assertNotIn("local_key", payload)
        self.assertNotIn("dps", payload)


if __name__ == "__main__":
    unittest.main()
