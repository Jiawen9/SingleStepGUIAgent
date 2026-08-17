"""Fast UI hierarchy dumps, XML archiving, and player location helpers."""

from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
PLAYER_DESCRIPTION = "视频播放器"


@dataclass(frozen=True)
class HierarchyDump:
    xml_text: str
    root: ElementTree.Element
    dump_seconds: float
    path: Path | None


@dataclass(frozen=True)
class XmlExecutionContext:
    initial_xml: Path
    output_dir: Path
    case_id: str
    start_index: int = 1


class XmlArchiveWriter:
    """Save successive hierarchy snapshots as <case_id>_<index>.xml."""

    def __init__(
        self,
        *,
        output_dir: Path | None = None,
        case_id: str | None = None,
        start_index: int = 1,
    ):
        if (output_dir is None) != (case_id is None):
            raise ValueError("XML output directory and case ID must be provided together.")
        if start_index < 0:
            raise ValueError("XML start index cannot be negative.")
        if case_id is not None:
            validate_xml_case_id(case_id)
            if not output_dir.is_dir():
                raise FileNotFoundError(f"XML output directory does not exist: {output_dir}")
        self.output_dir = output_dir
        self.case_id = case_id
        self.next_index = start_index

    def save(self, xml_text: str) -> Path | None:
        if self.output_dir is None or self.case_id is None:
            return None
        path = xml_artifact_path(self.output_dir, self.case_id, self.next_index)
        self.next_index += 1
        atomic_write_xml(path, xml_text)
        return path


def add_xml_archive_arguments(parser: Any) -> None:
    """Add internal archive arguments shared by the iQIYI atomic tools."""
    parser.add_argument("--initial-xml", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--xml-output-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--xml-case-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--xml-start-index",
        type=int,
        default=1,
        help=argparse.SUPPRESS,
    )


def writer_from_arguments(args: Any) -> XmlArchiveWriter:
    return XmlArchiveWriter(
        output_dir=args.xml_output_dir,
        case_id=args.xml_case_id,
        start_index=args.xml_start_index,
    )


def xml_artifact_path(directory: Path, case_id: str, index: int) -> Path:
    validate_xml_case_id(case_id)
    if index < 0:
        raise ValueError("XML index cannot be negative.")
    return directory / f"{case_id}_{index}.xml"


def validate_xml_case_id(case_id: str) -> None:
    if not case_id or Path(case_id).name != case_id or "/" in case_id or "\\" in case_id:
        raise ValueError("XML case ID must be one filename component.")


