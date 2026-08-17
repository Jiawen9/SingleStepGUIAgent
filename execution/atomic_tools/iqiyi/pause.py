#!/usr/bin/env python3
"""Pause iQIYI with two consecutive taps at the physical screen center."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from execution.timing import emit_atomic_timing
from device.xml_hierarchy import (
    add_xml_archive_arguments,
    dump_device_hierarchy,
    find_screen_center,
    hierarchy_timing_detail,
    load_hierarchy,
    writer_from_arguments,
)


ADB_PATH = Path(os.environ.get("ADB_PATH", r"D:\platform-tools\adb.exe"))


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pause iQIYI with two taps at the screen center."
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    add_xml_archive_arguments(parser)
    return parser.parse_args()


def select_device(requested_serial: str | None) -> str:
    if not ADB_PATH.is_file():
        raise FileNotFoundError(f"adb was not found: {ADB_PATH}")

    result = subprocess.run(
        [str(ADB_PATH), "devices"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    devices = [
        parts[0]
        for line in result.stdout.splitlines()[1:]
        if len(parts := line.split()) >= 2 and parts[1] == "device"
    ]
    if requested_serial:
        if requested_serial not in devices:
            raise RuntimeError(f"Device is unavailable: {requested_serial}")
        return requested_serial
    if not devices:
        raise RuntimeError("No authorized adb device is connected.")
    if len(devices) > 1:
        raise RuntimeError(
            "Multiple devices found. Use --serial to select one: " + ", ".join(devices)
        )
    return devices[0]


def load_uiautomator2():
    os.environ["ADBUTILS_ADB_PATH"] = str(ADB_PATH)
    try:
        import uiautomator2 as u2
    except ImportError as error:
        raise RuntimeError(
            "uiautomator2 is not installed. Run: python -m pip install -U uiautomator2"
        ) from error
    return u2


def adb_tap(serial: str, x: int, y: int) -> float:
    started_at = time.perf_counter()
    subprocess.run(
        [
            str(ADB_PATH),
            "-s",
            serial,
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
        ],
        check=True,
    )
    return time.perf_counter() - started_at


def main() -> int:
    args = parse_arguments()
    started_at = time.perf_counter()
    serial = select_device(args.serial)
    writer = writer_from_arguments(args)
    dump_seconds = 0.0
    dump_details: list[dict[str, object]] = []

    if args.initial_xml is not None:
        root = load_hierarchy(args.initial_xml)
    else:
        u2 = load_uiautomator2()
        device = u2.connect(serial)
        device.jsonrpc.setConfigurator(
            {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
        )
        hierarchy = dump_device_hierarchy(device, writer=writer)
        root = hierarchy.root
        dump_seconds = hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))

    x, y = find_screen_center(root)
    adb_seconds = 0.0
    adb_details: list[dict[str, object]] = []
    for operation in ("show_player_controls", "pause"):
        tap_seconds = adb_tap(serial, x, y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": operation,
                "command": "input tap",
                "x": x,
                "y": y,
                "seconds": tap_seconds,
            }
        )

    print(f"Paused with two screen-center taps at ({x}, {y}).")
    emit_atomic_timing(
        dump_xml_seconds=dump_seconds,
        adb_seconds=adb_seconds,
        total_seconds=time.perf_counter() - started_at,
        dump_details=dump_details,
        adb_details=adb_details,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        OSError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
