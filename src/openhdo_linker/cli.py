"""Command-line process entrypoint for the real OpenHDO Tuya Linker."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
import json
import logging
import signal
import sys

from .boundary import LinkerBoundary
from .config import Credentials
from .runtime_config import RuntimeConfig, RuntimeConfigError
from .server_client import LinkerServerClient
from .tuya import SUPPORTED_PROTOCOLS, TuyaDeviceConfig, TuyaLocalDriver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real OpenHDO Tuya-compatible Linker")
    parser.add_argument("--config", help="JSON config file; environment variables override it")
    parser.add_argument("--validate", action="store_true", help="validate config and exit without connecting")
    commands = parser.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="read DP indexes, types, and values from one real local device")
    inspect.add_argument("--ip", required=True, help="actual private or link-local device IP")
    inspect.add_argument("--device-id", required=True, help="actual device ID")
    inspect.add_argument("--local-key", required=True, help="actual 16 ASCII-byte local key")
    inspect.add_argument("--protocol-version", required=True, choices=SUPPORTED_PROTOCOLS)
    inspect.add_argument("--timeout", type=float, default=3.0, help="TCP/query timeout in seconds (0 < timeout <= 60)")
    return parser


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "array"
    return "unknown"


def _sanitize_value(value: object, sensitive: tuple[str, ...]) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(item, sensitive)
            for key, item in value.items()
            if str(key).lower() not in {"local_key", "server_token", "api_token", "token"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_value(item, sensitive) for item in value]
    if isinstance(value, str):
        for secret in sensitive:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def _inspection_payload(ip: str, device_id: str, protocol_version: str, dps: Mapping[int, object], *, sensitive: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "device_id": _sanitize_value(device_id, sensitive),
        "ip": _sanitize_value(ip, sensitive),
        "protocol_version": _sanitize_value(protocol_version, sensitive),
        "dps": [
            {"index": index, "type": _value_type(value), "value": _sanitize_value(value, sensitive)}
            for index, value in sorted(dps.items())
        ],
    }


async def _inspect(args: argparse.Namespace) -> int:
    device = TuyaDeviceConfig(
        ip=args.ip,
        device_id=args.device_id,
        local_key=args.local_key,
        protocol_version=args.protocol_version,
        timeout_s=args.timeout,
    )
    driver = TuyaLocalDriver(device)
    try:
        await driver.connect()
        dps = await driver.inspect_dps()
        print(json.dumps(_inspection_payload(args.ip, args.device_id, args.protocol_version, dps, sensitive=(args.local_key,)), sort_keys=True))
        return 0
    finally:
        await driver.disconnect()


async def _run(config: RuntimeConfig) -> None:
    driver = TuyaLocalDriver(config.device)
    boundary = LinkerBoundary(
        config.linker,
        Credentials({"local_key": config.device.local_key}),
        driver,
        light_id=config.light_id,
        device_id=config.device.device_id,
        descriptor=driver.descriptor(config.light_id),
    )
    client = LinkerServerClient(
        boundary,
        config.websocket_url,
        server_api_token=config.server_api_token,
        reconnect_initial_s=config.linker.reconnect_initial_s,
        reconnect_max_s=config.linker.reconnect_max_s,
        state_poll_interval_s=config.state_poll_interval_s,
        open_timeout_s=config.server_open_timeout_s,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                loop.add_signal_handler(signum, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    await client.run(stop)
    await driver.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return asyncio.run(_inspect(args))
        config = RuntimeConfig.from_env(args.config)
        if args.validate:
            print(f"configuration valid: server={config.websocket_url} light_id={config.light_id}")
            return 0
        asyncio.run(_run(config))
        return 0
    except (OSError, RuntimeConfigError, ValueError, RuntimeError) as error:
        print(f"openhdo-linker failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
