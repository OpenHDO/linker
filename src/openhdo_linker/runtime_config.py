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
from .tuya import TuyaDeviceConfig, TuyaDpMapping, TuyaPairingConfig

_LIGHT_ID = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")


class RuntimeConfigError(ValueError):
    """Raised when process or real-device configuration is incomplete."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    server_url: str
    linker: LinkerConfig
    device: TuyaDeviceConfig | None = field(default=None, repr=False)
    light_id: str | None = None
    state_poll_interval_s: float = 15.0
    server_open_timeout_s: float = 10.0
    server_api_token: str | None = field(default=None, repr=False)
    discovery_only: bool = False
    discovery_enabled: bool = False
    pairing: TuyaPairingConfig | None = field(default=None, repr=False)
    pairing_state_path: str = "openhdo-pairing.json"

    def __post_init__(self) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeConfigError("server must be a ws:// or wss:// URL with a host")
        if self.server_api_token is not None and not self.server_api_token.strip():
            raise RuntimeConfigError("server_api_token must not be empty")
        if not self.pairing_state_path.strip():
            raise RuntimeConfigError("pairing_state_path must not be empty")
        if not _is_local_host(parsed.hostname) and (parsed.scheme != "wss" or self.server_api_token is None):
            raise RuntimeConfigError("non-local servers require wss:// and OPENHDO_SERVER_TOKEN")
        if self.discovery_only:
            if self.device is not None or self.light_id is not None:
                raise RuntimeConfigError("discovery-only mode cannot include real-device configuration")
        else:
            if self.device is None:
                raise RuntimeConfigError("real device configuration is required outside discovery-only mode")
            if self.light_id is None or not _LIGHT_ID.fullmatch(self.light_id):
                raise RuntimeConfigError("light_id must be a lowercase OpenHDO identifier")
        if type(self.discovery_enabled) is not bool:
            raise RuntimeConfigError("discovery_enabled must be boolean")
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
        discovery_only = _boolean(env, "OPENHDO_DISCOVERY_ONLY", raw, "discovery_only", default=False)
        discovery_enabled = _boolean(
            env,
            "OPENHDO_TUYA_DISCOVERY_ENABLED",
            tuya_raw,
            "discovery_enabled",
            default=discovery_only,
        )
        linker = LinkerConfig(
            id=linker_id,
            version=linker_version,
            name=linker_name,
            transport="wifi",
            discovery=DiscoveryConfig(),
            reconnect_initial_s=float(_value(env, "OPENHDO_RECONNECT_INITIAL", linker_raw, "reconnect_initial_s", default=1.0)),
            reconnect_max_s=float(_value(env, "OPENHDO_RECONNECT_MAX", linker_raw, "reconnect_max_s", default=30.0)),
        )
        if discovery_only:
            return cls(
                server_url=server,
                linker=linker,
                pairing=_pairing_config(env, tuya_raw),
                pairing_state_path=_string(
                    env, "OPENHDO_TUYA_PAIRING_STATE", tuya_raw, "pairing_state", default="openhdo-pairing.json"
                ) or "openhdo-pairing.json",
                discovery_only=True,
                discovery_enabled=discovery_enabled,
                state_poll_interval_s=float(_value(env, "OPENHDO_STATE_POLL_INTERVAL", raw, "state_poll_interval_s", default=15.0)),
                server_open_timeout_s=float(_value(env, "OPENHDO_SERVER_OPEN_TIMEOUT", raw, "server_open_timeout_s", default=10.0)),
                server_api_token=server_api_token,
            )
        light_id = _string(env, "OPENHDO_LIGHT_ID", tuya_raw, "light_id", default=None)
        device_id = _string(env, "OPENHDO_TUYA_DEVICE_ID", tuya_raw, "device_id", required=True)
        if light_id is None:
            light_id = device_id
        if not isinstance(light_id, str):
            raise RuntimeConfigError("light_id must be a string")

        mapping = _tuya_mapping(env, tuya_raw)
        device = TuyaDeviceConfig(
            ip=_string(env, "OPENHDO_TUYA_IP", tuya_raw, "ip", required=True),
            device_id=device_id,
            local_key=_string(env, "OPENHDO_TUYA_LOCAL_KEY", tuya_raw, "local_key", required=True),
            protocol_version=_string(env, "OPENHDO_TUYA_PROTOCOL", tuya_raw, "protocol", required=True),
            dps=mapping,
            public_name=_string(env, "OPENHDO_TUYA_PUBLIC_NAME", tuya_raw, "public_name", default="LED lamp"),
            port=_tuya_number(env, tuya_raw, "OPENHDO_TUYA_PORT", "port", default=6668),
            timeout_s=float(_value(env, "OPENHDO_TUYA_TIMEOUT", tuya_raw, "timeout", default=3.0)),
            retries=_tuya_number(env, tuya_raw, "OPENHDO_TUYA_RETRIES", "retries", default=1),
        )
        return cls(
            server_url=server,
            linker=linker,
            device=device,
            light_id=light_id,
            discovery_enabled=discovery_enabled,
            state_poll_interval_s=float(_value(env, "OPENHDO_STATE_POLL_INTERVAL", raw, "state_poll_interval_s", default=15.0)),
            server_open_timeout_s=float(_value(env, "OPENHDO_SERVER_OPEN_TIMEOUT", raw, "server_open_timeout_s", default=10.0)),
            server_api_token=server_api_token,
            pairing_state_path=_string(
                env, "OPENHDO_TUYA_PAIRING_STATE", tuya_raw, "pairing_state", default="openhdo-pairing.json"
            ) or "openhdo-pairing.json",
        )


def _tuya_number(
    env: Mapping[str, str], raw: Mapping[str, Any], env_name: str, key: str,
    *, required: bool = False, default: int | None = None,
) -> int | None:
    value = _value(env, env_name, raw, key, required=required, default=default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeConfigError(f"{key} must be an integer") from error


def _tuya_mapping(env: Mapping[str, str], raw: Mapping[str, Any]) -> TuyaDpMapping:
    return TuyaDpMapping(
        power=_tuya_number(env, raw, "OPENHDO_TUYA_DP_POWER", "dp_power", required=True),
        brightness=_tuya_number(env, raw, "OPENHDO_TUYA_DP_BRIGHTNESS", "dp_brightness", required=True),
        color=_tuya_number(env, raw, "OPENHDO_TUYA_DP_COLOR", "dp_color", required=True),
        color_format=_string(env, "OPENHDO_TUYA_COLOR_FORMAT", raw, "color_format", required=True),
        brightness_min=_tuya_number(env, raw, "OPENHDO_TUYA_BRIGHTNESS_MIN", "brightness_min", required=True),
        brightness_max=_tuya_number(env, raw, "OPENHDO_TUYA_BRIGHTNESS_MAX", "brightness_max", required=True),
        white=_tuya_number(env, raw, "OPENHDO_TUYA_DP_WHITE", "dp_white"),
        white_min=_tuya_number(env, raw, "OPENHDO_TUYA_WHITE_MIN", "white_min"),
        white_max=_tuya_number(env, raw, "OPENHDO_TUYA_WHITE_MAX", "white_max"),
    )


def _pairing_config(env: Mapping[str, str], raw: Mapping[str, Any]) -> TuyaPairingConfig | None:
    required_keys = (
        "OPENHDO_TUYA_LOCAL_KEY", "OPENHDO_TUYA_PROTOCOL", "OPENHDO_TUYA_DP_POWER",
        "OPENHDO_TUYA_DP_BRIGHTNESS", "OPENHDO_TUYA_DP_COLOR", "OPENHDO_TUYA_COLOR_FORMAT",
        "OPENHDO_TUYA_BRIGHTNESS_MIN", "OPENHDO_TUYA_BRIGHTNESS_MAX",
    )
    if not any(
        _value(env, key, raw, key.removeprefix("OPENHDO_TUYA_").lower()) is not None
        for key in required_keys
    ):
        return None
    return TuyaPairingConfig(
        local_key=_string(env, "OPENHDO_TUYA_LOCAL_KEY", raw, "local_key", required=True),
        protocol_version=_string(env, "OPENHDO_TUYA_PROTOCOL", raw, "protocol", required=True),
        dps=_tuya_mapping(env, raw),
        public_name=_string(env, "OPENHDO_TUYA_PUBLIC_NAME", raw, "public_name", default="LED lamp"),
        port=_tuya_number(env, raw, "OPENHDO_TUYA_PORT", "port", default=6668),
        timeout_s=float(_value(env, "OPENHDO_TUYA_TIMEOUT", raw, "timeout", default=3.0)),
        retries=_tuya_number(env, raw, "OPENHDO_TUYA_RETRIES", "retries", default=1),
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


def _boolean(
    env: Mapping[str, str], env_name: str, raw: Mapping[str, Any], key: str,
    *, default: bool,
) -> bool:
    value = _value(env, env_name, raw, key, default=default)
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise RuntimeConfigError(f"{env_name} must be a boolean")


def _is_local_host(host: str) -> bool:
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
