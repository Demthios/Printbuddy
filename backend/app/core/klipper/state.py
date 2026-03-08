"""
backend/app/printers/klipper/state.py
=======================================
State mapper: raw Moonraker data → unified PrinterState

Moonraker sends printer object data as nested dicts.  This module is
responsible for picking out the values we care about and mapping them
into the PrinterState dataclass defined in base.py.

Keeping this logic in its own file means:
  - printer.py stays readable (no giant if/else soup)
  - Easy to add more mapped fields later without touching the adapter

EXAMPLE raw Moonraker status dict:
{
    "print_stats": {
        "state": "printing",
        "filename": "benchy.gcode",
        "print_duration": 1234.5,
        "total_duration": 1300.0,
    },
    "extruder": {
        "temperature": 215.3,
        "target": 215.0,
    },
    "heater_bed": {
        "temperature": 59.8,
        "target": 60.0,
    },
    "display_status": {
        "progress": 0.42,
    },
    "fan": {
        "speed": 0.75,
    },
}
"""

from backend.app.core.printer_base import (
    ExtruderTemp,
    PrintProgress,
    PrinterState,
    PrinterStatus,
    PrinterType,
    Temperature,
)


# ---------------------------------------------------------------------------
# Klipper print_stats.state → unified PrinterStatus
# ---------------------------------------------------------------------------

# Moonraker's possible print_stats.state values:
#   "standby"   — idle, nothing loaded
#   "printing"  — actively printing
#   "paused"    — paused by user or macro
#   "complete"  — print finished successfully
#   "cancelled" — print was cancelled
#   "error"     — Klippy error during print
_KLIPPER_STATUS_MAP: dict[str, PrinterStatus] = {
    "standby":   PrinterStatus.IDLE,
    "printing":  PrinterStatus.PRINTING,
    "paused":    PrinterStatus.PAUSED,
    "complete":  PrinterStatus.FINISHED,
    "cancelled": PrinterStatus.IDLE,
    "error":     PrinterStatus.ERROR,
}


def map_klippy_state_to_status(klippy_state: str) -> PrinterStatus:
    """
    Convert the top-level Klippy connection state to PrinterStatus.

    klippy_state comes from printer.info and can be:
      "ready"    — Klipper is up and running
      "startup"  — Klipper is still starting
      "shutdown" — Klipper has shut down (error or intentional)
      "disconnected" — Moonraker can't reach Klipper
    """
    if klippy_state in ("startup", "disconnected"):
        return PrinterStatus.OFFLINE
    if klippy_state == "shutdown":
        return PrinterStatus.ERROR
    # "ready" — use print_stats.state for finer-grained status
    return None  # Caller will use print_stats.state instead


def map_print_stats_state(state_str: str) -> PrinterStatus:
    """Map Klipper's print_stats.state string to our unified enum."""
    return _KLIPPER_STATUS_MAP.get(state_str, PrinterStatus.IDLE)


# ---------------------------------------------------------------------------
# Main mapping function
# ---------------------------------------------------------------------------

