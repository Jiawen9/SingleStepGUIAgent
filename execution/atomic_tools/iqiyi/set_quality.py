#!/usr/bin/env python3
"""Set iQIYI video quality through the player quality menu."""

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
    center,
    dump_device_hierarchy,
    find_clickable_by_semantic_text,
    find_player_center,
    hierarchy_timing_detail,
    load_hierarchy,
    writer_from_arguments,
)


ADB_PATH = Path(os.environ.get("ADB_PATH", r"D:\platform-tools\adb.exe"))
QUALITY_CONTROL_ID = "com.qiyi.video:id/tv_play_rate_layout"
CURRENT_QUALITY_ID = "com.qiyi.video:id/tv_play_rate"
QUALITY_LABELS = {
    "auto": "智能",
    "1080p": "1080P",
    "720p": "720P",
    "480p": "480P",
}


def resource_name(resource_id: str) -> str:
    """Return the resource name without an app-specific package prefix."""
    return resource_id.rsplit("/", 1)[-1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set iQIYI quality in the player."
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument(
        "--quality",
        choices=tuple(QUALITY_LABELS),
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


def find_quality_control(root):
    # Prefer the visible quality text and walk upward to the nearest clickable
    # ancestor. Tablet builds expose com.qiyi.video.pad while phone builds
    # expose com.qiyi.video, so semantic lookup works across both packages.
    semantic_fallback = None
    expected_name = resource_name(QUALITY_CONTROL_ID)
    current_quality_name = resource_name(CURRENT_QUALITY_ID)

    if uses_semantic_matching():
        for label in ("智能", "1080P", "720P", "480P"):
            match = find_clickable_by_semantic_text(root, {label})
            if match is not None:
                return match[0]
        return None

    # Set-top-box builds expose the visible current-quality TextView itself
    # as the clickable menu entry (tv_play_rate), without a clickable
    # tv_play_rate_layout wrapper.
    direct_controls = [
        node
        for node in root.iter("node")
        if resource_name(node.get("resource-id", ""))
        in {expected_name, current_quality_name}
        and node.get("clickable") == "true"
        and node.get("visible-to-user") != "false"
        and node.get("enabled") != "false"
    ]
    if direct_controls:
        return max(
            direct_controls,
            key=lambda node: (
                bounds(node)[2] - bounds(node)[0]
            ) * (
                bounds(node)[3] - bounds(node)[1]
            ),
        )

    for label in ("智能", "1080P", "720P", "480P"):
        match = find_clickable_by_semantic_text(root, {label})
        if match is not None:
            semantic_fallback = semantic_fallback or match[0]
            if resource_name(match[0].get("resource-id", "")) == expected_name:
                return match[0]

    if semantic_fallback is not None:
        return semantic_fallback

    # Resource ID is only a fallback for layouts that omit the visible label.
    # Match the final resource name because the package prefix varies by build.
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


def normalize_quality_node(node) -> str | None:
    """Map a visible quality label to the action's canonical value."""
    for value in (node.get("text", ""), node.get("content-desc", "")):
        normalized = "".join(value.upper().split())
        if "智能" in normalized:
            return "auto"
        for quality in ("1080p", "720p", "480p"):
            if normalized.endswith(QUALITY_LABELS[quality].upper()):
                return quality
    return None


def quality_entries(root) -> list[tuple[str, object, bool]]:
    """Return (canonical quality, clickable card, selected) menu entries."""
    parents = {child: parent for parent in root.iter() for child in parent}
    current_quality_name = resource_name(CURRENT_QUALITY_ID)
    quality_control_name = resource_name(QUALITY_CONTROL_ID)
    entries = []
    seen = set()
    for node in root.iter("node"):
        quality = normalize_quality_node(node)
        if quality is None or node.get("visible-to-user") == "false":
            continue
        if resource_name(node.get("resource-id", "")) == current_quality_name:
            continue
        candidate = node
        clickable_card = None
        selected = node.get("selected") == "true" or node.get("checked") == "true"
        for _ in range(5):
            if candidate.get("selected") == "true" or candidate.get("checked") == "true":
                selected = True
            if (
                clickable_card is None
                and candidate.get("clickable") == "true"
                and candidate.get("visible-to-user") != "false"
                and candidate.get("enabled") != "false"
                and resource_name(candidate.get("resource-id", ""))
                not in {quality_control_name, current_quality_name}
            ):
                try:
                    bounds(candidate)
                except RuntimeError:
                    pass
                else:
                    clickable_card = candidate
            candidate = parents.get(candidate)
            if candidate is None:
                break
        if clickable_card is None:
            continue
        key = (quality, clickable_card.get("bounds", ""))
        if key in seen:
            continue
        seen.add(key)
        entries.append((quality, clickable_card, selected))
    return entries


def find_current_quality(root) -> str | None:
    """Find one selected menu quality, or the visible current-value label."""
    selected_by_card: dict[str, set[str]] = {}
    for quality, card, is_selected in quality_entries(root):
        if not is_selected:
            continue
        selected_by_card.setdefault(card.get("bounds", ""), set()).add(quality)
    if len(selected_by_card) == 1:
        selected = next(iter(selected_by_card.values()))
        # The smart card can expose both "智能" and its effective maximum
        # resolution (for example "1080P") as selected descendants. Treat
        # that one card as the single smart/current quality.
        if "auto" in selected:
            return "auto"
        if len(selected) == 1:
            return selected.pop()

    if uses_semantic_matching():
        return None

    current_values = {
        normalize_quality_node(node)
        for node in root.iter("node")
        if resource_name(node.get("resource-id", ""))
        == resource_name(CURRENT_QUALITY_ID)
        and node.get("visible-to-user") != "false"
    }
    current_values.discard(None)
    if len(current_values) == 1:
        return current_values.pop()
    return None


def find_quality_option(root, quality: str):
    return next(
        (
            card
            for entry_quality, card, _ in quality_entries(root)
            if entry_quality == quality
        ),
        None,
    )


def rejection_payload(*, stage: str, quality: str, message: str) -> dict:
    return {
        "source": "atomic_tool",
        "stage": stage,
        "requested_quality": quality,
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
        locator_root = load_hierarchy(args.initial_xml)
    else:
        u2 = load_uiautomator2()
        device = u2.connect(serial)
        device.jsonrpc.setConfigurator(
            {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
        )
        locator = dump_device_hierarchy(device, writer=writer)
        dump_seconds += locator.dump_seconds
        dump_details.append(hierarchy_timing_detail(locator))
        locator_root = locator.root

    try:
        player_x, player_y = find_player_center(locator_root)
    except RuntimeError:
        message = "当前界面没有可定位的视频播放器，无法调整清晰度。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="player",
                quality=args.quality,
                message=message,
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

    if device is None:
        u2 = load_uiautomator2()
        device = u2.connect(serial)
        device.jsonrpc.setConfigurator(
            {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
        )

    quality_control = None
    controls_root = None
    for attempt in range(args.attempts):
        if attempt > 0 and args.retry_delay:
            time.sleep(args.retry_delay)
        hierarchy = dump_device_hierarchy(device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        controls_root = hierarchy.root
        quality_control = find_quality_control(controls_root)
        if quality_control is not None:
            break

    if quality_control is None:
        message = "当前播放界面没有可识别的清晰度入口。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="quality_control",
                quality=args.quality,
                message=message,
            ),
        )

    visible_current_quality = (
        find_current_quality(controls_root)
        if controls_root is not None
        else None
    )
    if visible_current_quality == args.quality:
        quality_label = QUALITY_LABELS[args.quality]
        return finish(
            status="executed",
            message=(
                f"Current iQIYI quality is already {quality_label}; "
                "no quality menu click was sent."
            ),
        )

    control_x, control_y = center(quality_control)
    tap_seconds = adb_tap(serial, control_x, control_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "open_quality_menu",
            "command": "input tap",
            "x": control_x,
            "y": control_y,
            "seconds": tap_seconds,
        }
    )

    quality_option = None
    current_quality = None
    panel_root = None
    for attempt in range(args.attempts):
        if attempt > 0 and args.retry_delay:
            time.sleep(args.retry_delay)
        hierarchy = dump_device_hierarchy(device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        panel_root = hierarchy.root
        current_quality = find_current_quality(panel_root)
        quality_option = find_quality_option(panel_root, args.quality)
        if current_quality is not None and (
            current_quality == args.quality or quality_option is not None
        ):
            break

    if current_quality is None or panel_root is None:
        message = "清晰度面板中没有找到唯一的当前清晰度。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="current_quality",
                quality=args.quality,
                message=message,
            ),
        )

    if current_quality == args.quality:
        quality_label = QUALITY_LABELS[args.quality]
        return finish(
            status="executed",
            message=f"Current iQIYI quality is already {quality_label}; no quality option click was sent.",
        )

    if quality_option is None:
        quality_label = QUALITY_LABELS[args.quality]
        message = f"当前视频没有可选的 {quality_label} 清晰度。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="quality_option",
                quality=args.quality,
                message=message,
            ),
        )

    option_x, option_y = center(quality_option)
    tap_seconds = adb_tap(serial, option_x, option_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "set_quality",
            "command": "input tap",
            "quality": args.quality,
            "x": option_x,
            "y": option_y,
            "seconds": tap_seconds,
        }
    )
    quality_label = QUALITY_LABELS[args.quality]
    return finish(
        status="executed",
        message=f"Selected iQIYI quality {quality_label} at ({option_x}, {option_y}).",
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
