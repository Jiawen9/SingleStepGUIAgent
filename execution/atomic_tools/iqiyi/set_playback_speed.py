#!/usr/bin/env python3
"""Set iQIYI playback speed through the player speed panel."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from execution.timing import emit_atomic_result, emit_atomic_timing
from execution.atomic_tools.iqiyi.mode import uses_semantic_matching
from device.xml_hierarchy import (
    add_xml_archive_arguments,
    bounds,
    center,
    dump_device_hierarchy,
    find_clickable_by_semantic_text,
    find_player_center,
    hierarchy_timing_detail,
    load_hierarchy,
    writer_from_arguments,
)


ADB_PATH = Path(os.environ.get("ADB_PATH", r"D:\platform-tools\adb.exe"))
SPEED_CONTROL_ID = "com.qiyi.video:id/tv_change_speed_play"
SUPPORTED_SPEEDS = ("0.75x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x")
SPEED_TEXT_PATTERN = re.compile(
    r"^\s*(0\.75|1\.0|1\.25|1\.5|2\.0|3\.0)\s*[xX]\s*$"
)
SPEED_DESCRIPTION_PATTERN = re.compile(
    r"^\s*(0\.75|1\.0|1\.25|1\.5|2\.0|3\.0)\s*倍速\s*$"
)


def resource_name(resource_id: str) -> str:
    """Return the resource name without an app-specific package prefix."""
    return resource_id.rsplit("/", 1)[-1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set iQIYI playback speed in the player."
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument(
        "--speed",
        choices=SUPPORTED_SPEEDS,
        required=True,
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="Maximum hierarchy snapshots for each animated menu stage.",
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


def normalize_speed_node(node) -> str | None:
    text_match = SPEED_TEXT_PATTERN.fullmatch(node.get("text", ""))
    if text_match is not None:
        return text_match.group(1) + "x"
    description_match = SPEED_DESCRIPTION_PATTERN.fullmatch(
        node.get("content-desc", "")
    )
    if description_match is not None:
        return description_match.group(1) + "x"
    return None


def find_speed_control(root):
    # Prefer the visible semantic label and walk to its nearest clickable
    # ancestor. Tablet builds use com.qiyi.video.pad while phone builds use
    # com.qiyi.video, and the text-based lookup works for both.
    match = find_clickable_by_semantic_text(root, {"倍速"})
    if match is not None:
        return match[0]

    if uses_semantic_matching():
        return None

    # Keep a resource-ID fallback for layouts that expose the ID but omit the
    # visible label. Match only the final resource name so package prefixes do
    # not affect the result.
    expected_name = resource_name(SPEED_CONTROL_ID)
    return next(
        (
            node
            for node in root.iter("node")
            if resource_name(node.get("resource-id", "")) == expected_name
            and node.get("clickable") == "true"
            and node.get("visible-to-user") != "false"
            and node.get("enabled") != "false"
        ),
        None,
    )


def speed_entries(root) -> list[tuple[str, object, bool]]:
    """Return (canonical speed, clickable card, selected) panel entries."""
    parents = {child: parent for parent in root.iter() for child in parent}
    entries = []
    seen = set()
    for node in root.iter("node"):
        speed = normalize_speed_node(node)
        if speed is None or node.get("visible-to-user") == "false":
            continue
        candidate = node
        clickable_card = None
        selected = node.get("selected") == "true"
        for _ in range(4):
            if (
                clickable_card is None
                and candidate.get("clickable") == "true"
                and candidate.get("visible-to-user") != "false"
                and candidate.get("enabled") != "false"
            ):
                try:
                    bounds(candidate)
                except RuntimeError:
                    pass
                else:
                    clickable_card = candidate
            if candidate.get("selected") == "true":
                selected = True
            candidate = parents.get(candidate)
            if candidate is None:
                break
        if clickable_card is None:
            continue
        key = (speed, clickable_card.get("bounds", ""))
        if key in seen:
            continue
        seen.add(key)
        entries.append((speed, clickable_card, selected))
    return entries


def find_current_speed(root) -> str | None:
    selected_speeds = {
        speed for speed, _, selected in speed_entries(root) if selected
    }
    if len(selected_speeds) != 1:
        return None
    return selected_speeds.pop()


def find_speed_option(root, speed: str):
    return next(
        (card for entry_speed, card, _ in speed_entries(root) if entry_speed == speed),
        None,
    )


def rejection_payload(*, stage: str, speed: str, message: str) -> dict:
    return {
        "source": "atomic_tool",
        "stage": stage,
        "requested_speed": speed,
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

    def ensure_device():
        nonlocal device
        if device is None:
            device = connect_uiautomator2(serial)
        return device

    def dump_until(finder):
        nonlocal dump_seconds
        found = None
        root = None
        active_device = ensure_device()
        for attempt in range(args.attempts):
            if attempt > 0 and args.retry_delay:
                time.sleep(args.retry_delay)
            hierarchy = dump_device_hierarchy(active_device, writer=writer)
            dump_seconds += hierarchy.dump_seconds
            dump_details.append(hierarchy_timing_detail(hierarchy))
            root = hierarchy.root
            found = finder(root)
            if found is not None:
                break
        return found, root

    if args.initial_xml is not None:
        initial_root = load_hierarchy(args.initial_xml)
    else:
        device = ensure_device()
        hierarchy = dump_device_hierarchy(device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        initial_root = hierarchy.root

    panel_root = initial_root
    current_speed = find_current_speed(panel_root)
    if current_speed is None:
        speed_control = find_speed_control(initial_root)
        if speed_control is None:
            try:
                player_x, player_y = find_player_center(initial_root)
            except RuntimeError:
                message = "当前界面没有可定位的爱奇艺视频播放器，无法调整倍速。"
                return finish(
                    status="rejected",
                    message=message,
                    rejection=rejection_payload(
                        stage="player", speed=args.speed, message=message
                    ),
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
            speed_control, _ = dump_until(find_speed_control)

        if speed_control is None:
            message = "当前播放界面没有可识别的倍速入口。"
            return finish(
                status="rejected",
                message=message,
                rejection=rejection_payload(
                    stage="speed_control", speed=args.speed, message=message
                ),
            )

        control_x, control_y = center(speed_control)
        tap_seconds = adb_tap(serial, control_x, control_y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": "open_speed_panel",
                "command": "input tap",
                "x": control_x,
                "y": control_y,
                "seconds": tap_seconds,
            }
        )
        current_speed, panel_root = dump_until(find_current_speed)

    if current_speed is None or panel_root is None:
        message = "倍速面板中没有找到唯一的当前播放倍速。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="current_speed", speed=args.speed, message=message
            ),
        )

    if current_speed == args.speed:
        return finish(
            status="executed",
            message=f"Current iQIYI playback speed is already {args.speed}; no speed option click was sent.",
        )

    speed_option = find_speed_option(panel_root, args.speed)
    if speed_option is None:
        message = f"当前倍速面板中没有可操作的 {args.speed} 选项。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="speed_option", speed=args.speed, message=message
            ),
        )

    option_x, option_y = center(speed_option)
    tap_seconds = adb_tap(serial, option_x, option_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "set_playback_speed",
            "command": "input tap",
            "current_speed": current_speed,
            "target_speed": args.speed,
            "x": option_x,
            "y": option_y,
            "seconds": tap_seconds,
        }
    )
    return finish(
        status="executed",
        message=(
            f"Changed iQIYI playback speed from {current_speed} to {args.speed} "
            f"at ({option_x}, {option_y})."
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
