"""Production WebSocket client for the Python OpenHDO server runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import logging
from typing import Any

import websockets

from .boundary import LinkerBoundary
from .protocol import Envelope, ProtocolError


class LinkerServerClient:
    def __init__(self, boundary: LinkerBoundary, url: str, *, server_api_token: str | None = None, reconnect_initial_s: float = 1.0, reconnect_max_s: float = 30.0, state_poll_interval_s: float = 15.0, open_timeout_s: float = 10.0, logger: logging.Logger | None = None) -> None:
        self.boundary = boundary
        self.url = url
        self.server_api_token = server_api_token
        self.reconnect_initial_s = reconnect_initial_s
        self.reconnect_max_s = reconnect_max_s
        self.state_poll_interval_s = state_poll_interval_s
        self.open_timeout_s = open_timeout_s
        self.logger = logger or logging.getLogger(__name__)
        self.last_error: str | None = None
        self._send_lock = asyncio.Lock()

    async def run(self, stop: asyncio.Event) -> None:
        delay = self.reconnect_initial_s
        while not stop.is_set():
            try:
                options = {
                    "open_timeout": self.open_timeout_s,
                    "ping_interval": 20,
                    "ping_timeout": 20,
                    "max_size": 1024 * 1024,
                }
                if self.server_api_token is not None:
                    header_name = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
                    options[header_name] = [("Authorization", f"Bearer {self.server_api_token}")]
                async with websockets.connect(self.url, **options) as websocket:
                    self.last_error = None
                    self.logger.info("connected to OpenHDO server")
                    await self._session(websocket, stop)
                    delay = self.reconnect_initial_s
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = str(error)
                self.logger.warning("OpenHDO server connection failed: %s", error)
                delay = min(delay * 2, self.reconnect_max_s)
            if not await _wait_or_stop(stop, delay):
                return

    async def _session(self, websocket: Any, process_stop: asyncio.Event) -> None:
        await self._send(websocket, self.boundary.register())
        session_stop = asyncio.Event()
        receiver = asyncio.create_task(self._receive_loop(websocket), name="openhdo-command-receiver")
        stopper = asyncio.create_task(process_stop.wait(), name="openhdo-process-stop")
        publisher = asyncio.create_task(self._publish_states(websocket, session_stop), name="openhdo-state-publisher")
        try:
            done, _ = await asyncio.wait((receiver, stopper), return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                await receiver
        finally:
            session_stop.set()
            tasks = (receiver, stopper, publisher)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.boundary.driver.disconnect()

    async def _receive_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            await self._receive(websocket, raw)

    async def _receive(self, websocket: Any, raw: str | bytes) -> None:
        try:
            message = Envelope.from_json(raw)
        except ProtocolError as error:
            self.logger.warning("rejected invalid server envelope: %s", error)
            return
        if message.type == "discovery.start":
            try:
                messages = await self.boundary.handle_discovery(message)
            except (KeyError, TypeError, ValueError) as error:
                self.logger.warning("rejected invalid discovery.start: %s", error)
                return
            await self._send_many(websocket, messages)
            return
        if message.type == "pairing.start":
            try:
                messages = await self.boundary.handle_pairing(message)
            except (KeyError, TypeError, ValueError) as error:
                self.logger.warning("rejected invalid pairing.start: %s", error)
                return
            await self._send_many(websocket, messages)
            return
        if not message.type.startswith("light.command."):
            self.logger.warning("ignored unsupported server message type: %s", message.type)
            return
        try:
            result = await self.boundary.handle(message)
        except (KeyError, TypeError, ValueError) as error:
            self.logger.warning("rejected invalid Light command: %s", error)
            return
        await self._send(websocket, result)
        if result.payload.get("status") == "applied":
            state = result.payload.get("state")
            if isinstance(state, Mapping):
                await self._send(
                    websocket,
                    Envelope(type="light.state.reported", source=self.boundary.config.id, payload=state),
                )

    async def _publish_states(self, websocket: Any, stop: asyncio.Event) -> None:
        delay = self.reconnect_initial_s
        unsubscribe = None
        while not stop.is_set():
            if not self.boundary.control_enabled:
                await _wait_or_stop(stop, 0.25)
                continue
            try:
                await self.boundary.driver.connect()

                async def publish(state: Any) -> None:
                    await self._send(websocket, self.boundary.state(state))

                unsubscribe = await self.boundary.driver.subscribe_state(self.boundary.device_id, publish)
                await publish(await self.boundary.driver.poll_state(self.boundary.device_id))
                delay = self.reconnect_initial_s
                while not await _wait_or_stop(stop, self.state_poll_interval_s):
                    await publish(await self.boundary.driver.poll_state(self.boundary.device_id))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = str(error)
                self.logger.warning("Tuya device health failure; reconnecting: %s", error)
                delay = min(delay * 2, self.reconnect_max_s)
            finally:
                if unsubscribe is not None:
                    await unsubscribe()
                    unsubscribe = None
                await self.boundary.driver.disconnect()
            if not await _wait_or_stop(stop, delay):
                return

    async def _send(self, websocket: Any, message: Envelope) -> None:
        async with self._send_lock:
            await websocket.send(message.to_json())

    async def _send_many(self, websocket: Any, messages: tuple[Envelope, ...]) -> None:
        for message in messages:
            await self._send(websocket, message)


async def _wait_or_stop(stop: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True
