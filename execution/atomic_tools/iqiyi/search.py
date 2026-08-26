#!/usr/bin/env python3
"""Search iQIYI using one query supplied by the VLA action."""

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
    hierarchy_timing_detail,
    load_hierarchy,
    nearest_clickable_ancestor,
    parent_map,
    writer_from_arguments,
)


ADB_PATH = Path(os.environ.get("ADB_PATH", r"D:\platform-tools\adb.exe"))
SEARCH_ENTRY_ID = "com.qiyi.video:id/layout_search"
SEARCH_BUTTON_ID = "com.qiyi.video:id/btn_search"
SEARCH_ENTRY_NAMES = {
    "layout_search",
    "search_layout",
    "id_home_search",
}
EDIT_TEXT_CLASS = "android.widget.EditText"


def resource_name(resource_id: str) -> str:
    """Return the resource name without an app-specific package prefix."""
    return resource_id.rsplit("/", 1)[-1]


def find_search_box_semantically(root):
    """Find a search-box description and walk to its clickable ancestor."""
    parents = parent_map(root)
    for node in root.iter("node"):
        for attribute in ("text", "content-desc"):
            value = node.get(attribute, "").strip()
            if not value or not (
                value == "搜索框" or value.startswith("搜索框 ")
            ):
                continue
            clickable = nearest_clickable_ancestor(node, parents)
            if clickable is not None:
                return clickable
    return None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open iQIYI search, replace the query text, and submit it."
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb device serial. Required only when multiple devices are connected.",
    )
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.0,
        help="Seconds to wait before the single search-page hierarchy dump.",
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


def find_search_entry(root):
    # Tablet builds expose com.qiyi.video.pad and often describe the field as
    # "搜索框 <current query>". Resolve that semantic node to its clickable
    # parent before falling back to the resource ID.
    # The set-top-box home page exposes a clickable ``id_home_search`` child
    # inside a non-clickable ``search_layout`` container. Prefer an explicitly
    # clickable resource match so tapping never depends on the wrapper.
    if uses_semantic_matching():
        semantic = find_search_box_semantically(root)
        if semantic is not None:
            return semantic
        semantic_search = find_clickable_by_semantic_text(root, {"搜索"})
        return semantic_search[0] if semantic_search is not None else None

    direct = next(
        (
            node
            for node in root.iter("node")
            if resource_name(node.get("resource-id", ""))
            in SEARCH_ENTRY_NAMES
            and node.get("clickable") == "true"
            and node.get("visible-to-user") != "false"
            and node.get("enabled") != "false"
        ),
        None,
    )
    if direct is not None:
        return direct

    semantic = find_search_box_semantically(root)
    if semantic is not None:
        return semantic

    # Some layouts put the click listener on an ancestor of the visible
    # ``搜索`` label without exposing a stable entry resource ID.
    semantic_search = find_clickable_by_semantic_text(root, {"搜索"})
    if semantic_search is not None:
        return semantic_search[0]

    expected_name = resource_name(SEARCH_ENTRY_ID)
    return next(
        (
            node
            for node in root.iter("node")
            if resource_name(node.get("resource-id", "")) == expected_name
            and node.get("visible-to-user") != "false"
            and node.get("enabled") != "false"
        ),
        None,
    )


def find_focused_edit_text(root):
    candidates = []
    for node in root.iter("node"):
        if (
            node.get("class") != EDIT_TEXT_CLASS
            or node.get("focused") != "true"
            or node.get("visible-to-user") == "false"
            or node.get("enabled") == "false"
        ):
            continue
        try:
            bounds(node)
        except RuntimeError:
            continue
        candidates.append(node)
    return candidates[0] if len(candidates) == 1 else None


def find_unique_edit_text(root):
    """Return the only visible enabled EditText, regardless of focus state."""
    candidates = [
        node
        for node in root.iter("node")
        if node.get("class") == EDIT_TEXT_CLASS
        and node.get("visible-to-user") != "false"
        and node.get("enabled") != "false"
    ]
    return candidates[0] if len(candidates) == 1 else None


def send_query_with_fast_input_ime(device, query: str) -> float:
    """Inject Unicode text through uiautomator2's FastInputIME."""
    started_at = time.perf_counter()
    fast_ime_enabled = False
    try:
        device.set_fastinput_ime(True)
        fast_ime_enabled = True
        device.send_keys(query, clear=True)
    finally:
        if fast_ime_enabled:
            device.set_fastinput_ime(False)
    return time.perf_counter() - started_at


def find_search_button(root):
    semantic = find_clickable_by_semantic_text(root, {"搜索"})
    if semantic is not None:
        return semantic[0]

    if uses_semantic_matching():
        return None

    expected_name = resource_name(SEARCH_BUTTON_ID)
    return next(
        (
            node
            for node in root.iter("node")
            if resource_name(node.get("resource-id", "")) in {
                expected_name,
                "right_search_icon",
            }
            and node.get("clickable") == "true"
            and node.get("visible-to-user") != "false"
            and node.get("enabled") != "false"
        ),
        None,
    )


