#!/usr/bin/env python3
"""Quickly dump Android UI XML through a persistent uiautomator2 service."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree


ADB_PATH = Path(r"D:\platform-tools\adb.exe")
BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quickly dump Android UI XML without waiting for UI idle."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output XML path (default: fast_ui_dump_YYYYmmdd_HHMMSS.xml).",
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument(
        "--compressed",
        action="store_true",
        help="Return a smaller hierarchy by omitting unimportant nodes.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=50,
        help="Maximum hierarchy depth (default: 50).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to wait after tapping before dumping (default: 0.2).",
    )
    parser.add_argument(
        "--pause-before-dump",
        action="store_true",
        help="Send KEYCODE_MEDIA_PAUSE before showing controls and dumping.",
    )

    tap_group = parser.add_mutually_exclusive_group()
    tap_group.add_argument(
        "--tap",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="Tap these coordinates immediately before dumping.",
    )
    tap_group.add_argument(
        "--tap-player",
        action="store_true",
        help="Locate the '视频播放器' node and tap its center before dumping.",
    )
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
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Unable to list adb devices.")

    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

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
    # adbutils honors this variable, so uiautomator2 uses the requested adb.exe.
    os.environ["ADBUTILS_ADB_PATH"] = str(ADB_PATH)
    try:
        import uiautomator2 as u2
    except ImportError as error:
        raise RuntimeError(
            "uiautomator2 is not installed. Run: python -m pip install -U uiautomator2"
        ) from error
    return u2


def pretty_xml(xml_text: str) -> bytes:
    try:
        document = minidom.parseString(xml_text.encode("utf-8"))
    except Exception as error:
        raise RuntimeError(f"Unable to parse hierarchy XML: {error}") from error
    return document.toprettyxml(indent="  ", encoding="utf-8")


def player_center(xml_text: str) -> tuple[int, int]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise RuntimeError(f"Unable to parse locator hierarchy: {error}") from error

    player_node = next(
        (
            node
            for node in root.iter("node")
            if node.get("content-desc") == "视频播放器"
        ),
        None,
    )
    if player_node is None:
        raise RuntimeError(
            "The '视频播放器' node was not found. Use --tap X Y instead."
        )

    bounds = player_node.get("bounds", "")
    match = BOUNDS_PATTERN.fullmatch(bounds)
    if not match:
        raise RuntimeError(f"Invalid player bounds: {bounds!r}")
    left, top, right, bottom = map(int, match.groups())
    return (left + right) // 2, (top + bottom) // 2


def main() -> int:
    args = parse_arguments()
    if args.max_depth < 1:
        raise ValueError("--max-depth must be at least 1.")
    if args.delay < 0:
        raise ValueError("--delay cannot be negative.")

    output_path = args.output or Path(
        f"fast_ui_dump_{datetime.now():%Y%m%d_%H%M%S}.xml"
    )
    output_path = output_path.expanduser().resolve()
    if not output_path.parent.is_dir():
        raise FileNotFoundError(
            f"Output directory does not exist: {output_path.parent}"
        )

    started_at = time.perf_counter()
    serial = select_device(args.serial)
    u2 = load_uiautomator2()

    print(f"Connecting to uiautomator2 on device {serial}...")
    device = u2.connect(serial)
    connected_at = time.perf_counter()

    # Disable the waits that make the stock `uiautomator dump` slow on video UIs.
    device.jsonrpc.setConfigurator(
        {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
    )

    if args.pause_before_dump:
        print("Pausing media playback...")
        device.press(127)  # Android KEYCODE_MEDIA_PAUSE

    tap_coordinates: tuple[int, int] | None = None
    if args.tap:
        tap_coordinates = tuple(args.tap)
        if tap_coordinates[0] < 0 or tap_coordinates[1] < 0:
            raise ValueError("Tap coordinates cannot be negative.")
    elif args.tap_player:
        print("Locating the video player...")
        locator_xml = device.dump_hierarchy(
            compressed=args.compressed,
            pretty=False,
            max_depth=args.max_depth,
        )
        tap_coordinates = player_center(locator_xml)

    if tap_coordinates:
        x, y = tap_coordinates
        print(f"Tapping at ({x}, {y})...")
        device.click(x, y)
        if args.delay:
            time.sleep(args.delay)

    dump_started_at = time.perf_counter()
    xml_text = device.dump_hierarchy(
        compressed=args.compressed,
        pretty=False,
        max_depth=args.max_depth,
    )
    dump_finished_at = time.perf_counter()
    output_path.write_bytes(pretty_xml(xml_text))
    finished_at = time.perf_counter()

    print(f"Export completed: {output_path}")
    print(
        "Timing: "
        f"connect={connected_at - started_at:.3f}s, "
        f"dump={dump_finished_at - dump_started_at:.3f}s, "
        f"total={finished_at - started_at:.3f}s"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
