#!/usr/bin/env python3
"""Select the previous or next iQIYI episode in the player."""

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
NEXT_EPISODE_ID = "com.qiyi.video:id/im_play_next"
EPISODE_MENU_ID = "com.qiyi.video:id/tv_change_episode"
EPISODE_BLOCK_ID = "com.qiyi.video:id/blockLayout"
EPISODE_CONTAINER_NAMES = {
    "blockLayout",
    "episodeGridView",
    "episode_item_root",
}
PLAYING_MARKER_NAMES = {"playing", "episode_item_playing"}
EPISODE_NUMBER_PATTERN = re.compile(
    r"^\s*(?:第\s*)?(\d+)(?:\s*集(?:\s|$)|\s|$)"
)


def resource_name(resource_id: str) -> str:
    """Return the resource name without an app-specific package prefix."""
    return resource_id.rsplit("/", 1)[-1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select the previous or next iQIYI episode."
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument(
        "--direction",
        choices=("previous", "next"),
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


def find_clickable_control(root, resource_id: str):
    expected_name = resource_name(resource_id)
    candidates = []
    for node in root.iter("node"):
        if (
            resource_name(node.get("resource-id", "")) != expected_name
            or node.get("clickable") != "true"
            or node.get("visible-to-user") == "false"
            or node.get("enabled") == "false"
        ):
            continue
        try:
            left, top, right, bottom = bounds(node)
        except RuntimeError:
            continue
        candidates.append(((right - left) * (bottom - top), node))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def find_next_episode_control(root):
    if uses_semantic_matching():
        match = find_clickable_by_semantic_text(root, {"下一集"})
        return match[0] if match is not None else None
    return find_clickable_control(root, NEXT_EPISODE_ID)


def find_episode_menu_control(root):
    if uses_semantic_matching():
        match = find_clickable_by_semantic_text(root, {"选集"})
        return match[0] if match is not None else None
    control = find_clickable_control(root, EPISODE_MENU_ID)
    if control is not None:
        return control

    # Some iQIYI tablet layouts expose the entry as a clickable card whose
    # child is the visible "选集" label, without tv_change_episode.
    match = find_clickable_by_semantic_text(root, {"选集"})
    return match[0] if match is not None else None


def episode_entries(root) -> list[tuple[int, object, bool]]:
    """Return (episode number, clickable card, selected) from the episode list."""
    parents = {child: parent for parent in root.iter() for child in parent}
    entries = []
    seen = set()
    semantic_mode = uses_semantic_matching()
    for node in root.iter("node"):
        match = EPISODE_NUMBER_PATTERN.match(node.get("text", ""))
        if match is None or node.get("visible-to-user") == "false":
            continue

        candidate = node
        clickable_card = None
        inside_episode_block = semantic_mode
        selected = node.get("selected") == "true"
        for _ in range(5):
            if resource_name(candidate.get("resource-id", "")) in (
                EPISODE_CONTAINER_NAMES
            ):
                inside_episode_block = True
            if (
                clickable_card is None
                and candidate.get("clickable") == "true"
                and candidate.get("visible-to-user") != "false"
                and candidate.get("enabled") != "false"
            ):
                clickable_card = candidate
            if candidate.get("selected") == "true":
                selected = True
            candidate = parents.get(candidate)
            if candidate is None:
                break

        if not inside_episode_block or clickable_card is None:
            continue

        # The tablet layout marks the current card with a descendant named
        # ``playing`` instead of selected="true" on the card or its ancestors.
        if not semantic_mode and any(
            resource_name(descendant.get("resource-id", ""))
            in PLAYING_MARKER_NAMES
            for descendant in clickable_card.iter("node")
        ):
            selected = True

        number = int(match.group(1))
        key = (number, clickable_card.get("bounds", ""))
        if key in seen:
            continue
        seen.add(key)
        entries.append((number, clickable_card, selected))
    return entries


def find_current_episode(root) -> int | None:
    selected_numbers = {
        number for number, _, selected in episode_entries(root) if selected
    }
    if len(selected_numbers) == 1:
        return selected_numbers.pop()
    if selected_numbers:
        return None
    if uses_semantic_matching():
        return None
    return infer_current_episode_from_playing_marker(root)


def _clickable_episode_card(node, parents):
    candidate = node
    inside_episode_container = False
    clickable_card = None
    for _ in range(6):
        if resource_name(candidate.get("resource-id", "")) in (
            EPISODE_CONTAINER_NAMES
        ):
            inside_episode_container = True
        if (
            clickable_card is None
            and candidate.get("clickable") == "true"
            and candidate.get("visible-to-user") != "false"
            and candidate.get("enabled") != "false"
        ):
            clickable_card = candidate
        candidate = parents.get(candidate)
        if candidate is None:
            break
    return clickable_card if inside_episode_container else None


def infer_current_episode_from_playing_marker(root) -> int | None:
    """Infer the episode whose number is replaced by a playing icon.

    The set-top-box layout replaces the current episode's numeric TextView
    with ``episode_item_playing``. Use the ordered grid cards and neighboring
    numeric labels; accept the inference only when both sides agree (or one
    side provides an unambiguous sequence position).
    """
    parents = {child: parent for parent in root.iter() for child in parent}
    cards: dict[str, tuple[object, int | None, bool]] = {}

    for node in root.iter("node"):
        name = resource_name(node.get("resource-id", ""))
        number_match = EPISODE_NUMBER_PATTERN.match(node.get("text", ""))
        is_marker = name in PLAYING_MARKER_NAMES
        if number_match is None and not is_marker:
            continue
        card = _clickable_episode_card(node, parents)
        if card is None:
            continue
        key = card.get("bounds", "")
        if not key:
            continue
        old_card, old_number, old_marker = cards.get(
            key, (card, None, False)
        )
        number = int(number_match.group(1)) if number_match else old_number
        cards[key] = (old_card, number, old_marker or is_marker)

    ordered = sorted(
        cards.values(),
        key=lambda item: (
            bounds(item[0])[1],
            bounds(item[0])[0],
        ),
    )
    marker_indexes = [
        index for index, (_, _, marker) in enumerate(ordered) if marker
    ]
    if len(marker_indexes) != 1:
        return None
    marker_index = marker_indexes[0]
    inferred: set[int] = set()
    for index in range(marker_index - 1, -1, -1):
        number = ordered[index][1]
        if number is not None:
            inferred.add(number + marker_index - index)
            break
    for index in range(marker_index + 1, len(ordered)):
        number = ordered[index][1]
        if number is not None:
            inferred.add(number - (index - marker_index))
            break
    inferred = {number for number in inferred if number >= 1}
    if len(inferred) != 1:
        return None
    return inferred.pop()


def find_episode_card(root, episode_number: int):
    return next(
        (
            card
            for number, card, _ in episode_entries(root)
            if number == episode_number
        ),
        None,
    )


def rejection_payload(*, stage: str, direction: str, message: str) -> dict:
    return {
        "source": "atomic_tool",
        "stage": stage,
        "direction": direction,
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

    if args.direction == "next":
        next_control = find_next_episode_control(initial_root)
        if next_control is None:
            # An already-open episode panel can expose episode cards in the
            # first XML snapshot instead of a dedicated next button.
            panel_current = find_current_episode(initial_root)
            if panel_current is not None:
                target_episode = panel_current + 1
                next_card = find_episode_card(initial_root, target_episode)
                if next_card is None:
                    message = f"选集面板中没有可操作的第{target_episode}集。"
                    return finish(
                        status="rejected",
                        message=message,
                        rejection=rejection_payload(
                            stage="next_episode",
                            direction=args.direction,
                            message=message,
                        ),
                    )

                next_x, next_y = center(next_card)
                tap_seconds = adb_tap(serial, next_x, next_y)
                adb_seconds += tap_seconds
                adb_details.append(
                    {
                        "operation": "next_episode",
                        "command": "input tap",
                        "current_episode": panel_current,
                        "target_episode": target_episode,
                        "x": next_x,
                        "y": next_y,
                        "seconds": tap_seconds,
                    }
                )
                return finish(
                    status="executed",
                    message=(
                        f"Selected iQIYI episode {target_episode} from current "
                        f"episode {panel_current} at ({next_x}, {next_y})."
                    ),
                )

            episode_menu = find_episode_menu_control(initial_root)
            if episode_menu is not None:
                menu_x, menu_y = center(episode_menu)
                tap_seconds = adb_tap(serial, menu_x, menu_y)
                adb_seconds += tap_seconds
                adb_details.append(
                    {
                        "operation": "open_episode_menu",
                        "command": "input tap",
                        "x": menu_x,
                        "y": menu_y,
                        "seconds": tap_seconds,
                    }
                )
                panel_current, panel_root = dump_until(find_current_episode)
                if panel_current is not None and panel_root is not None:
                    target_episode = panel_current + 1
                    next_card = find_episode_card(panel_root, target_episode)
                    if next_card is None:
                        message = f"选集面板中没有可操作的第{target_episode}集。"
                        return finish(
                            status="rejected",
                            message=message,
                            rejection=rejection_payload(
                                stage="next_episode",
                                direction=args.direction,
                                message=message,
                            ),
                        )

                    next_x, next_y = center(next_card)
                    tap_seconds = adb_tap(serial, next_x, next_y)
                    adb_seconds += tap_seconds
                    adb_details.append(
                        {
                            "operation": "next_episode",
                            "command": "input tap",
                            "current_episode": panel_current,
                            "target_episode": target_episode,
                            "x": next_x,
                            "y": next_y,
                            "seconds": tap_seconds,
                        }
                    )
                    return finish(
                        status="executed",
                        message=(
                            f"Selected iQIYI episode {target_episode} from current "
                            f"episode {panel_current} at ({next_x}, {next_y})."
                        ),
                    )

            try:
                player_x, player_y = find_player_center(initial_root)
            except RuntimeError:
                message = "当前界面没有可定位的爱奇艺视频播放器，无法播放下一集。"
                return finish(
                    status="rejected",
                    message=message,
                    rejection=rejection_payload(
                        stage="player", direction=args.direction, message=message
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
            next_control, _ = dump_until(find_next_episode_control)

        if next_control is None:
            message = "当前播放界面没有可识别的下一集按钮。"
            return finish(
                status="rejected",
                message=message,
                rejection=rejection_payload(
                    stage="next_episode_control",
                    direction=args.direction,
                    message=message,
                ),
            )

        next_x, next_y = center(next_control)
        tap_seconds = adb_tap(serial, next_x, next_y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": "next_episode",
                "command": "input tap",
                "x": next_x,
                "y": next_y,
                "seconds": tap_seconds,
            }
        )
        return finish(
            status="executed",
            message=f"Selected the next iQIYI episode at ({next_x}, {next_y}).",
        )

    panel_root = initial_root
    current_episode = find_current_episode(panel_root)
    if current_episode is None:
        episode_menu = find_episode_menu_control(initial_root)
        if episode_menu is None:
            try:
                player_x, player_y = find_player_center(initial_root)
            except RuntimeError:
                message = "当前界面没有可定位的爱奇艺视频播放器，无法播放上一集。"
                return finish(
                    status="rejected",
                    message=message,
                    rejection=rejection_payload(
                        stage="player", direction=args.direction, message=message
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
            episode_menu, _ = dump_until(find_episode_menu_control)

        if episode_menu is None:
            message = "当前播放界面没有可识别的选集入口或集数信息。"
            return finish(
                status="rejected",
                message=message,
                rejection=rejection_payload(
                    stage="episode_menu",
                    direction=args.direction,
                    message=message,
                ),
            )

        menu_x, menu_y = center(episode_menu)
        tap_seconds = adb_tap(serial, menu_x, menu_y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": "open_episode_menu",
                "command": "input tap",
                "x": menu_x,
                "y": menu_y,
                "seconds": tap_seconds,
            }
        )
        current_episode, panel_root = dump_until(find_current_episode)

    if current_episode is None or panel_root is None:
        message = "选集面板中没有找到唯一的当前播放集数。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="current_episode",
                direction=args.direction,
                message=message,
            ),
        )

    previous_episode = current_episode - 1
    if previous_episode < 1:
        message = "当前已经是第1集，没有上一集。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="previous_episode",
                direction=args.direction,
                message=message,
            ),
        )

    previous_card = find_episode_card(panel_root, previous_episode)
    if previous_card is None:
        message = f"选集面板中没有可操作的第{previous_episode}集。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(
                stage="previous_episode",
                direction=args.direction,
                message=message,
            ),
        )

    previous_x, previous_y = center(previous_card)
    tap_seconds = adb_tap(serial, previous_x, previous_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "previous_episode",
            "command": "input tap",
            "current_episode": current_episode,
            "target_episode": previous_episode,
            "x": previous_x,
            "y": previous_y,
            "seconds": tap_seconds,
        }
    )
    return finish(
        status="executed",
        message=(
            f"Selected iQIYI episode {previous_episode} from current episode "
            f"{current_episode} at ({previous_x}, {previous_y})."
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