def atomic_write_xml(path: Path, xml_text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(xml_text, encoding="utf-8")
    temporary.replace(path)


def parse_hierarchy(xml_text: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise RuntimeError(f"Unable to parse hierarchy snapshot: {error}") from error


def load_hierarchy(path: Path) -> ElementTree.Element:
    try:
        xml_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Unable to read initial hierarchy: {path}") from error
    return parse_hierarchy(xml_text)


def dump_device_hierarchy(
    device: Any,
    *,
    writer: XmlArchiveWriter | None = None,
    output_path: Path | None = None,
) -> HierarchyDump:
    if writer is not None and output_path is not None:
        raise ValueError("Use either an XML writer or an explicit output path.")
    started_at = time.perf_counter()
    xml_text = device.dump_hierarchy(
        compressed=False,
        pretty=False,
        max_depth=50,
        root_in_active=False,
    )
    dump_seconds = time.perf_counter() - started_at
    root = parse_hierarchy(xml_text)
    if output_path is not None:
        atomic_write_xml(output_path, xml_text)
        saved_path = output_path
    else:
        saved_path = writer.save(xml_text) if writer is not None else None
    return HierarchyDump(xml_text, root, dump_seconds, saved_path)


def fast_dump_to_file(
    *, adb_path: Path, serial: str, output_path: Path
) -> HierarchyDump:
    """Connect to uiautomator2 and archive one no-idle hierarchy snapshot."""
    os.environ["ADBUTILS_ADB_PATH"] = str(adb_path)
    try:
        import uiautomator2 as u2
    except ImportError as error:
        raise RuntimeError(
            "uiautomator2 is not installed. Run: python -m pip install -U uiautomator2"
        ) from error

    device = u2.connect(serial)
    device.jsonrpc.setConfigurator(
        {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
    )
    return dump_device_hierarchy(device, output_path=output_path)


def bounds(node: ElementTree.Element) -> tuple[int, int, int, int]:
    value = node.get("bounds", "")
    match = BOUNDS_PATTERN.fullmatch(value)
    if not match:
        raise RuntimeError(f"Invalid node bounds: {value!r}")
    left, top, right, bottom = map(int, match.groups())
    if right <= left or bottom <= top:
        raise RuntimeError(f"Empty node bounds: {value!r}")
    return left, top, right, bottom


def center(node: ElementTree.Element) -> tuple[int, int]:
    left, top, right, bottom = bounds(node)
    return (left + right) // 2, (top + bottom) // 2


def semantic_texts(node: ElementTree.Element) -> tuple[str, ...]:
    """Return non-empty visible text exposed by a hierarchy node."""
    values = []
    for attribute in ("text", "content-desc"):
        value = node.get(attribute, "").strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def parent_map(
    root: ElementTree.Element,
) -> dict[ElementTree.Element, ElementTree.Element]:
    """Build child-to-parent links for upward semantic target resolution."""
    return {child: parent for parent in root.iter() for child in parent}


def nearest_clickable_ancestor(
    node: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
    *,
    max_depth: int = 8,
) -> ElementTree.Element | None:
    """Resolve semantic text to the nearest usable clickable bounds."""
    candidate: ElementTree.Element | None = node
    for _ in range(max_depth + 1):
        if candidate is None:
            break
        if (
            candidate.get("clickable") == "true"
            and candidate.get("visible-to-user") != "false"
            and candidate.get("enabled") != "false"
        ):
            try:
                bounds(candidate)
            except RuntimeError:
                pass
            else:
                return candidate
        candidate = parents.get(candidate)
    return None


def spatially_corresponding_clickable(
    root: ElementTree.Element,
    semantic_node: ElementTree.Element,
) -> ElementTree.Element | None:
    """Find the smallest clickable region covering a semantic label center.

    Some Android hierarchies expose a visual button and its label as sibling
    nodes. In that case there is no clickable ancestor, but both occupy the
    same screen region.
    """
    try:
        label_left, label_top, label_right, label_bottom = bounds(semantic_node)
    except RuntimeError:
        return None
    label_x = (label_left + label_right) // 2
    label_y = (label_top + label_bottom) // 2
    candidates: list[tuple[int, ElementTree.Element]] = []
    for candidate in root.iter("node"):
        if (
            candidate.get("clickable") != "true"
            or candidate.get("visible-to-user") == "false"
            or candidate.get("enabled") == "false"
        ):
            continue
        try:
            left, top, right, bottom = bounds(candidate)
        except RuntimeError:
            continue
        if left <= label_x < right and top <= label_y < bottom:
            candidates.append(((right - left) * (bottom - top), candidate))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def find_clickable_by_semantic_text(
    root: ElementTree.Element,
    labels: set[str] | tuple[str, ...] | list[str],
    *,
    max_depth: int = 8,
) -> tuple[ElementTree.Element, str] | None:
    """Find exact text/description, then resolve its clickable ancestor."""
    expected = {label.strip() for label in labels if label.strip()}
    parents = parent_map(root)
    candidates: list[tuple[int, ElementTree.Element, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in root.iter("node"):
        if node.get("visible-to-user") == "false":
            continue
        for label in semantic_texts(node):
            if label not in expected:
                continue
            clickable = nearest_clickable_ancestor(
                node,
                parents,
                max_depth=max_depth,
            )
            if clickable is None:
                clickable = spatially_corresponding_clickable(root, node)
            if clickable is None:
                continue
            left, top, right, bottom = bounds(clickable)
            key = (label, clickable.get("bounds", ""))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(((right - left) * (bottom - top), clickable, label))
    if not candidates:
        return None
    _, clickable, label = min(candidates, key=lambda item: item[0])
    return clickable, label


def find_player_bounds(root: ElementTree.Element) -> tuple[int, int, int, int]:
    """Return bounds of the largest visible video-player semantic node."""
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []
    for node in root.iter("node"):
        if (
            node.get("content-desc") != PLAYER_DESCRIPTION
            or node.get("visible-to-user") == "false"
        ):
            continue
        try:
            player_bounds = bounds(node)
        except RuntimeError:
            continue
        left, top, right, bottom = player_bounds
        candidates.append(((right - left) * (bottom - top), player_bounds))
    if not candidates:
        raise RuntimeError("The visible '视频播放器' node was not found in XML snapshot 0.")
    _, player_bounds = max(candidates, key=lambda item: item[0])
    return player_bounds


def find_player_center(root: ElementTree.Element) -> tuple[int, int]:
    """Return the center of the largest visible iQIYI video-player node."""
    left, top, right, bottom = find_player_bounds(root)
    return (left + right) // 2, (top + bottom) // 2


def find_screen_center(root: ElementTree.Element) -> tuple[int, int]:
    """Infer the physical display center from all valid hierarchy bounds."""
    rectangles: list[tuple[int, int, int, int]] = []
    for node in root.iter("node"):
        try:
            rectangles.append(bounds(node))
        except RuntimeError:
            continue
    if not rectangles:
        raise RuntimeError("No valid screen bounds were found in the XML hierarchy.")
    left = min(rectangle[0] for rectangle in rectangles)
    top = min(rectangle[1] for rectangle in rectangles)
    right = max(rectangle[2] for rectangle in rectangles)
    bottom = max(rectangle[3] for rectangle in rectangles)
    return (left + right) // 2, (top + bottom) // 2


def hierarchy_timing_detail(hierarchy: HierarchyDump) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "file": hierarchy.path.name if hierarchy.path is not None else None,
        "seconds": hierarchy.dump_seconds,
    }
    if hierarchy.path is not None:
        match = re.search(r"_(\d+)\.xml$", hierarchy.path.name)
        if match:
            detail["sequence"] = int(match.group(1))
    return detail