def find_active_search_controls(root):
    edit_text = find_focused_edit_text(root)
    search_button = find_search_button(root)
    if edit_text is None or search_button is None:
        return None
    return edit_text, search_button


def is_search_page_loading(root) -> bool:
    return any(
        node.get("text", "").strip() == "加载中"
        and node.get("visible-to-user") != "false"
        for node in root.iter("node")
    )


def rejection_payload(*, stage: str, message: str) -> dict:
    return {
        "source": "atomic_tool",
        "stage": stage,
        "message": message,
    }


def main() -> int:
    args = parse_arguments()
    query = args.query.strip()
    if not query or len(query) > 100 or any(ord(character) < 32 for character in query):
        raise ValueError("--query must contain 1 to 100 printable characters.")
    if args.page_delay < 0:
        raise ValueError("--page-delay cannot be negative.")

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

    if args.initial_xml is not None:
        initial_root = load_hierarchy(args.initial_xml)
    else:
        device = ensure_device()
        hierarchy = dump_device_hierarchy(device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        initial_root = hierarchy.root

    active_controls = find_active_search_controls(initial_root)
    last_search_root = initial_root
    if active_controls is None:
        search_entry = find_search_entry(initial_root)
        if search_entry is None:
            message = "当前界面没有可操作的爱奇艺搜索框。"
            return finish(
                status="rejected",
                message=message,
                rejection=rejection_payload(stage="search_entry", message=message),
            )

        entry_x, entry_y = center(search_entry)
        tap_seconds = adb_tap(serial, entry_x, entry_y)
        adb_seconds += tap_seconds
        adb_details.append(
            {
                "operation": "open_search_page",
                "command": "input tap",
                "x": entry_x,
                "y": entry_y,
                "seconds": tap_seconds,
            }
        )

        active_device = ensure_device()
        if args.page_delay:
            time.sleep(args.page_delay)
        hierarchy = dump_device_hierarchy(active_device, writer=writer)
        dump_seconds += hierarchy.dump_seconds
        dump_details.append(hierarchy_timing_detail(hierarchy))
        last_search_root = hierarchy.root
        active_controls = find_active_search_controls(last_search_root)

    if active_controls is None:
        if is_search_page_loading(last_search_root):
            message = "搜索页面仍在加载，等待超时，未输入或提交搜索词。"
            stage = "search_page_loading"
        else:
            message = "搜索页中没有找到唯一的聚焦输入框和搜索按钮。"
            stage = "search_controls"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(stage=stage, message=message),
        )

    _, search_button = active_controls
    active_device = ensure_device()
    try:
        type_seconds = send_query_with_fast_input_ime(active_device, query)
    except Exception as error:
        raise RuntimeError("Unable to type the iQIYI search query.") from error
    adb_seconds += type_seconds
    adb_details.append(
        {
            "operation": "type_search_query",
            "command": "uiautomator2 FastInputIME send_keys",
            "clear": True,
            "characters": len(query),
            "seconds": type_seconds,
        }
    )

    # Never submit until the live hierarchy confirms that the complete query
    # reached the EditText. This prevents partial/garbled CJK input such as the
    # set-top box turning "海绵宝宝" into a single Latin letter.
    verification = dump_device_hierarchy(active_device, writer=writer)
    dump_seconds += verification.dump_seconds
    dump_details.append(hierarchy_timing_detail(verification))
    verified_edit = find_focused_edit_text(verification.root)
    if verified_edit is None:
        verified_edit = find_unique_edit_text(verification.root)
    actual_query = verified_edit.get("text", "") if verified_edit is not None else ""
    if actual_query != query:
        message = (
            "搜索词输入校验失败，未提交搜索："
            f"期望 {query!r}，实际 {actual_query!r}。"
        )
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(stage="query_input", message=message),
        )

    verified_button = find_search_button(verification.root)
    if verified_button is None:
        message = "搜索词已输入，但搜索按钮不可见，未提交搜索。"
        return finish(
            status="rejected",
            message=message,
            rejection=rejection_payload(stage="search_button", message=message),
        )
    search_button = verified_button

    button_x, button_y = center(search_button)
    tap_seconds = adb_tap(serial, button_x, button_y)
    adb_seconds += tap_seconds
    adb_details.append(
        {
            "operation": "submit_search",
            "command": "input tap",
            "x": button_x,
            "y": button_y,
            "seconds": tap_seconds,
        }
    )
    return finish(
        status="executed",
        message=f"Submitted iQIYI search query {query!r}.",
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
