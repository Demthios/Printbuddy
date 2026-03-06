"""
backend/app/printers/klipper/client.py
=======================================
Low-level Moonraker WebSocket client.

Moonraker speaks JSON-RPC 2.0 over a plain WebSocket on port 7125.
This client handles:
  - Connection + automatic reconnect with exponential back-off
  - Sending JSON-RPC requests and awaiting their responses (by request ID)
  - Routing server-side event notifications to registered callbacks
  - Subscribing to printer object updates (temps, print stats, fan, etc.)

ANALOGY FOR C# DEVS:
  Think of this like a SignalR HubConnection with typed method calls.
  - send_request()  ≈  hubConnection.InvokeAsync("Method", args)
  - subscribe()     ≈  hubConnection.On("Event", handler)
"""

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets  # pip install websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class MoonrakerClient:
    """
    Async WebSocket client for Moonraker's JSON-RPC API.

    Usage (inside an async context):
        client = MoonrakerClient(host="192.168.1.50", port=7125)
        await client.connect()
        result = await client.send_request("printer.info")
        await client.disconnect()
    """

    # How long to wait between reconnect attempts (seconds).
    # We double this on each failure up to MAX_BACKOFF.
    INITIAL_BACKOFF = 2
    MAX_BACKOFF = 60

    def __init__(
        self,
        host: str,
        port: int = 7125,
        api_key: Optional[str] = None,
        on_connected: Optional[Callable[[], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key  # Optional — most home installs don't need this

        # Callbacks the KlipperPrinter adapter registers
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        # Map: method_name  →  list of async callback functions
        # e.g. "notify_status_update" → [self._handle_status]
        self._notification_handlers: dict[str, list[Callable]] = {}

        # Map: request_id → asyncio.Future
        # When a response arrives we resolve the matching Future.
        self._pending_requests: dict[int, asyncio.Future] = {}

        self._ws = None              # Active websockets connection
        self._recv_task = None       # Background task that reads from the socket
        self._reconnect_task = None  # Background task that manages reconnection
        self._request_counter = 0    # Monotonically increasing request IDs
        self._connected = False
        self._should_run = False     # Set to False to stop the reconnect loop

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Start the connection loop.  Returns immediately; connection happens
        in the background.  Use the on_connected callback to know when ready.
        """
        self._should_run = True
        self._reconnect_task = asyncio.create_task(self._connection_loop())

    async def disconnect(self) -> None:
        """Stop reconnecting and close the socket cleanly."""
        self._should_run = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        self._connected = False

    def on_notification(self, method: str, handler: Callable) -> None:
        """
        Register a callback for a Moonraker server notification.

        Moonraker pushes notifications as JSON-RPC "requests" with no id.
        Common ones:
          "notify_status_update"   — printer object changes (temps, progress…)
          "notify_klippy_ready"    — Klipper finished starting up
          "notify_klippy_shutdown" — Klipper crashed or was reset
          "notify_proc_stat_update"— CPU/memory stats

        Args:
            method:  The JSON-RPC method name (string)
            handler: async def handler(params: list) — receives the params array
        """
        if method not in self._notification_handlers:
            self._notification_handlers[method] = []
        self._notification_handlers[method].append(handler)

    async def send_request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> Any:
        """
        Send a JSON-RPC request to Moonraker and await the response.

        Args:
            method:  JSON-RPC method name, e.g. "printer.print.pause"
            params:  Optional dict of parameters
            timeout: Seconds to wait before raising TimeoutError

        Returns:
            The "result" field from the JSON-RPC response.

        Raises:
            RuntimeError  — if not connected
            TimeoutError  — if Moonraker doesn't respond in time
            RuntimeError  — if Moonraker returns an error response
        """
        if not self._connected or self._ws is None:
            raise RuntimeError("Not connected to Moonraker")

        # Build the JSON-RPC envelope
        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": req_id,
        }
        if params:
            payload["params"] = params

        # Create a Future that _recv_loop will resolve when the response arrives
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            await self._ws.send(json.dumps(payload))
            # Wait for the response, with timeout
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            del self._pending_requests[req_id]
            raise TimeoutError(f"Moonraker did not respond to '{method}' within {timeout}s")
        except Exception:
            self._pending_requests.pop(req_id, None)
            raise

    async def subscribe_objects(self, objects: dict[str, Any]) -> dict:
        """
        Subscribe to printer object state updates.

        Moonraker will now push "notify_status_update" whenever any of
        the requested objects change.

        Args:
            objects: Dict mapping object names to None (subscribe all fields)
                     or a list of field names.
                     Example: {"extruder": None, "heater_bed": None, "print_stats": None}

        Returns:
            The current state of all requested objects (initial snapshot).
        """
        return await self.send_request(
            "printer.objects.subscribe",
            params={"objects": objects},
        )

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -----------------------------------------------------------------------
    # Internal: connection loop with back-off
    # -----------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """
        Reconnect loop.  Tries to connect; on failure waits with exponential
        back-off then tries again.  Runs until disconnect() is called.
        """
        backoff = self.INITIAL_BACKOFF
        while self._should_run:
            try:
                await self._open_connection()
                backoff = self.INITIAL_BACKOFF  # Reset on successful connect
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    f"Moonraker connection to {self.host}:{self.port} failed: {exc}. "
                    f"Retrying in {backoff}s…"
                )
                if self._on_disconnected:
                    self._on_disconnected()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)

    async def _open_connection(self) -> None:
        """
        Actually open the WebSocket, subscribe to objects, and sit in the
        receive loop until the connection drops.
        """
        uri = f"ws://{self.host}:{self.port}/websocket"
        if self.api_key:
            uri += f"?token={self.api_key}"

        logger.info(f"Connecting to Moonraker at {uri}")

        # websockets.connect() is an async context manager
        async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            self._connected = True
            logger.info(f"Connected to Moonraker at {self.host}:{self.port}")

            if self._on_connected:
                self._on_connected()

            # Start receiving messages — this blocks until the socket closes
            await self._recv_loop()

        # Socket closed — clean up
        self._ws = None
        self._connected = False
        # Reject all pending requests so callers don't hang
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError("Disconnected from Moonraker"))
        self._pending_requests.clear()

    async def _recv_loop(self) -> None:
        """
        Read messages from the WebSocket forever (until connection closes).
        Each message is either:
          - A response to one of our requests  (has "id" field)
          - A server notification              (no "id" field, has "method" field)
        """
        async for raw_message in self._ws:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning(f"Received non-JSON from Moonraker: {raw_message[:200]}")
                continue

            if "id" in msg and msg["id"] is not None:
                # This is a response to one of our requests
                self._handle_response(msg)
            elif "method" in msg:
                # This is a server-pushed notification
                await self._handle_notification(msg)

    def _handle_response(self, msg: dict) -> None:
        """Resolve or reject the Future waiting for this response ID."""
        req_id = msg.get("id")
        future = self._pending_requests.pop(req_id, None)
        if future is None or future.done():
            return  # Response arrived after timeout — ignore

        if "error" in msg:
            err = msg["error"]
            future.set_exception(RuntimeError(f"Moonraker error {err.get('code')}: {err.get('message')}"))
        else:
            future.set_result(msg.get("result"))

    async def _handle_notification(self, msg: dict) -> None:
        """Dispatch a server notification to registered handlers."""
        method = msg.get("method", "")
        params = msg.get("params", [])
        handlers = self._notification_handlers.get(method, [])
        for handler in handlers:
            try:
                await handler(params)
            except Exception as exc:
                logger.error(f"Error in notification handler for '{method}': {exc}")

    # -----------------------------------------------------------------------
    # Internal: helpers
    # -----------------------------------------------------------------------

    def _next_id(self) -> int:
        """Thread-safe monotonic request ID."""
        self._request_counter += 1
        return self._request_counter
