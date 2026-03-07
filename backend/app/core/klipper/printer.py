"""
backend/app/printers/klipper/printer.py
=========================================
KlipperPrinter — the top-level adapter class.

This is the "public face" of the Klipper integration.  The printer manager
(the thing that tracks all connected printers) only ever talks to this class
through the BasePrinter interface.

Responsibilities:
  1. Own a MoonrakerClient and handle connect/disconnect
  2. Subscribe to the right printer objects on connect
  3. Handle state notifications → call _emit() with updated PrinterState
  4. Implement the print control commands (pause/resume/cancel)
  5. Implement upload_and_print via KlipperFileTransfer

DESIGN PATTERN (for C# devs):
  This is like an Adapter pattern — KlipperPrinter adapts the
  Moonraker-specific API into the BasePrinter interface the rest of the
  app expects, similar to how you'd wrap a 3rd-party SDK behind your own
  IService interface.
"""

import asyncio
import logging
from typing import Callable, Optional

from backend.app.core.printer_base import BasePrinter, PrinterState, PrinterStatus, PrinterType
from backend.app.core.klipper.client import MoonrakerClient
from backend.app.core.klipper.file_transfer import KlipperFileTransfer
from backend.app.core.klipper.state import build_printer_state, merge_status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Printer object subscriptions
# ---------------------------------------------------------------------------

# These are the Moonraker printer objects we want to subscribe to.
# Moonraker will push updates whenever any of these change.
# None means "subscribe to all fields of this object".
SUBSCRIBED_OBJECTS = {
    "print_stats": None,        # filename, state, print_duration, total_duration
    "display_status": None,     # progress (0.0–1.0)
    "extruder": None,           # temperature, target
    "heater_bed": None,         # temperature, target
    "fan": None,                # speed
    "toolhead": None,           # position, max_velocity
    "virtual_sdcard": None,     # file_position, progress
}


