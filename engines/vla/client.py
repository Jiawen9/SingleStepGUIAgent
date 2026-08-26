"""OpenAI-compatible multimodal client using a prompt-defined action space."""

from __future__ import annotations

import base64
import json
import math
import re
import ssl
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any, Callable

from PIL import Image
import certifi
from qwen_vl_utils import fetch_image

from contracts import ActionSelection, ScreenSnapshot
from .prompts import load_app_prompt


VLA_MAX_IMAGE_TOKENS = 1024
QWEN_IMAGE_PATCH_SIZE = 16
QWEN_SPATIAL_MERGE_SIZE = 2
QWEN_VISUAL_TOKEN_SIDE_PIXELS = (
    QWEN_IMAGE_PATCH_SIZE * QWEN_SPATIAL_MERGE_SIZE
)
VLA_MAX_IMAGE_PIXELS = (
    VLA_MAX_IMAGE_TOKENS * QWEN_VISUAL_TOKEN_SIDE_PIXELS**2
)


BASE_SYSTEM_PROMPT = """# 角色与任务
你是一个单步 GUI Agent，根据当前截图和用户指令选择一个动作。

# 决策规则
- 从下方 Action Space 中选择且只选择一个最合适的动作。
- 基础 GUI 动作用于普通界面操作；用户意图与播放器或页面专用动作匹配时，优先选择对应的专用动作。
- 只依据截图中的可见状态决策，不猜测截图外的目标。目标不可见、状态不支持或无法可靠执行时选择 reject。
- 不输出计划、解释或多个动作。

# 输出格式
- 只输出一个合法 JSON 对象，不使用 Markdown，不添加任何其他文字。

# Action Space

## 基础 GUI 动作
- 下列动作中的坐标以当前输入图为准。
- {"action":"click","coordinate":[x,y]}: 点击普通可见目标，坐标取目标中心。
- {"action":"type","text":"要输入的文本"}: 向当前已聚焦的输入框输入文本。
- {"action":"swipe","start_coordinate":[x,y],"direction":"up","distance":"medium"}: 从指定起点按方向和距离滑动。direction 只能是 up、down、left、right，表示手指实际移动方向；distance 只能是 short、medium、long。

## 拒绝动作
- {"action":"reject"}: 无法可靠执行时拒绝。"""

# Compatibility name for code importing the common prompt constant. App
# actions are deliberately not included until build_system_prompt() is called.
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT

REFUSAL_POLICY_PROMPT = """## 单步完成性与拒绝规则
- 先判断 Action Space 中是否存在一个动作，能够独立完成用户的完整最终意图。只有答案明确为“是”时，才能选择该动作。
- 禁止把用户的完整任务降级成一个看似有帮助的局部步骤。点击应用入口、搜索入口、内容卡片或任意猜测坐标，如果执行后仍需后续动作才能完成最终意图，都必须 reject。
- 第一种必须拒绝的情况：用户要求的目标或完成动作所需的状态在当前截图中不可见、不能唯一定位或无法可靠确认。此时禁止猜测坐标。
- 第二种必须拒绝的情况：Action Space 中没有一个动作能独立完成用户的完整意图，包括多步骤任务、当前场景不支持的目标，以及只能完成局部步骤的情况。
- 用户指令包含多个操作目标时，不得只执行其中一个，也不得擅自改写为更容易的目标。
- `player_search` 只完成搜索，不等于播放搜索结果；`click` 只完成一次点击，不等于完成“打开应用并播放指定内容”等端到端任务。
- 普通点击仅在用户要求点击一个当前截图中明确可见、可唯一定位的目标时使用。不要因为某个可见元素与指令部分相关就点击它。
- 两种拒绝情况都只输出 {"action":"reject"}，不输出拒绝原因、解释或其他字段。"""

class ApiError(RuntimeError):
    """Raised when the model endpoint cannot produce one valid action."""


class VlaApiClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_base: str,
        model: str,
        timeout_seconds: float,
    ):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    @property
    def completion_url(self) -> str:
        if self.api_base.endswith("/chat/completions"):
            return self.api_base
        return self.api_base + "/chat/completions"

    def choose_action(
        self,
        *,
        instruction: str,
        snapshot: ScreenSnapshot,
        app_package: str | None = None,
        on_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[ActionSelection, dict[str, Any]]:
        model_png = resize_png_for_vla(snapshot.png)
        image_data = base64.b64encode(model_png).decode("ascii")
        system_text = build_system_prompt(app_package)
        user_text = build_user_prompt(instruction)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + image_data
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._post_json(payload)
        if on_response is not None:
            on_response(response)
        return parse_action_response(response), response

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "single-step-gui-agent/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.completion_url,
            data=body,
            method="POST",
            headers=headers,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=ssl_context,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:2000]
            raise ApiError(f"API returned HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ApiError(f"Unable to reach model API: {error.reason}") from error
        except TimeoutError as error:
            raise ApiError("Model API request timed out.") from error

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError("Model API did not return valid UTF-8 JSON.") from error
        if not isinstance(decoded, dict):
            raise ApiError("Model API response must be a JSON object.")
        return decoded


def resize_png_for_vla(png: bytes) -> bytes:
    """Prepare a screenshot with Qwen's official aspect-ratio-aware resize."""
    try:
        with Image.open(BytesIO(png)) as source:
            source.load()
            model_image = fetch_image(
                {
                    "image": source.copy(),
                    "max_pixels": VLA_MAX_IMAGE_PIXELS,
                },
                image_patch_size=QWEN_IMAGE_PATCH_SIZE,
            )
    except (OSError, ValueError, AssertionError) as error:
        raise ValueError("Unable to prepare the screenshot for VLA input.") from error

    output = BytesIO()
    model_image.save(output, format="PNG")
    return output.getvalue()


def build_system_prompt(app_package: str | None = None) -> str:
    """Build the common prompt plus actions for the detected foreground App."""
    app_prompt = load_app_prompt(app_package)
    if app_prompt is None:
        return f"{BASE_SYSTEM_PROMPT}\n\n{REFUSAL_POLICY_PROMPT}"
    reject_heading = "\n\n## 拒绝动作"
    prefix, separator, suffix = BASE_SYSTEM_PROMPT.partition(reject_heading)
    if not separator:
        raise RuntimeError("The base system prompt has no reject action section.")
    prompt = f"{prefix}\n\n{app_prompt.prompt}{separator}{suffix}"
    return f"{prompt}\n\n{REFUSAL_POLICY_PROMPT}"


def build_user_prompt(instruction: str) -> str:
    """Return the exact text content placed in the VLA user message."""
    return f"用户指令：{instruction}\n现在只输出一个符合动作空间的 JSON 对象。"


def parse_action_response(response: dict[str, Any]) -> ActionSelection:
    try:
        choices = response["choices"]
        message = choices[0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        api_message = _extract_api_error(response)
        raise ApiError(api_message or "Malformed chat completion response.") from error

    content = _content_as_text(message.get("content"))
    if not content.strip():
        raise ApiError("The model returned no action. Nothing was executed.")
    obj = _extract_json_object(content)
    if "action" in obj:
        action = obj.get("action")
        if action in {"click", "type", "swipe", "reject"}:
            return _parse_basic_action(obj)
        if not isinstance(action, str) or not action:
            raise ApiError("The flat action JSON must contain a valid action name.")
        return ActionSelection(
            action,
            {key: value for key, value in obj.items() if key != "action"},
            prompt_action=obj,
        )
    raise ApiError("The flat action JSON must contain an action field.")


SWIPE_DISTANCE_UNITS = {
    "short": 200,
    "medium": 400,
    "long": 600,
}


def _parse_basic_action(obj: dict[str, Any]) -> ActionSelection:
    action = obj.get("action")
    if action == "reject":
        _require_object_keys(obj, {"action"})
        return ActionSelection("reject", {}, prompt_action=obj)
    if action == "click":
        _require_object_keys(obj, {"action", "coordinate"})
        x, y = _coordinate_pair(obj.get("coordinate"), "coordinate")
        return ActionSelection(
            "click",
            {"x": x, "y": y},
            prompt_action=obj,
        )
    if action == "type":
        _require_object_keys(obj, {"action", "text"})
        return ActionSelection(
            "type",
            {"text": obj.get("text")},
            prompt_action=obj,
        )
    if action == "swipe":
        _require_object_keys(
            obj,
            {"action", "start_coordinate", "direction", "distance"},
        )
        x1, y1 = _coordinate_pair(
            obj.get("start_coordinate"),
            "start_coordinate",
        )
        direction = obj.get("direction")
        distance = obj.get("distance")
        if direction not in {"up", "down", "left", "right"}:
            raise ApiError("Swipe direction must be up, down, left, or right.")
        if distance not in SWIPE_DISTANCE_UNITS:
            raise ApiError("Swipe distance must be short, medium, or long.")
        delta = SWIPE_DISTANCE_UNITS[distance]
        x2, y2 = x1, y1
        if direction == "up":
            y2 = max(0, y1 - delta)
        elif direction == "down":
            y2 = min(1000, y1 + delta)
        elif direction == "left":
            x2 = max(0, x1 - delta)
        else:
            x2 = min(1000, x1 + delta)
        if x1 == x2 and y1 == y2:
            raise ApiError("Swipe start coordinate leaves no room in its direction.")
        return ActionSelection(
            "swipe",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            prompt_action=obj,
        )
    raise ApiError(f"Unknown basic action: {action!r}.")


def _require_object_keys(obj: dict[str, Any], expected: set[str]) -> None:
    if set(obj) != expected:
        raise ApiError(
            "Malformed basic action fields; expected "
            f"{sorted(expected)}, got {sorted(obj)}."
        )


def _coordinate_pair(value: Any, label: str) -> tuple[Any, Any]:
    if not isinstance(value, list) or len(value) != 2:
        raise ApiError(f"{label} must be a two-item JSON array.")
    return (
        _basic_coordinate(value[0], f"{label}[0]"),
        _basic_coordinate(value[1], f"{label}[1]"),
    )


def _basic_coordinate(value: Any, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1000
    ):
        raise ApiError(f"{label} must be a number from 0 to 1000.")
    return value


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _extract_json_object(content: str) -> dict[str, Any]:
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    candidates.append(content.strip())
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        candidates.append(content[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ApiError("The model did not return one valid action JSON object.")


def _extract_api_error(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return "Model API error: " + error["message"]
    return None
