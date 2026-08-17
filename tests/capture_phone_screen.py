#!/usr/bin/env python3
"""Capture an Android device screen and save it as a local PNG file."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ADB_PATH = Path(r"D:\platform-tools\adb.exe")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def adb_command(arguments: list[str], serial: str | None = None) -> list[str]:
    command = [str(ADB_PATH)]
    if serial:
        command.extend(["-s", serial])
    command.extend(arguments)
    return command


def run_adb_text(
    arguments: list[str],
    *,
    serial: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = adb_command(arguments, serial)
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown adb error"
        raise RuntimeError(f"adb command failed: {message}")
    return result


def select_device(requested_serial: str | None) -> str:
    if requested_serial:
        result = run_adb_text(
            ["get-state"], serial=requested_serial, check=False
        )
        if result.returncode != 0 or result.stdout.strip() != "device":
            state = result.stdout.strip() or result.stderr.strip() or "unknown"
            raise RuntimeError(
                f"Device is unavailable: {requested_serial} (state: {state})"
            )
        return requested_serial

    result = run_adb_text(["devices"])
    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    if not devices:
        raise RuntimeError(
            "No available device found. Connect the phone, enable USB debugging, "
            "and accept the authorization prompt."
        )
    if len(devices) > 1:
        raise RuntimeError(
            "Multiple devices found. Use --serial to select one: " + ", ".join(devices)
        )
    return devices[0]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture the current Android screen as a PNG file."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output PNG path (default: screenshot_YYYYmmdd_HHMMSS.png).",
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if not ADB_PATH.is_file():
        raise FileNotFoundError(f"adb was not found: {ADB_PATH}")

    output_path = args.output or Path(
        f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    )
    output_path = output_path.expanduser().resolve()
    if not output_path.parent.is_dir():
        raise FileNotFoundError(
            f"Output directory does not exist: {output_path.parent}"
        )
    if output_path.suffix.lower() != ".png":
        raise ValueError("The output file must use the .png extension.")

    serial = select_device(args.serial)
    print(f"Capturing the screen from device {serial}...")

    command = adb_command(["exec-out", "screencap", "-p"], serial)
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Screenshot failed: {message or 'unknown adb error'}")
    if not result.stdout.startswith(PNG_SIGNATURE):
        raise RuntimeError("adb did not return valid PNG screenshot data.")

    output_path.write_bytes(result.stdout)
    print(f"Screenshot saved: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
