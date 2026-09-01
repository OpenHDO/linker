"""Native local Tuya-compatible Wi-Fi driver.

Only the local LAN protocol is implemented here. Device-specific assumptions
live in :class:`TuyaDeviceConfig`, and missing credentials or mappings fail at
construction time.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import select
import socket
import struct
import time
from uuid import UUID

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from .config import Credentials, DiscoveryConfig
from .driver import StateCallback, Unsubscribe, VendorRgbDriver
from .models import DeviceDescriptor, DriverHealth, LightState, Rgb

PREFIX = 0x000055AA
SUFFIX = 0x0000AA55
TCP_PORT = 6668
UDP_PORTS = (6666, 6667, 7000)
COMMAND_CONTROL = 7
COMMAND_STATUS = 8
COMMAND_HEARTBEAT = 9
COMMAND_DP_QUERY = 10
COMMAND_SESSION_START = 3
COMMAND_SESSION_RESPONSE = 4
COMMAND_SESSION_FINISH = 5
COMMAND_CONTROL_NEW = 13
COMMAND_DP_QUERY_NEW = 16
COMMAND_REQUEST_DEVICE_INFO = 37
SUPPORTED_PROTOCOLS = ("3.1", "3.2", "3.3", "3.4")
DISCOVERY_KEY = b"yGAdlopoPVldABfn"
VERSION_HEADER = {version: version.encode("ascii") + b"\0" * 12 for version in SUPPORTED_PROTOCOLS}
NO_VERSION_HEADER = {
    COMMAND_DP_QUERY,
    COMMAND_HEARTBEAT,
    COMMAND_SESSION_START,
    COMMAND_SESSION_RESPONSE,
    COMMAND_SESSION_FINISH,
    COMMAND_DP_QUERY_NEW,
}


class TuyaProtocolError(ValueError):
    """The peer sent a malformed or unauthenticated local-protocol message."""


class TuyaConfigurationError(ValueError):
    """Required real-device onboarding information is missing or invalid."""


def _bounded_int(name: str, value: int, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise TuyaConfigurationError(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True, slots=True)
class TuyaDpMapping:
    """Explicit device data-point mapping; no vendor defaults are assumed."""

    power: int
    brightness: int
    color: int
    color_format: str
    brightness_min: int
    brightness_max: int
    white: int | None = None
    white_min: int | None = None
    white_max: int | None = None

    def __post_init__(self) -> None:
        indexes = (self.power, self.brightness, self.color)
        for name, value in zip(("power", "brightness", "color"), indexes):
            _bounded_int(f"DP {name}", value, 1, 255)
        if len(set(indexes)) != len(indexes):
            raise TuyaConfigurationError("power, brightness, and color DPs must be distinct")
        if self.color_format not in {"rgb_hex", "hsv_hex"}:
            raise TuyaConfigurationError("color_format must be rgb_hex or hsv_hex")
        _bounded_int("brightness_min", self.brightness_min, 0, 65535)
        _bounded_int("brightness_max", self.brightness_max, 0, 65535)
        if self.brightness_max <= self.brightness_min:
            raise TuyaConfigurationError("brightness_max must be greater than brightness_min")
        if self.white is None:
            if self.white_min is not None or self.white_max is not None:
                raise TuyaConfigurationError("white_min/white_max require a white DP")
        else:
            _bounded_int("DP white", self.white, 1, 255)
            if self.white in indexes:
                raise TuyaConfigurationError("white DP must be distinct")
            if self.white_min is None or self.white_max is None:
                raise TuyaConfigurationError("white_min and white_max are required with a white DP")
            _bounded_int("white_min", self.white_min, 0, 65535)
            _bounded_int("white_max", self.white_max, 0, 65535)
            if self.white_max <= self.white_min:
                raise TuyaConfigurationError("white_max must be greater than white_min")

    def brightness_to_dp(self, value: int) -> int:
        value = _bounded_int("brightness", value, 0, 255)
        span = self.brightness_max - self.brightness_min
        return self.brightness_min + (value * span + 127) // 255

    def brightness_from_dp(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TuyaProtocolError("brightness DP is not numeric")
        numeric = int(value)
        if not self.brightness_min <= numeric <= self.brightness_max:
            raise TuyaProtocolError("brightness DP is outside configured range")
        span = self.brightness_max - self.brightness_min
        return round((numeric - self.brightness_min) * 255 / span)

    def white_to_dp(self, value: int) -> int:
        if self.white is None or self.white_min is None or self.white_max is None:
            raise TuyaConfigurationError("white DP is not configured")
        value = _bounded_int("white", value, 0, 255)
        span = self.white_max - self.white_min
        return self.white_min + (value * span + 127) // 255

    def white_from_dp(self, value: object) -> int:
        if self.white is None or self.white_min is None or self.white_max is None:
            raise TuyaConfigurationError("white DP is not configured")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TuyaProtocolError("white DP is not numeric")
        numeric = int(value)
        if not self.white_min <= numeric <= self.white_max:
            raise TuyaProtocolError("white DP is outside configured range")
        return round((numeric - self.white_min) * 255 / (self.white_max - self.white_min))

    def encode_color(self, rgb: Rgb) -> str:
        if self.color_format == "rgb_hex":
            return f"{rgb.r:02x}{rgb.g:02x}{rgb.b:02x}"
        # Tuya's configurable HSV representation uses four hex digits per
        # component.  The format is selected explicitly, never inferred.
        import colorsys

        hue, saturation, value = colorsys.rgb_to_hsv(rgb.r / 255, rgb.g / 255, rgb.b / 255)
        return f"{round(hue * 360):04x}{round(saturation * 1000):04x}{round(value * 1000):04x}"

    def decode_color(self, value: object) -> Rgb:
        if not isinstance(value, str):
            raise TuyaProtocolError("color DP must be a hexadecimal string")
        if self.color_format == "rgb_hex":
            if len(value) not in {6, 8}:
                raise TuyaProtocolError("rgb_hex color DP must contain 6 or 8 hex digits")
            try:
                return Rgb(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
            except ValueError as error:
                raise TuyaProtocolError("rgb_hex color DP contains invalid hex") from error
        if len(value) != 12:
            raise TuyaProtocolError("hsv_hex color DP must contain 12 hex digits")
        try:
            hue = int(value[0:4], 16) / 360
            saturation = int(value[4:8], 16) / 1000
            brightness = int(value[8:12], 16) / 1000
        except ValueError as error:
            raise TuyaProtocolError("hsv_hex color DP contains invalid hex") from error
        import colorsys

        red, green, blue = colorsys.hsv_to_rgb(hue % 1, saturation, brightness)
        return Rgb(round(red * 255), round(green * 255), round(blue * 255))


@dataclass(frozen=True, slots=True)
class TuyaDeviceConfig:
    """All values required to address one actual local device."""

    ip: str
    device_id: str
    local_key: str = field(repr=False)
    protocol_version: str
    dps: TuyaDpMapping
    public_name: str = "LED lamp"
    port: int = TCP_PORT
    timeout_s: float = 3.0
    retries: int = 1

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.ip)
        except ValueError as error:
            raise TuyaConfigurationError("ip must be a valid IPv4 or IPv6 address") from error
        if not address.is_private and not address.is_loopback and not address.is_link_local:
            raise TuyaConfigurationError("local Tuya device IP must be private, loopback, or link-local")
        if not self.device_id or len(self.device_id) > 64:
            raise TuyaConfigurationError("device_id is required and must be at most 64 characters")
        if not self.public_name or len(self.public_name) > 128:
            raise TuyaConfigurationError("public_name must contain 1 to 128 characters")
        try:
            self.local_key.encode("ascii")
        except UnicodeEncodeError as error:
            raise TuyaConfigurationError("local_key must contain ASCII characters") from error
        if len(self.local_key.encode("ascii")) != 16:
            raise TuyaConfigurationError("local_key must be exactly 16 ASCII bytes")
        if self.protocol_version not in SUPPORTED_PROTOCOLS:
            supported = ", ".join(SUPPORTED_PROTOCOLS)
            raise TuyaConfigurationError(f"protocol_version must be one of {supported}; 3.5 is not implemented")
        _bounded_int("port", self.port, 1, 65535)
        if self.timeout_s <= 0 or self.timeout_s > 60:
            raise TuyaConfigurationError("timeout_s must be greater than 0 and at most 60")
        if type(self.retries) is not int or not 0 <= self.retries <= 5:
            raise TuyaConfigurationError("retries must be an integer from 0 to 5")

    @property
    def key_bytes(self) -> bytes:
        return self.local_key.encode("ascii")


@dataclass(frozen=True, slots=True)
class TuyaDiscoveryOptions:
    """Safe, opt-in UDP discovery settings."""

    enabled: bool = False
    broadcast_address: str = "255.255.255.255"
    ports: tuple[int, ...] = UDP_PORTS

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.broadcast_address)
        except ValueError as error:
            raise TuyaConfigurationError("broadcast_address must be an IPv4 address") from error
        if address.version != 4 or self.broadcast_address != "255.255.255.255":
            raise TuyaConfigurationError("discovery only permits the global LAN broadcast address")
        if not self.ports or any(type(port) is not int or not 1 <= port <= 65535 for port in self.ports):
            raise TuyaConfigurationError("discovery ports must be valid UDP ports")


@dataclass(frozen=True, slots=True)
class TuyaFrame:
    sequence: int
    command: int
    payload: bytes


def _aes_encrypt(data: bytes, key: bytes, *, pad: bool = True) -> bytes:
    if pad:
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        data = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return cipher.update(data) + cipher.finalize()


def _aes_decrypt(data: bytes, key: bytes, *, unpad: bool = True) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    result = cipher.update(data) + cipher.finalize()
    if not unpad:
        return result
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        return unpadder.update(result) + unpadder.finalize()
    except ValueError as error:
        raise TuyaProtocolError("invalid AES padding") from error


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _payload_for_command(version: str, command: int, body: Mapping[str, object], key: bytes, session_key: bytes | None) -> bytes:
    clear = _json_bytes(body)
    if version == "3.1" and command == COMMAND_CONTROL:
        encrypted = base64.b64encode(_aes_encrypt(clear, key))
        digest = hashlib.md5(b"data=" + encrypted + b"||lpv=3.1||" + key).hexdigest().encode("ascii")[8:24]
        return b"3.1" + digest + encrypted
    encryption_key = session_key if version == "3.4" and session_key is not None else key
    if version == "3.4":
        return _aes_encrypt(VERSION_HEADER[version] + clear, encryption_key)
    if command in NO_VERSION_HEADER:
        return _aes_encrypt(clear, encryption_key) if version != "3.1" else clear
    return VERSION_HEADER[version] + _aes_encrypt(clear, encryption_key)


def _frame_bytes(sequence: int, command: int, payload: bytes, *, protocol_version: str, integrity_key: bytes) -> bytes:
    trailer_size = 36 if protocol_version == "3.4" else 8
    length = len(payload) + trailer_size
    header = struct.pack(">IIII", PREFIX, sequence, command, length)
    signed = header + payload
    trailer = hmac.new(integrity_key, signed, "sha256").digest() if protocol_version == "3.4" else struct.pack(">I", binascii.crc32(signed) & 0xFFFFFFFF)
    return signed + trailer + struct.pack(">I", SUFFIX)


def parse_frame(data: bytes, *, protocol_version: str, integrity_key: bytes) -> TuyaFrame:
    if len(data) < 24 or data[:4] != struct.pack(">I", PREFIX) or data[-4:] != struct.pack(">I", SUFFIX):
        raise TuyaProtocolError("invalid Tuya frame prefix or suffix")
    _, sequence, command, length = struct.unpack(">IIII", data[:16])
    if length > 1024 * 1024 or len(data) != 16 + length:
        raise TuyaProtocolError("invalid Tuya frame length")
    trailer_size = 36 if protocol_version == "3.4" else 8
    if length < trailer_size:
        raise TuyaProtocolError("Tuya frame is shorter than its integrity trailer")
    payload_end = 16 + length - trailer_size
    payload = data[16:payload_end]
    signed = data[:payload_end]
    trailer = data[payload_end : payload_end + trailer_size]
    if protocol_version == "3.4":
        if not hmac.compare_digest(trailer[:32], hmac.new(integrity_key, signed, "sha256").digest()):
            raise TuyaProtocolError("Tuya frame HMAC verification failed")
    elif struct.unpack(">I", trailer[:4])[0] != (binascii.crc32(signed) & 0xFFFFFFFF):
        raise TuyaProtocolError("Tuya frame CRC verification failed")
    return TuyaFrame(sequence, command, payload)


def decode_payload(frame: TuyaFrame, *, protocol_version: str, key: bytes, session_key: bytes | None = None) -> bytes:
    payload = frame.payload
    if protocol_version == "3.1" and payload.startswith(b"3.1"):
        if len(payload) < 19:
            raise TuyaProtocolError("truncated 3.1 payload")
        encrypted = payload[19:]
        try:
            return _aes_decrypt(base64.b64decode(encrypted, validate=True), key)
        except (binascii.Error, ValueError) as error:
            raise TuyaProtocolError("invalid 3.1 encrypted payload") from error
    if protocol_version == "3.4" and frame.command in {COMMAND_SESSION_RESPONSE}:
        return _aes_decrypt(payload, key)
    if protocol_version == "3.4":
        decrypted = _aes_decrypt(payload, session_key if session_key is not None else key)
        header = VERSION_HEADER[protocol_version]
        if not decrypted.startswith(header):
            raise TuyaProtocolError("3.4 payload is missing its encrypted version header")
        return decrypted[len(header) :]
    if protocol_version in {"3.2", "3.3"}:
        header = VERSION_HEADER[protocol_version]
        encrypted = payload[len(header) :] if payload.startswith(header) else payload
        encryption_key = session_key if protocol_version == "3.4" and session_key is not None else key
        return _aes_decrypt(encrypted, encryption_key)
    return payload


def _without_retcode(payload: bytes) -> bytes:
    return payload[4:] if len(payload) >= 4 and payload[:4] == b"\0\0\0\0" else payload


def parse_dps(payload: bytes | bytearray | Mapping[str, object]) -> dict[int, object]:
    """Extract indexed DPs from a Tuya response without assuming a model."""

    if isinstance(payload, (bytes, bytearray)):
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TuyaProtocolError("Tuya payload is not valid JSON") from error
    else:
        value = payload
    if not isinstance(value, Mapping):
        raise TuyaProtocolError("Tuya payload must be an object")
    dps: object = value.get("dps")
    if dps is None and isinstance(value.get("data"), Mapping):
        dps = value["data"].get("dps")
    if not isinstance(dps, Mapping):
        raise TuyaProtocolError("Tuya payload does not contain indexed dps")
    result: dict[int, object] = {}
    for raw_index, dp_value in dps.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as error:
            raise TuyaProtocolError("Tuya DP index must be an integer") from error
        try:
            _bounded_int("DP index", index, 1, 255)
        except TuyaConfigurationError as error:
            raise TuyaProtocolError("Tuya DP index is outside the supported range") from error
        result[index] = dp_value
    return result


def state_from_dps(device_id: str, dps: Mapping[int, object], mapping: TuyaDpMapping, *, observed_at: datetime | None = None) -> LightState:
    """Map only configured values to the OpenHDO light state."""

    if mapping.power not in dps:
        raise TuyaProtocolError("configured power DP is absent")
    power = dps[mapping.power]
    if not isinstance(power, bool):
        raise TuyaProtocolError("configured power DP is not boolean")
    if mapping.brightness not in dps or mapping.color not in dps:
        raise TuyaProtocolError("configured brightness and color DPs are absent")
    rgb = mapping.decode_color(dps[mapping.color])
    brightness = mapping.brightness_from_dp(dps[mapping.brightness])
    white = mapping.white_from_dp(dps[mapping.white]) if mapping.white is not None and mapping.white in dps else None
    return LightState(device_id, True, power, rgb, brightness, observed_at, white)


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[TuyaFrame]


class TuyaLocalDriver(VendorRgbDriver):
    """A single-device native local Tuya TCP adapter."""

    def __init__(self, device: TuyaDeviceConfig | None = None, discovery: TuyaDiscoveryOptions | None = None) -> None:
        self._device = device
        self.discovery_options = discovery or TuyaDiscoveryOptions()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[int, _Pending] = {}
        self._callbacks: dict[str, list[StateCallback]] = {}
        self._raw_dps: dict[int, object] = {}
        self._sequence = 0
        self._session_key: bytes | None = None
        self._state: LightState | None = None
        self._last_error: str | None = None
        self._connected_at: datetime | None = None

    async def discover(self, config: DiscoveryConfig, credentials: Credentials) -> Sequence[DeviceDescriptor]:
        if not self.discovery_options.enabled:
            return ()
        found = await asyncio.to_thread(self._discover_sync, config.timeout_s)
        return tuple(self._descriptor(item["id"]) for item in found)

    @property
    def device(self) -> TuyaDeviceConfig:
        if self._device is None:
            raise TuyaConfigurationError(
                "real device configuration is required for connect/poll/commands; discovery may run without it"
            )
        return self._device

    def _descriptor(self, device_id: str) -> DeviceDescriptor:
        if self._device is None:
            return DeviceDescriptor(device_id, "LED lamp")
        color_modes = ("RGBW",) if self.device.dps.white is not None else ("RGB",)
        return DeviceDescriptor(device_id, self.device.public_name, color_modes)

    def _discover_sync(self, timeout_s: float) -> list[dict[str, str]]:
        sockets: list[socket.socket] = []
        found: dict[str, str] = {}
        try:
            for port in self.discovery_options.ports:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                sock.setblocking(False)
                sockets.append(sock)
            request = _frame_bytes(0, COMMAND_REQUEST_DEVICE_INFO, b"", protocol_version="3.1", integrity_key=b"")
            for sock in sockets:
                sock.sendto(request, (self.discovery_options.broadcast_address, 7000))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                readable, _, _ = select.select(sockets, [], [], remaining)
                for sock in readable:
                    data, address = sock.recvfrom(65535)
                    item = _parse_discovery_datagram(data, address[0])
                    if item and item.get("id"):
                        found[item["id"]] = item.get("ip", address[0])
            return [{"id": device_id, "ip": ip} for device_id, ip in found.items()]
        finally:
            for sock in sockets:
                sock.close()

    async def connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.device.ip, self.device.port), timeout=self.device.timeout_s
        )
        self._session_key = None
        try:
            if self.device.protocol_version == "3.4":
                await self._negotiate_session()
            self._reader_task = asyncio.create_task(self._reader_loop(), name=f"tuya-reader-{self.device.device_id}")
            self._connected_at = datetime.now(timezone.utc)
            self._last_error = None
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        writer, self._writer = self._writer, None
        self._reader = None
        self._session_key = None
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(ConnectionError("Tuya device disconnected"))
        self._pending.clear()
        if writer is not None:
            writer.close()
            await writer.wait_closed()

    async def poll_state(self, device_id: str) -> LightState:
        self._check_device(device_id)
        body: Mapping[str, object] = {}
        if self.device.protocol_version != "3.4":
            body = {"dps": {str(index): None for index in (self.device.dps.power, self.device.dps.brightness, self.device.dps.color)}}
        payload = await self._exchange(COMMAND_DP_QUERY_NEW if self.device.protocol_version == "3.4" else COMMAND_DP_QUERY, body)
        self._raw_dps = parse_dps(payload)
        state = self._revisioned(
            state_from_dps(self.device.device_id, self._raw_dps, self.device.dps, observed_at=datetime.now(timezone.utc))
        )
        self._state = state
        return state

    def descriptor(self, light_id: str | None = None) -> DeviceDescriptor:
        """Return the abstract capability descriptor for the configured device."""

        descriptor = self._descriptor(self.device.device_id)
        return replace(descriptor, id=light_id) if light_id is not None else descriptor

    def _revisioned(self, state: LightState) -> LightState:
        revision = 0 if self._state is None else self._state.state_revision + 1
        return replace(state, state_revision=revision)

    async def subscribe_state(self, device_id: str, callback: StateCallback) -> Unsubscribe:
        self._check_device(device_id)
        callbacks = self._callbacks.setdefault(device_id, [])
        callbacks.append(callback)

        async def unsubscribe() -> None:
            if callback in callbacks:
                callbacks.remove(callback)

        return unsubscribe

    async def turn_on(self, device_id: str, rgb: Rgb | None, brightness: int | None, white: int | None, command_id: UUID) -> LightState:
        self._check_device(device_id)
        values: dict[int, object] = {self.device.dps.power: True}
        if rgb is not None:
            values[self.device.dps.color] = self.device.dps.encode_color(rgb)
        if brightness is not None:
            values[self.device.dps.brightness] = self.device.dps.brightness_to_dp(brightness)
        if white is not None:
            values[self._white_dp()] = self.device.dps.white_to_dp(white)
        return await self._set_values(values)

    async def turn_off(self, device_id: str, command_id: UUID) -> LightState:
        self._check_device(device_id)
        return await self._set_values({self.device.dps.power: False})

    async def set_brightness(self, device_id: str, brightness: int, command_id: UUID) -> LightState:
        self._check_device(device_id)
        return await self._set_values({self.device.dps.brightness: self.device.dps.brightness_to_dp(brightness)})

    async def set_rgb(self, device_id: str, rgb: Rgb, command_id: UUID) -> LightState:
        self._check_device(device_id)
        return await self._set_values({self.device.dps.color: self.device.dps.encode_color(rgb)})

    async def set_white(self, device_id: str, white: int, command_id: UUID) -> LightState:
        self._check_device(device_id)
        return await self._set_values({self._white_dp(): self.device.dps.white_to_dp(white)})

    def _white_dp(self) -> int:
        if self.device.dps.white is None:
            raise TuyaConfigurationError("white channel is not configured for this device")
        return self.device.dps.white

    async def _set_values(self, values: Mapping[int, object]) -> LightState:
        body: Mapping[str, object]
        if self.device.protocol_version == "3.4":
            body = {"protocol": 5, "t": int(time.time()), "data": {"dps": {str(k): v for k, v in values.items()}}}
            command = COMMAND_CONTROL_NEW
        else:
            body = {"devId": self.device.device_id, "uid": self.device.device_id, "t": int(time.time()), "dps": {str(k): v for k, v in values.items()}}
            command = COMMAND_CONTROL
        await self._exchange(command, body)
        # ACK payloads vary by firmware. A confirmed read-after-write keeps
        # the abstract state truthful without guessing what an empty ACK means.
        state = await self.poll_state(self.device.device_id)
        self._state = state
        return state

    async def health(self) -> DriverHealth:
        connected = (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader_task is not None
            and not self._reader_task.done()
        )
        return DriverHealth(connected, self._last_error, datetime.now(timezone.utc))

    def _check_device(self, device_id: str) -> None:
        if device_id != self.device.device_id:
            raise TuyaConfigurationError(f"device_id {device_id!r} is not configured for this driver")

    async def _negotiate_session(self) -> None:
        local_nonce = b"0123456789abcdef"
        payload = _aes_encrypt(local_nonce, self.device.key_bytes)
        frame = _frame_bytes(self._next_sequence(), COMMAND_SESSION_START, payload, protocol_version="3.4", integrity_key=self.device.key_bytes)
        await self._write(frame)
        response = await self._read_frame_until(COMMAND_SESSION_RESPONSE)
        encrypted = response.payload
        remote = _aes_decrypt(encrypted, self.device.key_bytes)
        if len(remote) < 48 or not hmac.compare_digest(remote[16:48], hmac.new(self.device.key_bytes, local_nonce, "sha256").digest()):
            raise TuyaProtocolError("Tuya session negotiation response failed verification")
        remote_nonce = remote[:16]
        finish = hmac.new(self.device.key_bytes, remote_nonce, "sha256").digest()
        frame = _frame_bytes(self._next_sequence(), COMMAND_SESSION_FINISH, _aes_encrypt(finish, self.device.key_bytes), protocol_version="3.4", integrity_key=self.device.key_bytes)
        await self._write(frame)
        mixed = bytes(left ^ right for left, right in zip(local_nonce, remote_nonce))
        self._session_key = _aes_encrypt(mixed, self.device.key_bytes, pad=False)

    async def _exchange(self, command: int, body: Mapping[str, object]) -> bytes:
        async with self._send_lock:
            last_error: Exception | None = None
            for attempt in range(self.device.retries + 1):
                sequence: int | None = None
                try:
                    if self._writer is None or self._writer.is_closing():
                        await self.connect()
                    sequence = self._next_sequence()
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[TuyaFrame] = loop.create_future()
                    self._pending[sequence] = _Pending(future)
                    key = self._session_key if self.device.protocol_version == "3.4" and self._session_key else self.device.key_bytes
                    payload = _payload_for_command(self.device.protocol_version, command, body, self.device.key_bytes, self._session_key)
                    await self._write(_frame_bytes(sequence, command, payload, protocol_version=self.device.protocol_version, integrity_key=key))
                    frame = await asyncio.wait_for(future, timeout=self.device.timeout_s)
                    decoded = decode_payload(frame, protocol_version=self.device.protocol_version, key=self.device.key_bytes, session_key=self._session_key)
                    return _without_retcode(decoded)
                except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError, OSError, TuyaProtocolError) as error:
                    last_error = error
                    self._last_error = str(error)
                    if sequence is not None:
                        self._pending.pop(sequence, None)
                    await self.disconnect()
                    if attempt < self.device.retries:
                        await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            raise ConnectionError(f"Tuya exchange failed: {last_error}") from last_error

    async def _write(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("Tuya device is not connected")
        self._writer.write(data)
        await asyncio.wait_for(self._writer.drain(), timeout=self.device.timeout_s)

    async def _reader_loop(self) -> None:
        try:
            while self._reader is not None:
                frame = await _read_frame(self._reader, self.device.protocol_version, self._session_key or self.device.key_bytes)
                if frame.command == COMMAND_STATUS:
                    await self._handle_status(frame)
                    continue
                pending = self._pending.pop(frame.sequence, None)
                if pending is not None and not pending.future.done():
                    pending.future.set_result(frame)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = str(error)
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(ConnectionError(str(error)))
            self._pending.clear()

    async def _handle_status(self, frame: TuyaFrame) -> None:
        payload = _without_retcode(
            decode_payload(frame, protocol_version=self.device.protocol_version, key=self.device.key_bytes, session_key=self._session_key)
        )
        try:
            self._raw_dps.update(parse_dps(payload))
            state = self._revisioned(
                state_from_dps(self.device.device_id, self._raw_dps, self.device.dps, observed_at=datetime.now(timezone.utc))
            )
        except TuyaProtocolError:
            return
        self._state = state
        for callback in tuple(self._callbacks.get(self.device.device_id, ())):
            try:
                result = callback(state)
                if isinstance(result, Awaitable):
                    await result
            except Exception as error:
                # A publication consumer must not take down the device reader.
                self._last_error = f"state callback failed: {error}"

    async def _read_frame_until(self, command: int) -> TuyaFrame:
        while self._reader is not None:
            frame = await asyncio.wait_for(_read_frame(self._reader, "3.4", self.device.key_bytes), timeout=self.device.timeout_s)
            if frame.command == command:
                return frame
        raise ConnectionError("Tuya device disconnected during negotiation")

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return self._sequence


async def _read_frame(reader: asyncio.StreamReader, protocol_version: str, integrity_key: bytes) -> TuyaFrame:
    header = await reader.readexactly(16)
    if header[:4] != struct.pack(">I", PREFIX):
        raise TuyaProtocolError("invalid Tuya frame prefix")
    _, sequence, command, length = struct.unpack(">IIII", header)
    if length > 1024 * 1024:
        raise TuyaProtocolError("Tuya frame exceeds 1 MiB")
    rest = await reader.readexactly(length)
    return parse_frame(header + rest, protocol_version=protocol_version, integrity_key=integrity_key)


def _parse_discovery_datagram(data: bytes, ip: str) -> dict[str, str] | None:
    for protocol, key in (("3.1", DISCOVERY_KEY), ("3.4", DISCOVERY_KEY)):
        try:
            frame = parse_frame(data, protocol_version=protocol, integrity_key=key)
            payload = decode_payload(frame, protocol_version=protocol, key=key)
            value = json.loads(payload)
            if isinstance(value, Mapping):
                device_id = value.get("gwId") or value.get("devId") or value.get("id")
                if isinstance(device_id, str):
                    return {"id": device_id, "ip": str(value.get("ip", ip))}
        except (TuyaProtocolError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return None
