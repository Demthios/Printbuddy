"""
backend/app/printers/base.py
============================
Abstract base class (interface) that ALL printer adapters must implement.

Think of this like a C# interface — both BambuPrinter and KlipperPrinter
must implement every method here. The rest of the app (queue, archive,
notifications, WebSocket broadcaster) talks ONLY to this interface, so
it doesn't care whether the printer is a Bambu or a Klipper machine.

Python doesn't have interfaces like C# does, but ABC (Abstract Base Class)
gives us the same guarantee: if a subclass doesn't implement an abstract
method, Python raises a TypeError at import time.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Shared enums & data classes
# ---------------------------------------------------------------------------

class PrinterType(str, Enum):
    """Discriminator stored in the DB so we know which adapter to instantiate."""
    BAMBU = "bambu"
    KLIPPER = "klipper"


class PrinterStatus(str, Enum):
    """
    Unified printer state that the frontend understands.
    Both Bambu and Klipper states get mapped into this enum.
    """
    IDLE = "idle"           # Online, nothing printing
    PRINTING = "printing"   # Actively printing
    PAUSED = "paused"       # Print paused
    FINISHED = "finished"   # Print just completed (transitional)
    ERROR = "error"         # Klippy error / Bambu HMS error
    OFFLINE = "offline"     # Cannot reach the printer


@dataclass
class Temperature:
    """A temperature reading with current value and target setpoint."""
    actual: float = 0.0   # °C — what the sensor reads right now
    target: float = 0.0   # °C — what we asked it to heat to (0 = off)


@dataclass
class ExtruderTemp:
    """Temperature reading for a single extruder/tool in a multi-extruder setup."""
    tool_name: str = ""        # "T0", "T1", "T2"
    moonraker_name: str = ""   # "extruder", "extruder1", "extruder2"
    actual: float = 0.0
    target: float = 0.0


@dataclass
class PrintProgress:
    """Everything the UI needs to draw a progress bar and ETA."""
    filename: str = ""
    percent: float = 0.0        # 0–100
    elapsed_seconds: int = 0
    remaining_seconds: int = 0  # 0 = unknown


@dataclass
class PrinterState:
    """
    The canonical, printer-agnostic state object.

    The WebSocket broadcaster serialises this to JSON and pushes it to the
    frontend.  Both adapters fill in this dataclass — fields that don't apply
    (e.g. AMS on Klipper) are left at their zero values.
    """
    printer_id: str = ""
    printer_type: PrinterType = PrinterType.KLIPPER
    status: PrinterStatus = PrinterStatus.OFFLINE

    # Temperatures
    hotend: Temperature = field(default_factory=Temperature)
    bed: Temperature = field(default_factory=Temperature)
    extruders: list = field(default_factory=list)   # list[ExtruderTemp]
    active_extruder: str = ""                        # moonraker name e.g. "extruder1"

    # Print progress (only meaningful while status == PRINTING or PAUSED)
    progress: PrintProgress = field(default_factory=PrintProgress)

    # Fan speeds 0–100 (Klipper uses 0–1 internally, we normalise to %)
    fan_speed: float = 0.0

    # Camera stream URL — populated by the adapter, proxied or direct
    camera_url: Optional[str] = None

    # Human-readable error message when status == ERROR
    error_message: Optional[str] = None

    # Raw data blob — adapter-specific extras the frontend can optionally show
    # (e.g. Bambu AMS slots, Klipper MCU temperature)
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class BasePrinter(ABC):
    """
    Every printer adapter (Bambu, Klipper, …) inherits from this class and
    implements the abstract methods below.

    Lifecycle:
        1.  __init__  — called when the printer record is loaded from DB
        2.  connect() — called by the printer manager on startup / re-connect
        3.  [state callbacks fire whenever the printer sends data]
        4.  disconnect() — called on shutdown or printer removal
    """

    def __init__(
        self,
        printer_id: str,
        name: str,
        on_state_change: Callable[[PrinterState], None],
    ):
        """
        Args:
            printer_id:      UUID string from the DB row
            name:            Human-friendly name shown in the UI
            on_state_change: Callback the adapter MUST call every time
                             state changes.  The manager forwards this to
                             the WebSocket broadcaster.
        """
        self.printer_id = printer_id
        self.name = name
        self._on_state_change = on_state_change

        # Subclasses maintain their own _state and call _emit() when it changes
        self._state = PrinterState(
            printer_id=printer_id,
            status=PrinterStatus.OFFLINE,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """
        Open the connection to the printer (MQTT for Bambu,
        WebSocket JSON-RPC for Klipper/Moonraker).

        Must be non-blocking — use asyncio tasks for background polling.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Cleanly close all connections and cancel background tasks.
        Called on shutdown or when the printer is removed from the DB.
        """
        ...

    # ------------------------------------------------------------------
    # Print control
    # ------------------------------------------------------------------

    @abstractmethod
    async def pause_print(self) -> bool:
        """Pause the current print.  Returns True on success."""
        ...

    @abstractmethod
    async def resume_print(self) -> bool:
        """Resume a paused print.  Returns True on success."""
        ...

    @abstractmethod
    async def cancel_print(self) -> bool:
        """Cancel the current print.  Returns True on success."""
        ...

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    @abstractmethod
    async def upload_and_print(
        self,
        filename: str,
        file_bytes: bytes,
        start_immediately: bool = True,
    ) -> bool:
        """
        Upload a file to the printer and optionally start printing it.

        Args:
            filename:         The filename to store on the printer (e.g. "part.gcode")
            file_bytes:       Raw file content
            start_immediately: If True, start the print right after upload

        Returns:
            True if the operation succeeded.
        """
        ...

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Return the last known state.  Safe to call from any coroutine."""
        return self._state

    # ------------------------------------------------------------------
    # Internal helpers (not abstract — subclasses just call these)
    # ------------------------------------------------------------------

    def _emit(self, new_state: PrinterState) -> None:
        """
        Update internal state and fire the on_state_change callback.
        Subclasses call this whenever they parse new data from the printer.
        """
        self._state = new_state
        self._on_state_change(new_state)

    def _log(self, msg: str, level: str = "INFO") -> None:
        """
        Lightweight logger that prefixes the printer name.
        We use Python's built-in print here so it shows in uvicorn stdout;
        swap for `import logging` if you prefer structured logs.
        """
        print(f"[{level}] [{self.name}] {msg}")
