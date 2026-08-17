"""Prompt-aligned local validation for the model's flat action catalog."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from contracts import ActionSelection


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    parameters: dict[str, Any]


def build_action_specs(
    width: int,
    height: int,
    app_action_names: frozenset[str] | set[str] | None = None,
) -> tuple[ActionSpec, ...]:
    # Runtime validation uses the real screenshot dimensions. The model action
    # space is defined by the prompt; this local catalog only supplies the
    # parameter constraints needed to validate the returned JSON. Keep it
    # in Python so runtime execution never depends on a schema file.
    del width, height
    if app_action_names is None:
        return ACTION_SPECS
    common = {"click", "swipe", "type", "reject"}
    allowed = common | set(app_action_names)
    return tuple(spec for spec in ACTION_SPECS if spec.name in allowed)


def normalize_action(selection: ActionSelection) -> ActionSelection:
    """Normalize common numeric representations returned by compatible VLA APIs."""
    coordinate_fields: tuple[str, ...]
    if selection.name == "click":
        coordinate_fields = ("x", "y")
    elif selection.name == "swipe":
        coordinate_fields = ("x1", "y1", "x2", "y2")
    else:
        return selection

    arguments = dict(selection.arguments)
    for field in coordinate_fields:
        if field in arguments:
            arguments[field] = _coerce_number(arguments[field])
    return ActionSelection(
        selection.name,
        arguments,
        prompt_action=selection.prompt_action,
    )


def _object_parameters(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_EMPTY_PARAMETERS = _object_parameters({}, [])


# This catalog is deliberately local and compact. It mirrors the action names
# and parameter constraints described in api_client.SYSTEM_PROMPT, but it is
# not sent to the model and is not loaded from a runtime JSON file.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        "click",
        "点击普通可见目标；坐标使用原始截图的 0～1000 千分位。",
        _object_parameters(
            {"x": {"type": "number"}, "y": {"type": "number"}},
            ["x", "y"],
        ),
    ),
    ActionSpec(
        "swipe",
        "滑动一次；坐标使用原始截图的 0～1000 千分位。",
        _object_parameters(
            {
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
            },
            ["x1", "y1", "x2", "y2"],
        ),
    ),
    ActionSpec("type", "向当前已聚焦输入框追加文本。", _object_parameters(
        {"text": {"type": "string", "maxLength": 1000}}, ["text"]
    )),
    ActionSpec("player_pause", "暂停当前正在播放的视频。", _EMPTY_PARAMETERS),
    ActionSpec(
        "player_seek_to_start",
        "将视频定位到开头，不改变当前播放或暂停状态。",
        _EMPTY_PARAMETERS,
    ),
    ActionSpec(
        "player_previous_episode",
        "播放上一集；不存在上一集或集数信息不可见时拒绝。",
        _EMPTY_PARAMETERS,
    ),
    ActionSpec(
        "player_next_episode",
        "播放下一集；下一集入口不可见时拒绝。",
        _EMPTY_PARAMETERS,
    ),
    ActionSpec(
        "player_set_quality",
        "切换当前视频清晰度，auto 表示智能。",
        _object_parameters(
            {
                "quality": {
                    "type": "string",
                    "enum": ["auto", "1080p", "720p", "480p"],
                }
            },
            ["quality"],
        ),
    ),
    ActionSpec(
        "player_set_playback_speed",
        "切换当前视频播放倍速。",
        _object_parameters(
            {
                "speed": {
                    "type": "string",
                    "enum": ["0.75x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x"],
                }
            },
            ["speed"],
        ),
    ),
    ActionSpec(
        "player_search",
        "在当前可搜索页面中搜索指定内容。",
        _object_parameters(
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                }
            },
            ["query"],
        ),
    ),
    ActionSpec(
        "reject",
        "无法可靠执行时拒绝；目标不可见时 reason_type 输出 TARGET_NOT_VISIBLE；用户提出的目标不受当前场景支持时输出 UNSUPPORTED_TARGET。",
        _object_parameters(
            {
                "reason_type": {
                    "type": "string",
                    "enum": ["TARGET_NOT_VISIBLE", "UNSUPPORTED_TARGET"],
                }
            },
            ["reason_type"],
        ),
    ),
)


def validate_action(
    selection: ActionSelection, specs: tuple[ActionSpec, ...], width: int, height: int
) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("Screen dimensions must be positive.")
    known = {spec.name for spec in specs}
    if selection.name not in known:
        raise ValueError(f"Model selected an unknown action: {selection.name}")
    if not isinstance(selection.arguments, dict):
        raise ValueError("Action arguments must be a JSON object.")

    arguments = selection.arguments
    if selection.name == "click":
        _require_exact_keys(arguments, {"x", "y"})
        _vla_coordinate(arguments["x"], "x")
        _vla_coordinate(arguments["y"], "y")
    elif selection.name == "swipe":
        _require_exact_keys(arguments, {"x1", "y1", "x2", "y2"})
        _vla_coordinate(arguments["x1"], "x1")
        _vla_coordinate(arguments["x2"], "x2")
        _vla_coordinate(arguments["y1"], "y1")
        _vla_coordinate(arguments["y2"], "y2")
        if arguments["x1"] == arguments["x2"] and arguments["y1"] == arguments["y2"]:
            raise ValueError("Swipe start and end points cannot be identical.")
    elif selection.name == "type":
        _require_exact_keys(arguments, {"text"})
        text = arguments["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 1000
            or any(ord(character) < 32 for character in text)
        ):
            raise ValueError(
                "Type text must contain 1 to 1000 printable characters."
            )
    elif selection.name == "player_set_quality":
        _require_exact_keys(arguments, {"quality"})
        if arguments["quality"] not in {"auto", "1080p", "720p", "480p"}:
            raise ValueError("Invalid iQIYI quality.")
    elif selection.name == "player_set_playback_speed":
        _require_exact_keys(arguments, {"speed"})
        if arguments["speed"] not in {
            "0.75x",
            "1.0x",
            "1.25x",
            "1.5x",
            "2.0x",
            "3.0x",
        }:
            raise ValueError("Invalid iQIYI playback speed.")
    elif selection.name == "player_search":
        _require_exact_keys(arguments, {"query"})
        query = arguments["query"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 100
            or any(ord(character) < 32 for character in query)
        ):
            raise ValueError(
                "iQIYI search query must contain 1 to 100 printable characters."
            )
    elif selection.name.startswith("iqiyi_"):
        _require_exact_keys(arguments, set())
    elif selection.name == "reject":
        _require_exact_keys(arguments, {"reason_type"})
        allowed = {
            "TARGET_NOT_VISIBLE",
            "UNSUPPORTED_TARGET",
        }
        if arguments["reason_type"] not in allowed:
            raise ValueError("Invalid reject reason_type.")


def _require_exact_keys(arguments: dict[str, Any], expected: set[str]) -> None:
    actual = set(arguments)
    if actual != expected:
        raise ValueError(
            f"Invalid action arguments; expected {sorted(expected)}, got {sorted(actual)}."
        )


def _vla_coordinate(value: Any, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1000
    ):
        raise ValueError(f"{label} must be a number from 0 to 1000; got {value!r}.")


def _coerce_number(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        number = float(stripped)
    except ValueError:
        return value
    if not math.isfinite(number):
        return value
    return int(number) if number.is_integer() else number
