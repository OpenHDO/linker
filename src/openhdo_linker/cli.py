"""Command-line process entrypoint for the real OpenHDO Tuya Linker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .boundary import LinkerBoundary
from .config import Credentials
from .runtime_config import RuntimeConfig, RuntimeConfigError
from .server_client import LinkerServerClient
from .tuya import TuyaLocalDriver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real OpenHDO Tuya-compatible Linker")
    parser.add_argument("--config", help="JSON config file; environment variables override it")
    parser.add_argument("--validate", action="store_true", help="validate config and exit without connecting")
    return parser


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
