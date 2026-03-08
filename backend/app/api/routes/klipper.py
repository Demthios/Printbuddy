"""
backend/app/api/routes/klipper.py
===================================
FastAPI router for Klipper/Moonraker printer management.

Endpoints:
  POST   /api/v1/klipper/printers                     — add a new Klipper printer
  GET    /api/v1/klipper/printers/{id}/status         — get current state
  POST   /api/v1/klipper/printers/{id}/pause          — pause print
  POST   /api/v1/klipper/printers/{id}/resume         — resume print
  POST   /api/v1/klipper/printers/{id}/cancel         — cancel print
  POST   /api/v1/klipper/printers/{id}/firmware-restart — restart Klipper
  POST   /api/v1/klipper/printers/{id}/upload         — upload gcode + optional print start
  GET    /api/v1/klipper/printers/{id}/files          — list files on printer
  POST   /api/v1/klipper/test-connection              — test Moonraker reachability
"""

import dataclasses
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_db
from backend.app.core.printer_base import PrinterStatus
from backend.app.models.printer import Printer
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/klipper", tags=["klipper"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AddKlipperPrinterRequest(BaseModel):
    name: str = Field(..., example="Voron 2.4 - Bay 1")
    host: str = Field(..., example="192.168.1.50")
    port: int = Field(default=7125)
    api_key: Optional[str] = Field(default=None)
    camera_url: Optional[str] = Field(default=None)
    upload_subfolder: str = Field(default="printbuddy")
    location: Optional[str] = Field(default=None)


class KlipperPrinterResponse(BaseModel):
    printer_id: int
    name: str
    host: str
    port: int
    camera_url: Optional[str]
    status: str


class PrintControlResponse(BaseModel):
    success: bool
    message: str


class UploadResponse(BaseModel):
    success: bool
    filename: str
    message: str


class FileListItem(BaseModel):
    filename: str
    size: int
    modified: float


class TestConnectionRequest(BaseModel):
    host: str
    port: int = 7125
    api_key: Optional[str] = None


class TestConnectionResponse(BaseModel):
    success: bool
    klippy_state: Optional[str]
    moonraker_version: Optional[str]
    message: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_klipper_printer(printer_id: int):
    """
    Look up a live KlipperPrinter instance from the manager.
    Raises 404 if not found or if it's a Bambu printer ID.
    """
    client = printer_manager.get_klipper_client(printer_id)
    if client is None:
        raise HTTPException(
            status_code=404,
            detail=f"Klipper printer {printer_id} not found or not connected. "
                   "Is it active and of type 'klipper'?"
        )
    return client


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/test-connection",
    response_model=TestConnectionResponse,
    summary="Test Moonraker reachability before adding a printer",
)
async def test_connection(body: TestConnectionRequest):
    """
    Attempt a one-shot connection to a Moonraker instance to verify
    it is reachable and Klippy is running.  Does not persist anything.
    """
    from backend.app.core.klipper.client import MoonrakerClient

    connected_event = __import__("asyncio").Event()
    client = MoonrakerClient(
        host=body.host,
        port=body.port,
        api_key=body.api_key,
        on_connected=lambda: connected_event.set(),
    )

    try:
        await client.connect()
        try:
            await __import__("asyncio").wait_for(connected_event.wait(), timeout=8)
        except __import__("asyncio").TimeoutError:
            return TestConnectionResponse(
                success=False,
                klippy_state=None,
                moonraker_version=None,
                message=f"Timed out connecting to {body.host}:{body.port} — is Moonraker running?",
            )

        info = await client.send_request("printer.info")
        return TestConnectionResponse(
            success=True,
            klippy_state=info.get("klippy_state"),
            moonraker_version=info.get("software_version"),
            message="Connected successfully",
        )
    except Exception as exc:
        return TestConnectionResponse(
            success=False,
            klippy_state=None,
            moonraker_version=None,
            message=str(exc),
        )
    finally:
        await client.disconnect()


