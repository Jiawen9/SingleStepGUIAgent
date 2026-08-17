"""Persist one engine pipeline run and visualize its selected action."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from engines.vla.client import build_system_prompt, build_user_prompt
from execution.executor import vla_coordinate_to_pixel
from contracts import (
    ActionSelection,
    EngineResult,
    ExecutionCommand,
    ExecutionInput,
    ExecutionResult,
    ScreenSnapshot,
)
from output.serialization import action_as_prompt_object


_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class ArtifactPaths:
    directory: Path
    original_image: Path
    done_image: Path
    draw_image: Path
    prompt: Path
    response: Path
    result: Path


def save_run_artifacts(
    *,
    project_root: Path,
    case_id: str,
    instruction: str,
    snapshot: ScreenSnapshot,
    raw_response: dict[str, Any],
    app_package: str | None = None,
) -> ArtifactPaths:
    """Compatibility helper that saves all pre-execution artifacts."""
    paths = prepare_run_artifacts(project_root=project_root, case_id=case_id)
    save_prompt(
        paths=paths,
        instruction=instruction,
        app_package=app_package,
    )
    save_original_screenshot(paths=paths, snapshot=snapshot)
    save_model_response(paths=paths, raw_response=raw_response)
    return paths


def prepare_run_artifacts(*, project_root: Path, case_id: str) -> ArtifactPaths:
    """Create a clean case directory before observation or model inference."""
    safe_case_id = validate_case_id(case_id)
    directory = project_root / "screenshots" / safe_case_id
    directory.mkdir(parents=True, exist_ok=True)

    paths = ArtifactPaths(
        directory=directory,
        original_image=directory / f"{safe_case_id}.png",
        done_image=directory / f"{safe_case_id}_done.png",
        draw_image=directory / f"{safe_case_id}_draw.png",
        prompt=directory / "prompt.txt",
        response=directory / "response.json",
        result=directory / "result.json",
    )
    # A reused case ID must not expose artifacts from the previous run.
    artifact_paths = (
        paths.original_image,
        paths.done_image,
        paths.draw_image,
        paths.prompt,
        paths.response,
        paths.result,
    )
    for stale_path in artifact_paths:
        if stale_path.is_file():
            stale_path.unlink()
        temporary_path = stale_path.with_name(stale_path.name + ".tmp")
        if temporary_path.is_file():
            temporary_path.unlink()
    xml_prefix = safe_case_id + "_"
    for stale_path in directory.iterdir():
        name = stale_path.name
        if not stale_path.is_file() or not name.startswith(xml_prefix):
            continue
        suffix = name[len(xml_prefix) :]
        if suffix.endswith(".xml") and suffix[:-4].isdigit():
            stale_path.unlink()
        elif suffix.endswith(".xml.tmp") and suffix[:-8].isdigit():
            stale_path.unlink()
    return paths


def save_prompt(
    *,
    paths: ArtifactPaths,
    instruction: str,
    app_package: str | None = None,
) -> None:
    _atomic_write_text(
        paths.prompt,
        format_prompt_record(instruction, app_package=app_package),
    )


def save_original_screenshot(
    *, paths: ArtifactPaths, snapshot: ScreenSnapshot
) -> None:
    _atomic_write_bytes(paths.original_image, snapshot.png)


def save_model_response(
    *, paths: ArtifactPaths, raw_response: dict[str, Any]
) -> None:
    content = json.dumps(
        extract_model_output(raw_response),
        ensure_ascii=False,
        indent=2,
    )
    _atomic_write_text(paths.response, content)


def save_done_screenshot(
    *,
    paths: ArtifactPaths,
    snapshot: ScreenSnapshot,
) -> None:
    """Save the clean screenshot captured after action completion."""
    _atomic_write_bytes(paths.done_image, snapshot.png)


def save_draw_screenshot(
    *,
    paths: ArtifactPaths,
    snapshot: ScreenSnapshot,
    selection: ActionSelection,
) -> None:
    """Draw the selected action on the original pre-model screenshot."""
    annotated = annotate_screenshot(snapshot, selection)
    _atomic_write_bytes(paths.draw_image, annotated)


def save_execution_result(
    *,
    paths: ArtifactPaths,
    selection: ActionSelection | None,
    status: str,
    message: str,
    timings_seconds: dict[str, float],
    timing_details: dict[str, list[dict[str, Any]]] | None = None,
    rejection: dict[str, Any] | None = None,
    decision_source: str | None = None,
    route: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "message": message,
        "timing_seconds": {
            key: round(value, 6) for key, value in timings_seconds.items()
        },
    }
    if timing_details is not None:
        payload["timing_details"] = {
            category: [
                {
                    key: (
                        round(float(value), 6)
                        if key == "seconds"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        else value
                    )
                    for key, value in entry.items()
                }
                for entry in entries
            ]
            for category, entries in timing_details.items()
        }
    if rejection is not None:
        payload["rejection"] = rejection
    if decision_source is not None:
        payload["decision_source"] = decision_source
    if route is not None:
        payload["route"] = route
    if selection is not None:
        payload["action"] = action_as_prompt_object(selection)
    _atomic_write_text(
        paths.result,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def save_pipeline_result(
    *,
    paths: ArtifactPaths,
    status: str,
    execution_input: ExecutionInput | None,
    engine_results: tuple[EngineResult, ...] = (),
    selected_engine_result: EngineResult | None = None,
    command: ExecutionCommand | None = None,
    execution: ExecutionResult | None = None,
    timings_seconds: dict[str, float] | None = None,
    error: str | None = None,
) -> None:
    """Write the versioned, engine-neutral run record."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "input": None,
        "engine_results": [result.as_dict() for result in engine_results],
        "selected_engine_result": (
            selected_engine_result.as_dict()
            if selected_engine_result is not None
            else None
        ),
        "command": command.as_dict() if command is not None else None,
        "execution": None,
        "timings_seconds": {
            key: round(value, 6)
            for key, value in (timings_seconds or {}).items()
        },
    }
    if execution_input is not None:
        payload["input"] = {
            "case_id": execution_input.case_id,
            "instruction": execution_input.instruction,
            "app_package": execution_input.app_package,
            "serial": execution_input.snapshot.serial,
            "screenshot": {
                "width": execution_input.snapshot.width,
                "height": execution_input.snapshot.height,
            },
            "initial_xml": (
                execution_input.initial_xml.name
                if execution_input.initial_xml is not None
                else None
            ),
            "diagnostics": dict(execution_input.input_diagnostics),
        }
    if execution is not None:
        payload["execution"] = {
            "status": execution.status,
            "action_name": execution.action_name,
            "message": execution.message,
            "rejection": execution.rejection,
            "timings_seconds": {
                key: round(value, 6)
                for key, value in execution.timings_seconds.items()
            },
            "timing_details": execution.timing_details,
        }
    if error is not None:
        payload["error"] = error
    _atomic_write_text(
        paths.result,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def validate_case_id(case_id: str) -> str:
    value = case_id.strip()
    if not value:
        raise ValueError("Case ID cannot be empty.")
    if len(value) > 100:
        raise ValueError("Case ID cannot exceed 100 characters.")
    if value in {".", ".."} or value.endswith((" ", ".")):
        raise ValueError("Case ID is not a valid Windows folder name.")
    if _INVALID_WINDOWS_NAME.search(value):
        raise ValueError("Case ID contains an invalid Windows filename character.")
    if value.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        raise ValueError("Case ID uses a reserved Windows filename.")
    return value


def format_prompt_record(
    instruction: str,
    *,
    app_package: str | None = None,
) -> str:
    """Return only the textual content sent in the system and user messages."""
    return (
        f"{build_system_prompt(app_package)}\n\n"
        f"{build_user_prompt(instruction)}"
    )


def extract_model_output(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Remove API envelope metadata and retain only assistant output fields."""
    try:
        message = raw_response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Cannot archive model output from a malformed response.") from error
    if not isinstance(message, dict):
        raise ValueError("Model output message must be a JSON object.")
    output: dict[str, Any] = {}
    for key, value in message.items():
        if key == "role" or value is None or value == "":
            continue
        output[key] = value
    return output


def annotate_screenshot(
    snapshot: ScreenSnapshot, selection: ActionSelection
) -> bytes:
    try:
        with Image.open(BytesIO(snapshot.png)) as source:
            source.load()
            image = source.convert("RGB")
    except (OSError, ValueError) as error:
        raise ValueError("Unable to open the original screenshot for annotation.") from error

    if image.size != (snapshot.width, snapshot.height):
        raise ValueError("Original screenshot dimensions do not match the snapshot metadata.")

    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    scale = max(1.0, min(width, height) / 1000)
    line_width = max(5, round(7 * scale))
    marker_radius = max(14, round(18 * scale))

    if selection.name == "click":
        x = vla_coordinate_to_pixel(selection.arguments["x"], width)
        y = vla_coordinate_to_pixel(selection.arguments["y"], height)
        _draw_click_marker(draw, x, y, marker_radius, line_width)
        label = None
    elif selection.name == "swipe":
        x1 = vla_coordinate_to_pixel(selection.arguments["x1"], width)
        y1 = vla_coordinate_to_pixel(selection.arguments["y1"], height)
        x2 = vla_coordinate_to_pixel(selection.arguments["x2"], width)
        y2 = vla_coordinate_to_pixel(selection.arguments["y2"], height)
        _draw_swipe_arrow(draw, x1, y1, x2, y2, marker_radius, line_width)
        label = None
    else:
        label = _action_label(selection)

    if label is not None:
        _draw_label(draw, label, width, height)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _action_label(selection: ActionSelection) -> str:
    return json.dumps(
        action_as_prompt_object(selection),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _draw_click_marker(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    radius: int,
    line_width: int,
) -> None:
    outer = (x - radius, y - radius, x + radius, y + radius)
    draw.ellipse(outer, fill=(255, 45, 45, 70), outline=(255, 255, 255, 255), width=line_width + 3)
    draw.ellipse(outer, outline=(255, 35, 35, 255), width=line_width)
    arm = round(radius * 1.6)
    draw.line((x - arm, y, x + arm, y), fill=(255, 35, 35, 255), width=line_width)
    draw.line((x, y - arm, x, y + arm), fill=(255, 35, 35, 255), width=line_width)


def _draw_swipe_arrow(
    draw: ImageDraw.ImageDraw,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    line_width: int,
) -> None:
    color = (255, 35, 35, 255)
    draw.ellipse(
        (x1 - radius, y1 - radius, x1 + radius, y1 + radius),
        fill=(255, 255, 255, 220),
        outline=color,
        width=line_width,
    )
    draw.line((x1, y1, x2, y2), fill=(255, 255, 255, 255), width=line_width + 5)
    draw.line((x1, y1, x2, y2), fill=color, width=line_width)

    dx = x2 - x1
    dy = y2 - y1
    distance = math.hypot(dx, dy)
    if distance == 0:
        return
    ux, uy = dx / distance, dy / distance
    head = max(radius * 2.2, line_width * 5)
    wing = head * 0.55
    base_x = x2 - ux * head
    base_y = y2 - uy * head
    perpendicular_x, perpendicular_y = -uy, ux
    points = [
        (x2, y2),
        (base_x + perpendicular_x * wing, base_y + perpendicular_y * wing),
        (base_x - perpendicular_x * wing, base_y - perpendicular_y * wing),
    ]
    draw.polygon(points, fill=color)


def _draw_label(
    draw: ImageDraw.ImageDraw, label: str, width: int, height: int
) -> None:
    font_size = max(18, round(min(width, height) / 35))
    font = _load_font(font_size)
    margin = max(12, round(font_size * 0.65))
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    available_width = max(1, width - margin * 2)
    while text_width > available_width and font_size > 12:
        font_size -= 1
        font = _load_font(font_size)
        margin = max(8, round(font_size * 0.65))
        available_width = max(1, width - margin * 2)
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
    if text_width > available_width:
        label = label[: max(12, int(len(label) * available_width / text_width) - 1)] + "…"
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
    box = (0, 0, width, min(height, text_height + margin * 2))
    draw.rectangle(box, fill=(8, 18, 35, 205))
    draw.text(
        (margin, margin - text_box[1]),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows_directory / "Fonts" / "msyh.ttc",
        windows_directory / "Fonts" / "segoeui.ttf",
        Path("DejaVuSans.ttf"),
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()
