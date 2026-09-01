"""Real-LAN, read-only smoke command for one configured local device."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .tuya import TuyaDeviceConfig, TuyaDpMapping, TuyaLocalDriver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll one real local Tuya-compatible device")
    parser.add_argument("--ip", required=True, help="actual device LAN IP address")
    parser.add_argument("--device-id", required=True, help="actual device ID")
    parser.add_argument("--local-key", default=os.environ.get("OPENHDO_TUYA_LOCAL_KEY"), help="actual 16-byte local key; prefer OPENHDO_TUYA_LOCAL_KEY")
    parser.add_argument("--protocol-version", required=True, choices=("3.1", "3.2", "3.3", "3.4"))
    parser.add_argument("--dp-power", required=True, type=int)
    parser.add_argument("--dp-brightness", required=True, type=int)
    parser.add_argument("--dp-color", required=True, type=int)
    parser.add_argument("--color-format", required=True, choices=("rgb_hex", "hsv_hex"))
    parser.add_argument("--brightness-min", required=True, type=int)
    parser.add_argument("--brightness-max", required=True, type=int)
    parser.add_argument("--dp-white", type=int, help="actual white-channel DP for RGBW devices")
    parser.add_argument("--white-min", type=int)
    parser.add_argument("--white-max", type=int)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--public-name", default="LED lamp")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.local_key:
        raise ValueError("provide the real local key with --local-key or OPENHDO_TUYA_LOCAL_KEY")
    if args.dp_white is None and (args.white_min is not None or args.white_max is not None):
        raise ValueError("provide --dp-white, --white-min, and --white-max together")
    if args.dp_white is not None and (args.white_min is None or args.white_max is None):
        raise ValueError("provide --dp-white, --white-min, and --white-max together")
    mapping = TuyaDpMapping(
        power=args.dp_power,
        brightness=args.dp_brightness,
        color=args.dp_color,
        color_format=args.color_format,
        brightness_min=args.brightness_min,
        brightness_max=args.brightness_max,
        white=args.dp_white,
        white_min=args.white_min,
        white_max=args.white_max,
    )
    driver = TuyaLocalDriver(
        TuyaDeviceConfig(
            ip=args.ip,
            device_id=args.device_id,
            local_key=args.local_key,
            protocol_version=args.protocol_version,
            dps=mapping,
            public_name=args.public_name,
            timeout_s=args.timeout,
            retries=args.retries,
        )
    )
    try:
        await driver.connect()
        state = await driver.poll_state(args.device_id)
        health = await driver.health()
        print(json.dumps({"status": "ok", "state": state.to_payload(), "health": {"connected": health.connected, "last_error": health.last_error}}, sort_keys=True))
        return 0
    finally:
        await driver.disconnect()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parser().parse_args(argv)))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
