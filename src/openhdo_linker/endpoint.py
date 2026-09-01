"""Transport-neutral JSON-line endpoint for the OpenHDO boundary."""

from __future__ import annotations

from .boundary import LinkerBoundary
from .protocol import Envelope


class JsonLineEndpoint:
    """Consumes one v1 JSON line and returns one result JSON line."""

    def __init__(self, boundary: LinkerBoundary) -> None:
        self._boundary = boundary

    async def handle(self, line: str | bytes) -> str:
        message = Envelope.from_json(line)
        return (await self._boundary.handle(message)).to_json()