class KlipperPrinter(BasePrinter):
    """
    Adapter for a Klipper/Moonraker printer.

    Construction:
        printer = KlipperPrinter(
            printer_id="uuid-from-db",
            name="Voron 2.4",
            host="192.168.1.50",
            port=7125,
            on_state_change=my_broadcast_function,
            camera_url="http://192.168.1.50:8080/?action=stream",
        )
        await printer.connect()

    All state changes are pushed to on_state_change automatically.
    """

    def __init__(
        self,
        printer_id: str,
        name: str,
        host: str,
        on_state_change: Callable[[PrinterState], None],
        port: int = 7125,
        api_key: Optional[str] = None,
        camera_url: Optional[str] = None,
        upload_subfolder: str = "printbuddy",  # files go into gcodes/printbuddy/
    ):
        super().__init__(printer_id=printer_id, name=name, on_state_change=on_state_change)

        self.host = host
        self.port = port
        self.api_key = api_key
        self.camera_url = camera_url

        # Last full status snapshot — we merge incremental updates into this
        # so build_printer_state() always gets a complete picture.
        self._status_snapshot: dict = {}

        # Klippy's top-level state ("ready", "startup", "shutdown", …)
        self._klippy_state: str = "disconnected"

        # MoonrakerClient handles the WebSocket connection
        self._client = MoonrakerClient(
            host=host,
            port=port,
            api_key=api_key,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        # File transfer helper for uploads
        self._transfer = KlipperFileTransfer(
            host=host,
            port=port,
            api_key=api_key,
            upload_path=upload_subfolder,
        )

    # -----------------------------------------------------------------------
    # BasePrinter lifecycle
    # -----------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Register notification handlers then start the connection loop.
        The actual socket connect happens asynchronously in the background.
        """
        # Register for the notifications we care about
        self._client.on_notification("notify_status_update", self._on_status_update)
        self._client.on_notification("notify_klippy_ready", self._on_klippy_ready)
        self._client.on_notification("notify_klippy_shutdown", self._on_klippy_shutdown)
        self._client.on_notification("notify_klippy_disconnected", self._on_klippy_disconnected)

        # Start the connection loop (non-blocking — returns immediately)
        await self._client.connect()
        self._log(f"Connection loop started for {self.host}:{self.port}")

    async def disconnect(self) -> None:
        """Cleanly shut down the WebSocket client."""
        await self._client.disconnect()
        self._log("Disconnected")

    # -----------------------------------------------------------------------
    # BasePrinter print control
    # -----------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._klippy_state == "ready"

    async def pause_print(self) -> bool:
        """Send pause command to Moonraker."""
        try:
            await self._client.send_request("printer.print.pause")
            self._log("Print paused")
            return True
        except Exception as exc:
            self._log(f"Failed to pause: {exc}", "ERROR")
            return False

    async def resume_print(self) -> bool:
        """Send resume command to Moonraker."""
        try:
            await self._client.send_request("printer.print.resume")
            self._log("Print resumed")
            return True
        except Exception as exc:
            self._log(f"Failed to resume: {exc}", "ERROR")
            return False

    async def cancel_print(self) -> bool:
        """Send cancel command to Moonraker."""
        try:
            await self._client.send_request("printer.print.cancel")
            self._log("Print cancelled")
            return True
        except Exception as exc:
            self._log(f"Failed to cancel: {exc}", "ERROR")
            return False

    async def upload_and_print(
        self,
        filename: str,
        file_bytes: bytes,
        start_immediately: bool = True,
    ) -> bool:
        """
        Upload a gcode file and optionally start the print.

        We separate the upload from the print-start so the UI can show
        upload progress separately from print-start confirmation.
        (For Phase 1 we do them sequentially; Phase 2 can add WS progress.)
        """
        self._log(f"Uploading {filename} ({len(file_bytes)} bytes)")

        # Step 1: upload the file
        uploaded = await self._transfer.upload(
            filename=filename,
            file_bytes=file_bytes,
            print_after=False,  # We'll trigger start separately for clarity
        )
        if not uploaded:
            self._log(f"Upload of {filename} failed", "ERROR")
            return False

        # Step 2: optionally start printing
        if start_immediately:
            started = await self._transfer.start_print(filename)
            if not started:
                self._log(f"File uploaded but print start failed for {filename}", "WARNING")
                return False
            self._log(f"Print started: {filename}")

        return True

    # -----------------------------------------------------------------------
    # Additional Klipper-specific commands
    # -----------------------------------------------------------------------

    async def emergency_stop(self) -> bool:
        """
        Send an emergency stop (FIRMWARE_RESTART equivalent).
        This immediately halts all motion and heaters.
        Use with caution — the printer will need to be re-homed.
        """
        try:
            await self._client.send_request("printer.emergency_stop")
            return True
        except Exception as exc:
            self._log(f"Emergency stop failed: {exc}", "ERROR")
            return False

    async def firmware_restart(self) -> bool:
        """
        Restart the Klipper firmware (useful when Klippy is in error state).
        Equivalent to typing FIRMWARE_RESTART in the console.
        """
        try:
            await self._client.send_request("printer.firmware_restart")
            return True
        except Exception as exc:
            self._log(f"Firmware restart failed: {exc}", "ERROR")
            return False

    async def set_temperature(self, heater: str, temp: float) -> bool:
        """
        Set a heater temperature.

        Args:
            heater: "extruder" or "heater_bed" (or "extruder1" etc.)
            temp:   Target temperature in °C (0 to turn off)
        """
        try:
            # Moonraker uses GCode commands for temperature control
            if heater == "heater_bed":
                gcode = f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={temp}"
            else:
                gcode = f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={temp}"

            await self._client.send_request(
                "printer.gcode.script",
                params={"script": gcode},
            )
            return True
        except Exception as exc:
            self._log(f"Set temperature failed: {exc}", "ERROR")
            return False

    async def list_macros(self) -> list[str]:
        """Return user-defined gcode macros from Moonraker."""
        try:
            result = await self._client.send_request("printer.objects.list")
            objects = result.get("objects", [])
            macros = []
            skip = {"PAUSE", "RESUME", "CANCEL_PRINT", "M104", "M109", "M140", "M190", "M117", "M600"}
            for obj in objects:
                if obj.startswith("gcode_macro "):
                    name = obj[len("gcode_macro "):]
                    if not name.startswith("_") and name not in skip:
                        macros.append(name)
            return sorted(macros)
        except Exception as exc:
            self._log(f"list_macros failed: {exc}", "ERROR")
            return []

    async def run_macro(self, macro_name: str) -> bool:
        """Run a gcode macro by name."""
        # Sanitize: only allow alphanumeric + underscore
        import re
        if not re.match(r'^[A-Z0-9_]+$', macro_name.upper()):
            self._log(f"Invalid macro name: {macro_name}", "ERROR")
            return False
        try:
            await self._client.send_request(
                "printer.gcode.script",
                params={"script": macro_name.upper()},
            )
            return True
        except Exception as exc:
            self._log(f"run_macro {macro_name} failed: {exc}", "ERROR")
            return False

    async def list_macros(self) -> list[str]:
        """Return user-defined gcode macros from Moonraker."""
        try:
            result = await self._client.send_request("printer.objects.list")
            objects = result.get("objects", [])
            macros = []
            skip = {"PAUSE", "RESUME", "CANCEL_PRINT", "M104", "M109", "M140", "M190", "M117", "M600"}
            for obj in objects:
                if obj.startswith("gcode_macro "):
                    name = obj[len("gcode_macro "):]
                    if not name.startswith("_") and name not in skip:
                        macros.append(name)
            return sorted(macros)
        except Exception as exc:
            self._log(f"list_macros failed: {exc}", "ERROR")
            return []

    async def run_macro(self, macro_name: str) -> bool:
        """Run a gcode macro by name."""
        # Sanitize: only allow alphanumeric + underscore
        import re
        if not re.match(r'^[A-Z0-9_]+$', macro_name.upper()):
            self._log(f"Invalid macro name: {macro_name}", "ERROR")
            return False
        try:
            await self._client.send_request(
                "printer.gcode.script",
                params={"script": macro_name.upper()},
            )
            return True
        except Exception as exc:
            self._log(f"run_macro {macro_name} failed: {exc}", "ERROR")
            return False

    async def list_macros(self) -> list[str]:
        """Return user-defined gcode macros from Moonraker."""
        try:
            result = await self._client.send_request("printer.objects.list")
            objects = result.get("objects", [])
            macros = []
            skip = {"PAUSE", "RESUME", "CANCEL_PRINT", "M104", "M109", "M140", "M190", "M117", "M600"}
            for obj in objects:
                if obj.startswith("gcode_macro "):
                    name = obj[len("gcode_macro "):]
                    if not name.startswith("_") and name not in skip:
                        macros.append(name)
            return sorted(macros)
        except Exception as exc:
            self._log(f"list_macros failed: {exc}", "ERROR")
            return []

    async def run_macro(self, macro_name: str) -> bool:
        """Run a gcode macro by name."""
        # Sanitize: only allow alphanumeric + underscore
        import re
        if not re.match(r'^[A-Z0-9_]+$', macro_name.upper()):
            self._log(f"Invalid macro name: {macro_name}", "ERROR")
            return False
        try:
            await self._client.send_request(
                "printer.gcode.script",
                params={"script": macro_name.upper()},
            )
            return True
        except Exception as exc:
            self._log(f"run_macro {macro_name} failed: {exc}", "ERROR")
            return False

    async def start_print(self, filename: str) -> bool:
        """Start printing an already-uploaded file by its path on the printer."""
        try:
            started = await self._transfer.start_print(filename)
            if started:
                self._log(f"Reprint started: {filename}")
            return started
        except Exception as exc:
            self._log(f"start_print failed for {filename}: {exc}", "ERROR")
            return False

    async def list_files(self) -> list[dict]:
        """Return the list of gcode files on this printer."""
        return await self._transfer.list_files()

    # -----------------------------------------------------------------------
    # Moonraker notification handlers (private)
    # -----------------------------------------------------------------------

    def _on_connected(self) -> None:
        """
        Called by MoonrakerClient when the WebSocket connects.
        We need to do async work here (subscribe to objects, fetch initial
        state), so we schedule a coroutine on the event loop.
        """
        asyncio.create_task(self._post_connect_setup())

    def _on_disconnected(self) -> None:
        """
        Called by MoonrakerClient when the socket drops.
        Mark the printer offline immediately so the UI reflects reality.
        """
        self._klippy_state = "disconnected"
        self._status_snapshot = {}
        offline_state = PrinterState(
            printer_id=self.printer_id,
            printer_type=PrinterType.KLIPPER,
            status=PrinterStatus.OFFLINE,
            camera_url=self.camera_url,
        )
        self._emit(offline_state)

    async def _on_klippy_ready(self, params: list) -> None:
        """Klippy finished starting — fetch fresh state and resubscribe."""
        self._log("Klippy is ready")
        self._klippy_state = "ready"
        await self._subscribe_and_fetch()

    async def _on_klippy_shutdown(self, params: list) -> None:
        """Klippy has shut down (crash or intentional stop)."""
        self._log("Klippy shutdown", "WARNING")
        self._klippy_state = "shutdown"
        # Emit error state immediately
        error_state = build_printer_state(
            printer_id=self.printer_id,
            printer_name=self.name,
            status_data=self._status_snapshot,
            klippy_state="shutdown",
            camera_url=self.camera_url,
        )
        self._emit(error_state)

    async def _on_klippy_disconnected(self, params: list) -> None:
        """Moonraker lost its connection to Klippy (different from WS disconnect)."""
        self._log("Klippy disconnected from Moonraker", "WARNING")
        self._klippy_state = "disconnected"
        offline_state = PrinterState(
            printer_id=self.printer_id,
            printer_type=PrinterType.KLIPPER,
            status=PrinterStatus.OFFLINE,
            camera_url=self.camera_url,
        )
        self._emit(offline_state)

    async def _on_status_update(self, params: list) -> None:
        """
        Handle notify_status_update notifications.

        Moonraker sends these as:
            params = [{ "extruder": {"temperature": 215.3}, ... }, eventtime]

        The first element is a dict of changed objects/fields.
        We merge it into our full snapshot and rebuild the state.
        """
        if not params or not isinstance(params[0], dict):
            return

        delta = params[0]

        # Merge partial update into our full snapshot
        self._status_snapshot = merge_status(self._status_snapshot, delta)

        # Rebuild and emit the unified state
        new_state = build_printer_state(
            printer_id=self.printer_id,
            printer_name=self.name,
            status_data=self._status_snapshot,
            klippy_state=self._klippy_state,
            camera_url=self.camera_url,
        )
        self._emit(new_state)

    # -----------------------------------------------------------------------
    # Post-connect setup
    # -----------------------------------------------------------------------

    async def _post_connect_setup(self) -> None:
        """
        Run after the WebSocket connects:
          1. Fetch printer.info to get Klippy state
          2. Subscribe to printer objects
          3. Fetch current state snapshot
          4. Emit initial state

        This runs as an asyncio task so it doesn't block the connection loop.
        """
        try:
            # Step 1: check Klippy state
            info = await self._client.send_request("printer.info")
            self._klippy_state = info.get("klippy_state", "ready")
            self._log(f"Klippy state: {self._klippy_state}")

            # Step 2 & 3: subscribe and get initial snapshot
            await self._subscribe_and_fetch()

        except Exception as exc:
            self._log(f"Post-connect setup failed: {exc}", "ERROR")

    async def _subscribe_and_fetch(self) -> None:
        """
        Subscribe to printer objects and emit initial state.
        Called both on first connect and when Klippy becomes ready.
        """
        try:
            # Subscribe returns the current state of all objects
            response = await self._client.subscribe_objects(SUBSCRIBED_OBJECTS)

            # response.status is the initial snapshot dict
            initial_status = response.get("status", {}) if isinstance(response, dict) else {}
            self._status_snapshot = initial_status

            # Build and emit the initial state
            initial_state = build_printer_state(
                printer_id=self.printer_id,
                printer_name=self.name,
                status_data=self._status_snapshot,
                klippy_state=self._klippy_state,
                camera_url=self.camera_url,
            )
            self._emit(initial_state)
            self._log(f"Initial state: {initial_state.status.value}")

        except Exception as exc:
            self._log(f"Failed to subscribe to objects: {exc}", "ERROR")