def build_printer_state(
    printer_id: str,
    printer_name: str,
    status_data: dict,
    klippy_state: str = "ready",
    camera_url: str | None = None,
    extruder_names: list | None = None,
) -> PrinterState:
    """
    Build a PrinterState from a raw Moonraker status dict.

    Args:
        printer_id:   UUID from DB
        printer_name: Human-readable name (used for logging only)
        status_data:  The dict Moonraker gives us for subscribed objects.
                      May be partial — only the changed fields are included
                      in incremental updates, so callers should merge with
                      the last full snapshot before calling this.
        klippy_state: From printer.info["klippy_state"]
        camera_url:   MJPEG stream URL (passed through unchanged)

    Returns:
        A fully populated PrinterState.
    """

    # --- Determine overall status -------------------------------------------
    # First check the Klippy-level state (is the MCU even connected?)
    top_level = map_klippy_state_to_status(klippy_state)
    if top_level is not None:
        # Klippy isn't "ready" — no point parsing print stats
        return PrinterState(
            printer_id=printer_id,
            printer_type=PrinterType.KLIPPER,
            status=top_level,
            camera_url=camera_url,
            error_message=f"Klippy state: {klippy_state}" if top_level == PrinterStatus.ERROR else None,
        )

    # Klippy is ready — map print_stats.state
    print_stats = status_data.get("print_stats", {})
    raw_state = print_stats.get("state", "standby")
    status = map_print_stats_state(raw_state)

    # --- Temperatures --------------------------------------------------------
    resolved_extruder_names = extruder_names or ["extruder"]
    extruders = []
    for mn in resolved_extruder_names:
        data = status_data.get(mn, {})
        tool_name = "T0" if mn == "extruder" else f"T{mn[8:]}"
        extruders.append(ExtruderTemp(
            tool_name=tool_name,
            moonraker_name=mn,
            actual=round(float(data.get("temperature", 0.0)), 1),
            target=round(float(data.get("target", 0.0)), 1),
        ))

    # hotend is T0 alias for backward compatibility
    hotend = Temperature(
        actual=extruders[0].actual if extruders else 0.0,
        target=extruders[0].target if extruders else 0.0,
    )

    bed_data = status_data.get("heater_bed", {})
    bed = Temperature(
        actual=round(float(bed_data.get("temperature", 0.0)), 1),
        target=round(float(bed_data.get("target", 0.0)), 1),
    )

    # Active extruder from toolhead
    toolhead_data = status_data.get("toolhead", {})
    active_extruder = toolhead_data.get("extruder", "extruder")

    # --- Print progress ------------------------------------------------------
    # display_status.progress is a float 0.0–1.0
    display_status = status_data.get("display_status", {})
    progress_frac = float(display_status.get("progress", 0.0))
    percent = round(progress_frac * 100, 1)

    filename = print_stats.get("filename", "")

    # Klipper tracks print_duration (time actively printing, excludes pauses)
    # and total_duration (wall clock since print started).
    print_duration = float(print_stats.get("print_duration", 0))
    total_duration = float(print_stats.get("total_duration", 0))
    elapsed = int(total_duration)

    # Estimate remaining time from progress percentage
    # ETA = (elapsed / progress) - elapsed   — only valid when progress > 1%
    if progress_frac > 0.01:
        estimated_total = total_duration / progress_frac
        remaining = max(0, int(estimated_total - total_duration))
    else:
        remaining = 0

    progress = PrintProgress(
        filename=filename,
        percent=percent,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
    )

    # --- Fan speed -----------------------------------------------------------
    fan_data = status_data.get("fan", {})
    # Moonraker reports fan speed as 0.0–1.0; we store as 0–100
    fan_speed = round(float(fan_data.get("speed", 0.0)) * 100, 1)

    # --- Error message -------------------------------------------------------
    error_message = None
    if status == PrinterStatus.ERROR:
        error_message = print_stats.get("message", "Unknown Klipper error")

    # --- Extra data (Klipper-specific, shown in detail panels) ---------------
    extra = {
        "print_duration_seconds": int(print_duration),
        "position": toolhead_data.get("position", []),  # [x, y, z, e]
        "max_velocity": toolhead_data.get("max_velocity"),
        "extruder_name": active_extruder,
    }

    return PrinterState(
        printer_id=printer_id,
        printer_type=PrinterType.KLIPPER,
        status=status,
        hotend=hotend,
        bed=bed,
        extruders=extruders,
        active_extruder=active_extruder,
        progress=progress,
        fan_speed=fan_speed,
        camera_url=camera_url,
        error_message=error_message,
        extra=extra,
    )


def merge_status(base: dict, update: dict) -> dict:
    """
    Deep-merge a partial Moonraker status update into the full snapshot.

    Moonraker's notify_status_update only sends CHANGED fields.
    We need to maintain a complete picture, so we merge the delta into the
    last full snapshot before mapping.

    This is a shallow merge per top-level object key (e.g. "extruder"),
    and a deep merge within each object's fields.
    """
    result = {**base}  # Shallow copy of the full snapshot
    for obj_name, fields in update.items():
        if obj_name in result and isinstance(result[obj_name], dict):
            # Merge new fields into the existing object dict
            result[obj_name] = {**result[obj_name], **fields}
        else:
            result[obj_name] = fields
    return result
