"""
WebSocket client for brainBox → manageServer persistent connection.

Maintains a long-lived WS connection to the edge server.
All upstream (event) and downstream (req/resp) communication
flows through this single connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger("brainBox.ws_client")

RequestHandler = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class EdgeWSClient:
    """
    Persistent WebSocket client to manageServer.

    - Auto-reconnects with exponential backoff on disconnect
    - Sends ``auth`` event on each (re)connect with box_id
    - Provides ``send_event()`` for fire-and-forget upstream messages
    - Dispatches incoming ``req`` messages to the ``on_request`` handler
    """

    def __init__(
        self,
        ws_url: str,
        box_id: str,
        on_request: RequestHandler,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._ws_url = ws_url
        self._box_id = box_id
        self._on_request = on_request
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = False

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the connection loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        logger.info("EdgeWSClient started (url=%s box_id=%s)", self._ws_url, self._box_id)

    async def stop(self) -> None:
        """Stop the connection loop and close the socket."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("EdgeWSClient stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    #  Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        """Connect and reconnect with exponential backoff."""
        delay = self._reconnect_base_delay
        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    await self._send_auth(ws)
                    self._connected = True
                    delay = self._reconnect_base_delay  # reset on successful connect
                    logger.info("WS connected to %s", self._ws_url)

                    await self._handle_messages(ws)

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                logger.warning("WS disconnected from %s: %s", self._ws_url, e)
            except Exception:
                logger.exception("WS connection error to %s", self._ws_url)
            finally:
                self._connected = False
                self._ws = None

            if not self._running:
                break
            logger.info("WS reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect_max_delay)

    async def _send_auth(self, ws: WebSocketClientProtocol) -> None:
        """Send auth event on connect."""
        msg = {
            "type": "event",
            "action": "auth",
            "payload": {"box_id": self._box_id},
        }
        await ws.send(json.dumps(msg, ensure_ascii=False))

    async def _handle_messages(self, ws: WebSocketClientProtocol) -> None:
        """Receive loop: dispatch 'req' messages to handler, log others."""
        async for raw in ws:
            try:
                msg: Dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received invalid JSON from server")
                continue

            msg_type = msg.get("type", "")
            if msg_type == "req":
                await self._handle_request(ws, msg)

    async def _handle_request(self, ws: WebSocketClientProtocol, msg: Dict[str, Any]) -> None:
        """Process an incoming 'req' and send back a 'resp'."""
        request_id = msg.get("request_id", "")
        action = msg.get("action", "")
        payload = msg.get("payload", {})

        try:
            result = await self._on_request(action, payload)
            resp = {
                "type": "resp",
                "request_id": request_id,
                "action": action,
                "payload": result,
                "error": None,
            }
        except Exception as e:
            logger.exception("Error handling request action=%s", action)
            resp = {
                "type": "resp",
                "request_id": request_id,
                "action": action,
                "payload": {},
                "error": str(e),
            }

        try:
            await ws.send(json.dumps(resp, ensure_ascii=False, default=str))
        except websockets.ConnectionClosed:
            logger.warning("Failed to send resp for %s: connection closed", request_id)

    # ------------------------------------------------------------------
    #  Public: send event (fire-and-forget)
    # ------------------------------------------------------------------

    async def send_event(self, action: str, payload: Dict[str, Any]) -> None:
        """Send an upstream event (heartbeat, drone_report, etc.)."""
        if not self._ws or not self._connected:
            return
        msg = {
            "type": "event",
            "action": action,
            "payload": payload,
        }
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False, default=str))
        except websockets.ConnectionClosed:
            logger.debug("Failed to send event '%s': connection closed", action)
        except Exception:
            logger.exception("Failed to send event '%s'", action)
