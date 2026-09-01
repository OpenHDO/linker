"""The language-neutral OpenHDO v1 message envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping
from uuid import UUID, uuid4

PROTOCOL_VERSION = 1
_TYPE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class ProtocolError(ValueError):
    """Raised when an envelope crosses the protocol boundary incorrectly."""


def _uuid(value: Any, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProtocolError(f"{field_name} must be a UUID") from error


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ProtocolError("ts must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProtocolError("ts must be an ISO-8601 string") from error
    if result.tzinfo is None:
        raise ProtocolError("ts must include a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Envelope:
    """Validated, serializable v1 message shared with the server."""

    type: str
    source: str
    payload: Mapping[str, Any]
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: UUID | None = None
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {self.version}")
        if not isinstance(self.id, UUID):
            raise ProtocolError("id must be a UUID")
        if self.correlation_id is not None and not isinstance(self.correlation_id, UUID):
            raise ProtocolError("correlation_id must be a UUID")
        if not isinstance(self.type, str) or not _TYPE.fullmatch(self.type):
            raise ProtocolError("type must be a lowercase domain name")
        if not isinstance(self.source, str) or not self.source or len(self.source) > 128:
            raise ProtocolError("source must contain 1 to 128 characters")
        if not isinstance(self.payload, Mapping):
            raise ProtocolError("payload must be an object")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ProtocolError("timestamp must include a timezone")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "v": self.version,
            "id": str(self.id),
            "type": self.type,
            "ts": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": self.source,
            "payload": dict(self.payload),
        }
        if self.correlation_id is not None:
            result["correlation_id"] = str(self.correlation_id)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Envelope":
        if not isinstance(data, Mapping):
            raise ProtocolError("envelope must be an object")
        allowed = {"v", "id", "type", "ts", "source", "correlation_id", "payload"}
        unknown = set(data) - allowed
        if unknown:
            raise ProtocolError(f"unknown envelope fields: {', '.join(sorted(unknown))}")
        required = {"v", "id", "type", "ts", "source", "payload"}
        missing = required - set(data)
        if missing:
            raise ProtocolError(f"missing envelope fields: {', '.join(sorted(missing))}")
        if data["v"] != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {data['v']}")
        if not isinstance(data["payload"], Mapping):
            raise ProtocolError("payload must be an object")
        if not isinstance(data["type"], str) or not isinstance(data["source"], str):
            raise ProtocolError("type and source must be strings")
        correlation = data.get("correlation_id")
        return cls(
            type=data["type"],
            source=data["source"],
            payload=data["payload"],
            id=_uuid(data["id"], "id"),
            timestamp=_timestamp(data["ts"]),
            correlation_id=None if correlation is None else _uuid(correlation, "correlation_id"),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "Envelope":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ProtocolError("envelope must contain valid JSON") from error
        return cls.from_dict(data)
