from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from urllib.request import urlopen
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from jsonschema import Draft202012Validator, RefResolver  # noqa: E402

from openhdo_linker import DeviceDescriptor, Envelope, LightState, Rgb  # noqa: E402


CONTRACT_ROOT = "https://raw.githubusercontent.com/OpenHDO/server/master/contracts/v1/"


def contract(name: str) -> dict[str, object]:
    with urlopen(CONTRACT_ROOT + name, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise AssertionError(f"server contract {name} must be an object")
    return value


def validate(value: object, schema: dict[str, object], base: str, store: dict[str, object]) -> None:
    resolver = RefResolver(base, schema, store=store)
    Draft202012Validator(schema, resolver=resolver).validate(value)


class ServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.envelope = contract("envelope.schema.json")
        cls.manifest = contract("link-manifest.schema.json")
        cls.capability = contract("light-capability.schema.json")
        cls.light = contract("light.schema.json")
        cls.state = contract("light-state.schema.json")

    def test_register_and_capability_validate_against_server_contracts(self) -> None:
        descriptor = DeviceDescriptor("living-room-lamp", "LED lamp", ("RGB",))
        payload = {
            "id": "openhdo.linker.rgb",
            "version": "0.3.0",
            "name": "OpenHDO Tuya LED Linker",
            "transports": ["wifi"],
            "devices": [descriptor.to_payload()],
        }
        validate(payload["devices"][0]["capabilities"][0], self.capability, CONTRACT_ROOT + "light-capability.schema.json", {})
        validate(payload, self.manifest, CONTRACT_ROOT + "link-manifest.schema.json", {
            CONTRACT_ROOT + "light-capability.schema.json": self.capability,
        })
        validate({
            "v": 1,
            "id": "00000000-0000-4000-8000-000000000120",
            "type": "link.register",
            "ts": "2026-01-01T00:00:05Z",
            "source": "openhdo.linker.rgb",
            "payload": payload,
        }, self.envelope, CONTRACT_ROOT + "envelope.schema.json", {})

    def test_reported_state_has_only_canonical_server_fields(self) -> None:
        state = LightState("tuya-device-1", True, True, Rgb(255, 96, 32), 255, datetime.now(timezone.utc), state_revision=41)
        message = Envelope(type="light.state.reported", source="openhdo.linker.rgb", payload=state.to_payload(light_id="living-room-lamp"))
        validate(message.to_dict(), self.state, CONTRACT_ROOT + "light-state.schema.json", {
            CONTRACT_ROOT + "envelope.schema.json": self.envelope,
            CONTRACT_ROOT + "light.schema.json": self.light,
        })
        self.assertEqual(set(message.payload), {"light_id", "power", "brightness", "rgb_color", "state_revision"})

    def test_command_result_statuses_have_contract_metadata(self) -> None:
        result_schema = {
            "type": "object",
            "required": ["status", "light_id", "command_id", "idempotency_key"],
            "properties": {
                "status": {"enum": ["accepted", "applied", "rejected", "failed"]},
                "light_id": {"type": "string"},
                "command_id": {"type": "string", "format": "uuid"},
                "idempotency_key": {"type": "string", "minLength": 1},
                "state": {
                    "type": "object",
                    "required": ["light_id", "power", "brightness", "rgb_color", "state_revision"],
                    "properties": {
                        "light_id": {"type": "string"},
                        "power": {"type": "boolean"},
                        "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
                        "rgb_color": {"type": "object", "required": ["r", "g", "b"]},
                        "state_revision": {"type": "integer", "minimum": 0},
                    },
                },
                "error": {"type": "object", "required": ["code", "message"]},
            },
            "allOf": [
                {"if": {"properties": {"status": {"const": "applied"}}}, "then": {"required": ["state"], "not": {"required": ["error"]}}},
                {"if": {"properties": {"status": {"enum": ["rejected", "failed"]}}}, "then": {"required": ["error"], "not": {"required": ["state"]}}},
            ],
        }
        state = LightState("tuya-device-1", True, True, Rgb(1, 2, 3), 200, datetime.now(timezone.utc), state_revision=1)
        metadata = {"light_id": "living-room-lamp", "command_id": str(UUID(int=1)), "idempotency_key": "cmd-1"}
        for status, extra in (("applied", {"state": state.to_payload(light_id="living-room-lamp")}), ("rejected", {"error": {"code": "invalid_command", "message": "bad value"}}), ("failed", {"error": {"code": "driver_error", "message": "not confirmed"}})):
            validate({**metadata, "status": status, **extra}, result_schema, CONTRACT_ROOT + "light.schema.json", {})


if __name__ == "__main__":
    unittest.main()