@router.post(
    "/printers",
    response_model=KlipperPrinterResponse,
    summary="Add a new Klipper/Moonraker printer",
    status_code=201,
)
async def add_klipper_printer(
    body: AddKlipperPrinterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new Klipper printer, persist it to the DB, and connect
    immediately.  The printer will appear on the dashboard right away.

    Required fields: name, host
    Optional: port (default 7125), api_key, camera_url, upload_subfolder, location
    """
    # Check for duplicate host+port
    existing = await db.execute(
        select(Printer).where(
            Printer.moonraker_host == body.host,
            Printer.moonraker_port == body.port,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"A Klipper printer at {body.host}:{body.port} already exists",
        )

    # Create the DB row — Klipper printers use moonraker_host/port instead of
    # ip_address/serial_number/access_code which are Bambu-specific.
    # We fill the Bambu-required NOT NULL columns with placeholder values so
    # the existing schema constraint doesn't block us.
    printer = Printer(
        name=body.name,
        printer_type="klipper",
        # Bambu-required columns — not used for Klipper
        serial_number=f"klipper-{body.host}",
        ip_address=body.host,
        access_code="",
        nozzle_count=1,
        # Klipper columns
        moonraker_host=body.host,
        moonraker_port=body.port,
        moonraker_api_key=body.api_key,
        klipper_camera_url=body.camera_url,
        klipper_upload_subfolder=body.upload_subfolder or "printbuddy",
        # Common columns
        location=body.location,
        is_active=True,
        auto_archive=False,
        print_hours_offset=0.0,
        runtime_seconds=0,
        external_camera_enabled=False,
        plate_detection_enabled=False,
    )
    db.add(printer)
    await db.commit()
    await db.refresh(printer)

    # Connect immediately
    try:
        await printer_manager.connect_printer(printer)
        logger.info("Klipper printer %s (%s) connected", printer.id, printer.name)
    except Exception as exc:
        logger.warning(
            "Klipper printer %s added to DB but connection failed: %s", printer.id, exc
        )
        # Don't fail the request — printer is saved and will reconnect on restart

    # Get current status if connected
    state = printer_manager.get_status(printer.id)
    status_str = state.status.value if state else PrinterStatus.OFFLINE.value

    return KlipperPrinterResponse(
        printer_id=printer.id,
        name=printer.name,
        host=body.host,
        port=body.port,
        camera_url=body.camera_url,
        status=status_str,
    )


@router.get(
    "/printers/{printer_id}/status",
    summary="Get current Klipper printer state",
)
async def get_printer_status(printer_id: int):
    """
    Return the current state of the Klipper printer as a JSON object.
    This is a REST polling fallback — the WebSocket at /api/v1/ws gives
    real-time updates and is preferred for the dashboard.
    """
    client = _get_klipper_printer(printer_id)
    state = client.get_state()
    # dataclasses.asdict() deep-converts the dataclass to a plain dict
    result = dataclasses.asdict(state)
    # Convert enum values to strings so they JSON-serialize cleanly
    result["status"] = state.status.value
    result["printer_type"] = state.printer_type.value
    return result


@router.post(
    "/printers/{printer_id}/pause",
    response_model=PrintControlResponse,
    summary="Pause the current print",
)
async def pause_print(printer_id: int):
    """Send a pause command to the Klipper printer via Moonraker."""
    client = _get_klipper_printer(printer_id)
    success = await client.pause_print()
    return PrintControlResponse(
        success=success,
        message="Print paused" if success else "Failed to pause — is a print running?",
    )


@router.post(
    "/printers/{printer_id}/resume",
    response_model=PrintControlResponse,
    summary="Resume a paused print",
)
async def resume_print(printer_id: int):
    """Send a resume command to the Klipper printer via Moonraker."""
    client = _get_klipper_printer(printer_id)
    success = await client.resume_print()
    return PrintControlResponse(
        success=success,
        message="Print resumed" if success else "Failed to resume — is a print paused?",
    )


@router.post(
    "/printers/{printer_id}/cancel",
    response_model=PrintControlResponse,
    summary="Cancel the current print",
)
async def cancel_print(printer_id: int):
    """Send a cancel command to the Klipper printer via Moonraker."""
    client = _get_klipper_printer(printer_id)
    success = await client.cancel_print()
    return PrintControlResponse(
        success=success,
        message="Print cancelled" if success else "Failed to cancel",
    )


@router.post(
    "/printers/{printer_id}/firmware-restart",
    response_model=PrintControlResponse,
    summary="Restart Klipper firmware",
)
async def firmware_restart(printer_id: int):
    """
    Trigger FIRMWARE_RESTART on the Klipper printer.
    Use this to recover from error states (stepper faults, thermistor errors, etc.).
    The printer will need to re-home after this.
    """
    client = _get_klipper_printer(printer_id)
    success = await client.firmware_restart()
    return PrintControlResponse(
        success=success,
        message="Firmware restart sent" if success else "Failed to send firmware restart",
    )


@router.post(
    "/printers/{printer_id}/upload",
    response_model=UploadResponse,
    summary="Upload a gcode file and optionally start printing",
)
async def upload_file(
    printer_id: int,
    file: UploadFile = File(...),
    start_print: bool = Form(default=True),
):
    """
    Upload a .gcode file to the Klipper printer's gcodes directory
    and optionally start printing it immediately.

    The file is stored under the printer's configured upload_subfolder
    (default: gcodes/printbuddy/).
    """
    if not file.filename or not file.filename.endswith(".gcode"):
        raise HTTPException(
            status_code=400,
            detail="Only .gcode files are accepted for Klipper printers",
        )

    client = _get_klipper_printer(printer_id)
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    logger.info(
        "Uploading %s (%d bytes) to Klipper printer %d (start=%s)",
        file.filename, len(file_bytes), printer_id, start_print,
    )

    success = await client.upload_and_print(
        filename=file.filename,
        file_bytes=file_bytes,
        start_immediately=start_print,
    )

    if success:
        msg = "Uploaded and print started" if start_print else "Upload successful"
    else:
        msg = "Upload failed — check Moonraker logs for details"

    return UploadResponse(success=success, filename=file.filename, message=msg)


class StartPrintRequest(BaseModel):
    filename: str = Field(..., description="Path relative to gcodes root, e.g. 'printbuddy/part.gcode'")


@router.post(
    "/printers/{printer_id}/print",
    response_model=PrintControlResponse,
    summary="Start printing an already-uploaded file",
)
async def start_print(printer_id: int, body: StartPrintRequest):
    """
    Tell the printer to start printing a file that is already on its storage.
    Use this for reprinting without re-uploading.
    """
    client = _get_klipper_printer(printer_id)
    success = await client.start_print(body.filename)
    return PrintControlResponse(
        success=success,
        message="Print started" if success else f"Failed to start print for {body.filename}",
    )


@router.get(
    "/printers/{printer_id}/files",
    response_model=list[FileListItem],
    summary="List gcode files stored on the printer",
)
async def list_files(printer_id: int):
    """
    Return all gcode files in the printer's upload subfolder.
    Useful for building a reprint UI or checking what's already uploaded.
    """
    client = _get_klipper_printer(printer_id)
    raw_files = await client.list_files()

    result = []
    for f in raw_files:
        # Moonraker returns filename, size, modified — all we need
        result.append(FileListItem(
            filename=f.get("filename", ""),
            size=int(f.get("size", 0)),
            modified=float(f.get("modified", 0)),
        ))
    return result

class SetTemperatureRequest(BaseModel):
    heater: str = Field(..., example="extruder")  # "extruder" or "heater_bed"
    temperature: float = Field(..., ge=0, le=350)


@router.post(
    "/printers/{printer_id}/temperature",
    response_model=PrintControlResponse,
    summary="Set heater temperature",
)
async def set_temperature(printer_id: int, body: SetTemperatureRequest):
    """
    Set a heater target temperature.
    heater: "extruder" for nozzle, "heater_bed" for bed.
    temperature: 0 to turn off, otherwise target in °C.
    """
    client = _get_klipper_printer(printer_id)
    success = await client.set_temperature(body.heater, body.temperature)
    return PrintControlResponse(
        success=success,
        message=f"Temperature set to {body.temperature}°C" if success else "Failed to set temperature",
    )


class ChangeToolRequest(BaseModel):
    tool_index: int = Field(..., ge=0, le=9)


@router.post(
    "/printers/{printer_id}/tool",
    response_model=PrintControlResponse,
    summary="Switch active tool/extruder",
)
async def change_tool(printer_id: int, body: ChangeToolRequest):
    """Send T0/T1/T2... gcode to switch the active extruder."""
    client = _get_klipper_printer(printer_id)
    success = await client.change_tool(body.tool_index)
    return PrintControlResponse(
        success=success,
        message=f"Switched to T{body.tool_index}" if success else "Tool change failed",
    )


@router.post(
    "/printers/{printer_id}/emergency-stop",
    response_model=PrintControlResponse,
    summary="Emergency stop — halts all motion and heaters immediately",
)
async def emergency_stop(printer_id: int):
    """
    Trigger EMERGENCY_STOP on the Klipper printer.
    This immediately halts all motion and turns off all heaters.
    A firmware restart will be required to resume operation.
    """
    client = _get_klipper_printer(printer_id)
    success = await client.emergency_stop()
    return PrintControlResponse(
        success=success,
        message="Emergency stop triggered" if success else "Failed to trigger emergency stop",
    )


@router.get(
    "/printers/{printer_id}/macros",
    response_model=list[str],
    summary="List available gcode macros",
)
async def list_macros(printer_id: int):
    """Return user-defined gcode macros available on the printer."""
    client = _get_klipper_printer(printer_id)
    return await client.list_macros()


@router.post(
    "/printers/{printer_id}/macros/{macro_name}",
    response_model=PrintControlResponse,
    summary="Run a gcode macro",
)
async def run_macro(printer_id: int, macro_name: str):
    """Execute a gcode macro by name."""
    client = _get_klipper_printer(printer_id)
    success = await client.run_macro(macro_name)
    return PrintControlResponse(
        success=success,
        message=f"Macro {macro_name} executed" if success else f"Failed to run macro {macro_name}",
    )
