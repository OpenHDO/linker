"""Validated process configuration loaded from JSON and environment variables."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from .config import DiscoveryConfig, LinkerConfig
from .tuya import TuyaDeviceConfig, TuyaDpMapping

_LIGHT_ID = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")


class RuntimeConfigError(ValueError):
    """Raised when process or real-device configuration is incomplete."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    server_url: str
    linker: LinkerConfig
    device: TuyaDeviceConfig = field(repr=False)
    light_id: str
    state_poll_interval_s: float = 15.0
    server_open_timeout_s: float = 10.0
    server_api_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeConfigError("server must be a ws:// or wss:// URL with a host")
        if self.server_api_token is not None and not self.server_api_token.strip():
            raise RuntimeConfigError("server_api_token must not be empty")
        if not _is_local_host(parsed.hostname) and (parsed.scheme != "wss" or self.server_api_token is None):
            raise RuntimeConfigError("non-local servers require wss:// and OPENHDO_SERVER_TOKEN")
        if not _LIGHT_ID.fullmatch(self.light_id):
            raise RuntimeConfigError("light_id must be a lowercase OpenHDO identifier")
        if self.state_poll_interval_s <= 0 or self.server_open_timeout_s <= 0:
            raise RuntimeConfigError("runtime timeouts must be positive")

    @property
    def websocket_url(self) -> str:
        return f"{self.server_url.rstrip('/')}/api/v1/linkers/{quote(self.linker.id, safe='')}"

    @classmethod
    def from_env(cls, config_path: str | os.PathLike[str] | None = None, *, environ: Mapping[str, str] | None = None) -> "RuntimeConfig":
        env = os.environ if environ is None else environ
        path_value = config_path or env.get("OPENHDO_CONFIG")
        raw = _load_json(path_value) if path_value else {}
        if not isinstance(raw, Mapping):
            raise RuntimeConfigError("config file root must be an object")

        linker_raw = _section(raw, "linker")
        server_raw = _section(raw, "server_config")
        tuya_raw = _section(raw, "tuya")
        server = _string(env, "OPENHDO_SERVER", raw, "server", required=True)
        server_api_token = _string(env, "OPENHDO_SERVER_TOKEN", raw, "server_api_token", default=server_raw.get("api_token"))
        linker_id = _string(env, "OPENHDO_LINKER_ID", linker_raw, "id", default="openhdo.linker.rgb")
        linker_version = _string(env, "OPENHDO_LINKER_VERSION", linker_raw, "version", default="0.3.0")
        linker_name = _string(env, "OPENHDO_LINKER_NAME", linker_raw, "name", default="OpenHDO Tuya LED Linker")
        light_id = _string(env, "OPENHDO_LIGHT_ID", tuya_raw, "light_id", default=None)
        device_id = _string(env, "OPENHDO_TUYA_DEVICE_ID", tuya_raw, "device_id", required=True)
        if light_id is None:
            light_id = device_id
        if not isinstance(light_id, str):
            raise RuntimeConfigError("light_id must be a string")

        def number(env_name: str, key: str, *, required: bool = False, default: int | None = None) -> int | None:
            value = _value(env, env_name, tuya_raw, key, required=required, default=default)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as error:
                raise RuntimeConfigError(f"{key} must be an integer") from error

        mapping = TuyaDpMapping(
            power=number("OPENHDO_TUYA_DP_POWER", "dp_power", required=True),
            brightness=number("OPENHDO_TUYA_DP_BRIGHTNESS", "dp_brightness", required=True),
            color=number("OPENHDO_TUYA_DP_COLOR", "dp_color", required=True),
            color_format=_string(env, "OPENHDO_TUYA_COLOR_FORMAT", tuya_raw, "color_format", required=True),
            brightness_min=number("OPENHDO_TUYA_BRIGHTNESS_MIN", "brightness_min", required=True),
            brightness_max=number("OPENHDO_TUYA_BRIGHTNESS_MAX", "brightness_max", required=True),
            white=number("OPENHDO_TUYA_DP_WHITE", "dp_white"),
            white_min=number("OPENHDO_TUYA_WHITE_MIN", "white_min"),
            white_max=number("OPENHDO_TUYA_WHITE_MAX", "white_max"),
        )
        device = TuyaDeviceConfig(
            ip=_string(env, "OPENHDO_TUYA_IP", tuya_raw, "ip", required=True),
            device_id=device_id,
            local_key=_string(env, "OPENHDO_TUYA_LOCAL_KEY", tuya_raw, "local_key", required=True),
            protocol_version=_string(env, "OPENHDO_TUYA_PROTOCOL", tuya_raw, "protocol", required=True),
            dps=mapping,
            public_name=_string(env, "OPENHDO_TUYA_PUBLIC_NAME", tuya_raw, "public_name", default="LED lamp"),
            port=number("OPENHDO_TUYA_PORT", "port", default=6668),
            timeout_s=float(_value(env, "OPENHDO_TUYA_TIMEOUT", tuya_raw, "timeout", default=3.0)),
            retries=number("OPENHDO_TUYA_RETRIES", "retries", default=1),
        )
        return cls(
            server_url=server,
            linker=LinkerConfig(
                id=linker_id,
                version=linker_version,
                name=linker_name,
                transport="wifi",
                discovery=DiscoveryConfig(),
                reconnect_initial_s=float(_value(env, "OPENHDO_RECONNECT_INITIAL", linker_raw, "reconnect_initial_s", default=1.0)),
                reconnect_max_s=float(_value(env, "OPENHDO_RECONNECT_MAX", linker_raw, "reconnect_max_s", default=30.0)),
            ),
            device=device,
            light_id=light_id,
            state_poll_interval_s=float(_value(env, "OPENHDO_STATE_POLL_INTERVAL", raw, "state_poll_interval_s", default=15.0)),
            server_open_timeout_s=float(_value(env, "OPENHDO_SERVER_OPEN_TIMEOUT", raw, "server_open_timeout_s", default=10.0)),
            server_api_token=server_api_token,
        )


def _load_json(path_value: str | os.PathLike[str]) -> Mapping[str, Any]:
    try:
        with Path(path_value).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeConfigError(f"cannot load config: {error}") from error
    if not isinstance(value, Mapping):
        raise RuntimeConfigError("config file root must be an object")
    return value


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = raw.get(name, {})
    if not isinstance(section, Mapping):
        raise RuntimeConfigError(f"config section {name!r} must be an object")
    return section


def _value(
    env: Mapping[str, str], env_name: str, raw: Mapping[str, Any], key: str,
    *, required: bool = False, default: Any = None,
) -> Any:
    value = env.get(env_name, raw.get(key, default))
    if required and (value is None or (isinstance(value, str) and not value.strip())):
        raise RuntimeConfigError(f"{env_name} is required")
    return value


def _string(
    env: Mapping[str, str], env_name: str, raw: Mapping[str, Any], key: str,
    *, required: bool = False, default: str | None = None,
) -> str | None:
    value = _value(env, env_name, raw, key, required=required, default=default)
    if value is not None and not isinstance(value, str):
        raise RuntimeConfigError(f"{key} must be a string")
    return value


def _is_local_host(host: str) -> bool:
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
