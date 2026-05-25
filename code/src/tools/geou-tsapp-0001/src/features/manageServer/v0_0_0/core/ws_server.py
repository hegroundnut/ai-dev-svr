"""
WebSocket server for brainBox connections.

brainBox nodes connect via persistent WebSocket to manageServer.
All bidirectional communication flows through this single connection:
  - brainBox → manageServer: event messages (heartbeat, drone_report, trajectory_report)
  - manageServer → brainBox: req messages (commands), expecting resp replies

The server runs its own asyncio event loop in a daemon thread,
bridging sync platform threads with the async WS handlers via concurrent.futures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger("manageServer.ws_server")

EventCallback = Callable[[str, Dict[str, Any]], None]
OfflineCallback = Callable[[str], None]


class BrainBoxWSManager:
    """
    WebSocket server for brainBox connections.

    Runs its own asyncio event loop in a daemon thread.
    Provides a synchronous ``send_request()`` interface for use from
    platform threads (EdgeManager methods).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 15002,
        on_event: Optional[EventCallback] = None,
        on_box_offline: Optional[OfflineCallback] = None,
        request_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_box_offline = on_box_offline
        self._request_timeout = request_timeout

        self._lock = threading.Lock()
        self._connections: Dict[str, WebSocketServerProtocol] = {}
        self._pending: Dict[str, tuple[Future, str]] = {}  # request_id → (future, box_id)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[websockets.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the WS server in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop, name="ws-server", daemon=True
        )
        self._thread.start()
        logger.info("WS server starting on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Stop the WS server and clean up."""
        self._running = False
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._lock:
            for req_id, (future, _) in list(self._pending.items()):
                if not future.done():
                    future.set_exception(ConnectionError("WS server shutting down"))
                del self._pending[req_id]
            self._connections.clear()
        logger.info("WS server stopped")

    def _run_event_loop(self) -> None:
        """Entry point for the daemon thread — runs the asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            logger.exception("WS server event loop crashed")
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        """Async: bind and serve until stopped."""
        self._server = await websockets.serve(
            self._handler, self._host, self._port,
            ping_interval=20, ping_timeout=10,
        )
        logger.info("WS server listening on ws://%s:%d", self._host, self._port)
        try:
            await self._server.wait_closed()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    #  Connection handler
    # ------------------------------------------------------------------

    async def _handler(self, ws: WebSocketServerProtocol, path: str = "/") -> None:
        """Handle a single brainBox WebSocket connection."""
        box_id = None
        try:
            # Wait for auth message
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)
            if msg.get("action") != "auth":
                await ws.close(4000, "First message must be auth")
                return
            box_id = msg.get("payload", {}).get("box_id", "")
            if not box_id:
                await ws.close(4000, "Missing box_id in auth")
                return
        except asyncio.TimeoutError:
            await ws.close(4000, "Auth timeout")
            return
        except Exception:
            logger.exception("Auth error")
            return

        # Register connection — reject if box_id is already connected
        with self._lock:
            if box_id in self._connections:
                logger.warning("BrainBox %s attempted duplicate connection — rejected", box_id)
                await ws.close(4002, f"BrainBox {box_id} is already connected")
                box_id = None  # don't let finally-block cleanup touch the real connection
                return
            self._connections[box_id] = ws

        logger.info("BrainBox %s connected via WS", box_id)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from %s", box_id)
                    continue
                self._process_message(box_id, msg)
        except websockets.ConnectionClosed:
            logger.info("BrainBox %s disconnected", box_id)
        finally:
            self._cleanup_connection(box_id)

    def _process_message(self, box_id: str, msg: Dict[str, Any]) -> None:
        """Dispatch an incoming message from a brainBox (runs on event loop thread)."""
        msg_type = msg.get("type", "")

        if msg_type == "resp":
            request_id = msg.get("request_id", "")
            with self._lock:
                entry = self._pending.pop(request_id, None)
            if entry is None:
                logger.debug("No pending future for request_id=%s", request_id)
                return
            future, _ = entry
            if future.done():
                return
            if msg.get("error"):
                future.set_exception(RuntimeError(str(msg["error"])))
            else:
                future.set_result(msg.get("payload", {}))

        elif msg_type == "event":
            action = msg.get("action", "")
            payload = msg.get("payload", {})
            cb = self._on_event
            if cb and action:
                cb(action, payload)

    def _cleanup_connection(self, box_id: str) -> None:
        """Cancels all pending futures for a disconnected brainBox (event loop thread)."""
        with self._lock:
            self._connections.pop(box_id, None)
            cancelled: list[str] = []
            for req_id, (future, bid) in list(self._pending.items()):
                if bid == box_id:
                    if not future.done():
                        future.set_exception(ConnectionError(f"BrainBox {box_id} disconnected"))
                    cancelled.append(req_id)
            for req_id in cancelled:
                del self._pending[req_id]

        offline_cb = self._on_box_offline
        if offline_cb:
            offline_cb(box_id)

    # ------------------------------------------------------------------
    #  Synchronous send_request (called from platform threads)
    # ------------------------------------------------------------------

    def send_request(
        self,
        box_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Send a request to a brainBox and wait for its response.

        Thread-safe. Called from EdgeManager's methods (platform thread).
        Blocks until the response arrives or timeout.
        """
        if not self._loop:
            raise ConnectionError("WS server not started")

        ws: Optional[WebSocketServerProtocol] = None
        with self._lock:
            ws = self._connections.get(box_id)
        if ws is None:
            raise ConnectionError(f"BrainBox {box_id} not connected via WS")

        request_id = str(uuid.uuid4())
        future: Future = Future()
        with self._lock:
            self._pending[request_id] = (future, box_id)

        msg = {
            "type": "req",
            "request_id": request_id,
            "action": action,
            "payload": payload or {},
        }
        raw = json.dumps(msg, ensure_ascii=False, default=str)

        try:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, raw), self._loop)
        except Exception as e:
            with self._lock:
                self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(e)
            raise

        try:
            result: Dict[str, Any] = future.result(timeout=timeout or self._request_timeout)
            return result
        except TimeoutError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise

    async def _safe_send(self, ws: WebSocketServerProtocol, raw: str) -> None:
        """Send a message on the event loop (scheduled via run_coroutine_threadsafe)."""
        try:
            await ws.send(raw)
        except websockets.ConnectionClosed as e:
            logger.error("Failed to send to brainBox: %s", e)

    # ------------------------------------------------------------------
    #  Query
    # ------------------------------------------------------------------

    def is_connected(self, box_id: str) -> bool:
        with self._lock:
            return box_id in self._connections

    @property
    def connected_boxes(self) -> list[str]:
        with self._lock:
            return list(self._connections.keys())
