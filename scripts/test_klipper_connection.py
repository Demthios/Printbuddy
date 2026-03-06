#!/usr/bin/env python3
"""
scripts/test_klipper_connection.py
====================================
Standalone smoke test for the Klipper integration.
Run this BEFORE wiring into the full Bambuddy app to verify your
Moonraker docker is reachable and the adapter works correctly.

Usage:
    python scripts/test_klipper_connection.py --host 127.0.0.1 --port 7125

    # With API key:
    python scripts/test_klipper_connection.py --host 192.168.1.50 --api-key abc123

    # Upload and print a test file:
    python scripts/test_klipper_connection.py --host 127.0.0.1 --upload test.gcode
"""

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

# Add the project root to sys.path so we can import backend modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.core.printer_base import PrinterState, PrinterStatus
from backend.app.core.klipper.client import MoonrakerClient
from backend.app.core.klipper.file_transfer import KlipperFileTransfer
from backend.app.core.klipper.printer import KlipperPrinter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_state(state: PrinterState) -> None:
    """Pretty-print a PrinterState for the terminal."""
    print("\n" + "=" * 60)
    print(f"  Printer: {state.printer_id}")
    print(f"  Status:  {state.status.value.upper()}")
    print(f"  Hotend:  {state.hotend.actual}°C → {state.hotend.target}°C")
    print(f"  Bed:     {state.bed.actual}°C → {state.bed.target}°C")
    print(f"  Fan:     {state.fan_speed}%")
    if state.status in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
        p = state.progress
        print(f"  File:    {p.filename}")
        print(f"  Progress:{p.percent:.1f}%")
        elapsed = f"{p.elapsed_seconds // 60}m {p.elapsed_seconds % 60}s"
        remaining = f"{p.remaining_seconds // 60}m {p.remaining_seconds % 60}s"
        print(f"  Elapsed: {elapsed}  Remaining: {remaining}")
    if state.error_message:
        print(f"  ERROR:   {state.error_message}")
    if state.extra:
        print(f"  Extra:   {json.dumps(state.extra, indent=2)}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Test 1: Raw Moonraker client
# ---------------------------------------------------------------------------

async def test_raw_client(host: str, port: int, api_key: str | None) -> bool:
    """
    Test the low-level MoonrakerClient directly.
    Verifies we can connect and get printer.info.
    """
    print(f"\n[TEST 1] Raw MoonrakerClient → {host}:{port}")

    connected = asyncio.Event()
    client = MoonrakerClient(
        host=host,
        port=port,
        api_key=api_key,
        on_connected=lambda: connected.set(),
    )

    await client.connect()

    try:
        # Wait up to 10 seconds for connection
        await asyncio.wait_for(connected.wait(), timeout=10)
        print("  ✓ WebSocket connected")

        # Fetch printer info
        info = await client.send_request("printer.info")
        klippy_state = info.get("klippy_state", "unknown")
        print(f"  ✓ printer.info → klippy_state={klippy_state}")
        print(f"  ✓ Moonraker version: {info.get('software_version', 'unknown')}")

        # Fetch current objects
        objects = await client.send_request("printer.objects.list")
        obj_list = objects.get("objects", [])
        print(f"  ✓ Available objects: {', '.join(obj_list[:8])}{'…' if len(obj_list) > 8 else ''}")

        return True

    except asyncio.TimeoutError:
        print(f"  ✗ Connection timed out — is Moonraker running at {host}:{port}?")
        return False
    except Exception as exc:
        print(f"  ✗ Error: {exc}")
        return False
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Test 2: Full KlipperPrinter adapter
# ---------------------------------------------------------------------------

async def test_printer_adapter(host: str, port: int, api_key: str | None) -> bool:
    """
    Test the KlipperPrinter adapter end-to-end.
    Creates a printer, connects, waits for state, then disconnects.
    """
    print(f"\n[TEST 2] KlipperPrinter adapter → {host}:{port}")

    states_received = []

    def on_state_change(state: PrinterState):
        states_received.append(state)
        print_state(state)

    printer = KlipperPrinter(
        printer_id="test-001",
        name="Test Klipper",
        host=host,
        port=port,
        api_key=api_key,
        on_state_change=on_state_change,
    )

    await printer.connect()

    # Wait up to 15 seconds for at least one state update
    deadline = time.time() + 15
    while time.time() < deadline and len(states_received) == 0:
        await asyncio.sleep(0.5)

    await printer.disconnect()

    if states_received:
        print(f"\n  ✓ Received {len(states_received)} state update(s)")
        return True
    else:
        print("  ✗ No state updates received within 15 seconds")
        return False


# ---------------------------------------------------------------------------
# Test 3: File upload
# ---------------------------------------------------------------------------

async def test_file_upload(host: str, port: int, api_key: str | None, gcode_path: str) -> bool:
    """
    Test uploading a gcode file to Moonraker.
    Does NOT start the print — just verifies upload works.
    """
    print(f"\n[TEST 3] File upload → {gcode_path}")

    path = Path(gcode_path)
    if not path.exists():
        print(f"  ✗ File not found: {gcode_path}")
        return False

    file_bytes = path.read_bytes()
    transfer = KlipperFileTransfer(
        host=host,
        port=port,
        api_key=api_key,
        upload_path="printbuddy-test",
    )

    print(f"  Uploading {path.name} ({len(file_bytes)} bytes)…")
    success = await transfer.upload(
        filename=path.name,
        file_bytes=file_bytes,
        print_after=False,
    )

    if success:
        print(f"  ✓ Upload succeeded: {path.name}")
        # List files to confirm it's there
        files = await transfer.list_files("printbuddy-test")
        matching = [f for f in files if path.name in f.get("filename", "")]
        if matching:
            print(f"  ✓ File confirmed on printer: {matching[0]}")
        return True
    else:
        print("  ✗ Upload failed")
        return False


# ---------------------------------------------------------------------------
# Test 4: Live monitoring (watch state for 30 seconds)
# ---------------------------------------------------------------------------

async def test_live_monitor(host: str, port: int, api_key: str | None, duration: int = 30) -> None:
    """
    Connect and watch printer state for `duration` seconds.
    Good for verifying real-time updates work correctly.
    """
    print(f"\n[TEST 4] Live monitor for {duration} seconds… (Ctrl+C to stop early)")

    update_count = 0

    def on_state_change(state: PrinterState):
        nonlocal update_count
        update_count += 1
        # Only print every 5th update to avoid spam
        if update_count % 5 == 1:
            print_state(state)
        else:
            print(f"  → Update #{update_count}: {state.status.value} | "
                  f"Hotend: {state.hotend.actual}°C | Bed: {state.bed.actual}°C")

    printer = KlipperPrinter(
        printer_id="monitor-test",
        name="Monitor Test",
        host=host,
        port=port,
        api_key=api_key,
        on_state_change=on_state_change,
    )

    await printer.connect()

    try:
        await asyncio.sleep(duration)
    except asyncio.CancelledError:
        pass
    finally:
        await printer.disconnect()
        print(f"\n  Received {update_count} state updates in {duration}s "
              f"({update_count / duration:.1f}/sec)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Test Klipper/Moonraker integration")
    parser.add_argument("--host", default="127.0.0.1", help="Moonraker host IP")
    parser.add_argument("--port", type=int, default=7125, help="Moonraker port")
    parser.add_argument("--api-key", default=None, help="Moonraker API key (if required)")
    parser.add_argument("--upload", default=None, help="Path to a .gcode file to test upload")
    parser.add_argument("--monitor", type=int, default=0,
                        help="Watch live state for N seconds (0 = skip)")
    parser.add_argument("--skip-adapter", action="store_true",
                        help="Skip the full adapter test (just test raw client)")

    args = parser.parse_args()

    print(f"\nKlipper Integration Test")
    print(f"Target: {args.host}:{args.port}")
    print(f"API key: {'set' if args.api_key else 'none'}")

    results = {}

    # Test 1: Raw client
    results["raw_client"] = await test_raw_client(args.host, args.port, args.api_key)

    # Test 2: Full adapter
    if not args.skip_adapter:
        results["adapter"] = await test_printer_adapter(args.host, args.port, args.api_key)

    # Test 3: File upload
    if args.upload:
        results["upload"] = await test_file_upload(args.host, args.port, args.api_key, args.upload)

    # Test 4: Live monitor
    if args.monitor > 0:
        await test_live_monitor(args.host, args.port, args.api_key, args.monitor)

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    for name, passed in results.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {name}")

    all_passed = all(results.values())
    print(f"\n{'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
