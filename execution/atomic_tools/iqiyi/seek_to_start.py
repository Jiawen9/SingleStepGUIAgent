#!/usr/bin/env python3
"""Move an iQIYI video's progress to the start without changing play state."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from execution.timing import emit_atomic_result, emit_atomic_timing
from execution.atomic_tools.iqiyi.mode import uses_semantic_matching
from device.xml_hierarchy import (
    add_xml_archive_arguments,
    bounds,
    dump_device_hierarchy,
    find_player_center,
    hierarchy_timing_detail,
    load_hierarchy,
    writer_from_arguments,
)


ADB_PATH = Path(os.environ.get("ADB_PATH", r"D:\platform-tools\adb.exe"))
PROGRESS_BAR_NAME = "play_progress"


def resource_name(resource_id: str) -> str:
    """Return the Android resource name without its package prefix."""
    return resource_id.rsplit("/", 1)[-1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move an iQIYI video's progress to the start without changing "
            "its play or pause state."
        )
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="Maximum hierarchy snapshots while controls are animating.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.05,
        help="Seconds between fallback hierarchy snapshots.",
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
            "Multiple devices found. Use --serial to select one: "
            + ", ".join(devices)
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


def connect_uiautomator2(serial: str):
    u2 = load_uiautomator2()
    device = u2.connect(serial)
    device.jsonrpc.setConfigurator(
        {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
    )
    return device


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


def find_progress_bar(root):
    """Return the widest visible, enabled iQIYI playback SeekBar.

    Package prefixes differ between phone, tablet and set-top-box builds, so
    match the stable resource name instead of a complete resource-id.
    """
    candidates = []
    for node in root.iter("node"):
        if (
            node.get("class") != "android.widget.SeekBar"
            or node.get("visible-to-user") == "false"
            or node.get("enabled") == "false"
        ):
            continue
        if (
            not uses_semantic_matching()
            and resource_name(node.get("resource-id", "")) != PROGRESS_BAR_NAME
        ):
            continue
        try:
            left, top, right, bottom = bounds(node)
        except RuntimeError:
            continue
        candidates.append(((right - left, (right - left) * (bottom - top)), node))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def progress_start_coordinates(progress_bar) -> tuple[int, int]:
    """Choose a point one pixel inside the SeekBar's left boundary."""
    left, top, right, bottom = bounds(progress_bar)
    return min(left + 1, right - 1), (top + bottom) // 2


def rejection_payload(*, stage: str, message: str) -> dict:
    return {
        "source": "atomic_tool",
        "reason_type": "TARGET_NOT_VISIBLE",
        "stage": stage,
        "message": message,
    }


def main() -> int:
    args = parse_arguments()
    if args.attempts < 1:
        raise ValueError("--attempts must be at least 1.")
    if args.retry_delay < 0:
        raise ValueError("--retry-delay cannot be negative.")

    started_at = time.perf_counter()
    serial = select_device(args.serial)
    writer = writer_from_arguments(args)
    dump_seconds = 0.0
    adb_seconds = 0.0
    dump_details = []
    adb_details = []
    device = None

    def finish(
        *, status: str, message: str, rejection: dict | None = None
    ) -> int:
        print(message)
        emit_atomic_result(
            status=status,
            message=message,
            rejection=rejection,
        )
        emit_atomic_timing(
            dump_xml_seconds=dump_seconds,
            adb_seconds=adb_seconds,
            total_seconds=time.perf_counter() - started_at,
            dump_details=dump_details,
            adb_details=adb_details,
        )
        return 0

    if args.initial_xml is not None:
        initial_root = load_hierarchy(args.initial_xml)
    else:
        device = connect_uiautomator2(serial)
        hierarchy = dump_device_hierarchy(device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        initial_root = hierarchy.root

    progress_bar = find_progress_bar(initial_root)
    progress_source = "initial_xml"

    if progress_bar is None:
        try:
            player_x, player_y = find_player_center(initial_root)
        except RuntimeError:
            message = "当前界面没有可定位的爱奇艺视频播放器，无法定位到开头。"
            return finish(
                status="rejected",
                message=message,
                rejection=rejection_payload(stage="player", message=message),
            )

        tap_seconds = adb_tap(serial, player_x, player_y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": "show_player_controls",
                "command": "input tap",
                "x": player_x,
                "y": player_y,
                "seconds": tap_seconds,
            }
        )

        if device is None:
            device = connect_uiautomator2(serial)

        progress_source = "controls_xml"
        for attempt in range(args.attempts):
            if attempt > 0 and args.retry_delay:
                time.sleep(args.retry_delay)
            hierarchy = dump_device_hierarchy(device, writer=writer)
            dump_seconds += hierarchy.dump_seconds
            dump_details.append(hierarchy_timing_detail(hierarchy))
            progress_bar = find_progress_bar(hierarchy.root)
            if progress_bar is not None:
                break

    if progress_bar is None:
        message = "当前播放界面没有可操作的进度条，无法定位到开头。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(stage="progress_bar", message=message),
        )

    start_x, start_y = progress_start_coordinates(progress_bar)
    tap_seconds = adb_tap(serial, start_x, start_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "seek_to_start",
            "command": "input tap",
            "source": progress_source,
            "x": start_x,
            "y": start_y,
            "seconds": tap_seconds,
        }
    )
    return finish(
        status="executed",
        message=(
            "Moved iQIYI playback progress to the start at "
            f"({start_x}, {start_y}) without changing play state."
        ),
    )


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
