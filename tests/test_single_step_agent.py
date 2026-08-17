from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch
from xml.etree import ElementTree

from PIL import Image

from engines.validation import (
    build_action_specs,
    normalize_action,
    validate_action,
)
from storage.artifacts import (
    _action_label,
    prepare_run_artifacts,
    save_done_screenshot,
    save_draw_screenshot,
    save_execution_result,
    save_model_response,
    save_original_screenshot,
    save_prompt,
    save_run_artifacts,
    validate_case_id,
)
from engines.vla.client import (
    ApiError,
    QWEN_VISUAL_TOKEN_SIDE_PIXELS,
    VLA_MAX_IMAGE_TOKENS,
    VlaApiClient,
    build_system_prompt,
    build_user_prompt,
    parse_action_response,
    resize_png_for_vla,
)
from device.adb import AdbController
from config import AgentConfig, load_env_file
from execution.executor import ActionExecutor, vla_coordinate_to_pixel
from contracts import ActionSelection, ScreenSnapshot
from execution.atomic_tools.iqiyi import (
    change_episode as change_episode_iqiyi,
)
from execution.atomic_tools.iqiyi import pause as pause_iqiyi
from execution.atomic_tools.iqiyi.mode import (
    MODE_ENVIRONMENT_VARIABLE,
    normalize_action_mode,
)
from execution.atomic_tools.iqiyi import (
    set_playback_speed as set_playback_speed_iqiyi,
)
from execution.atomic_tools.iqiyi import search as search_iqiyi
from execution.atomic_tools.iqiyi.change_episode import (
    find_current_episode,
    find_episode_card,
    find_episode_menu_control,
    find_next_episode_control,
)
from execution.atomic_tools.iqiyi.set_playback_speed import (
    find_current_speed,
    find_speed_control,
    find_speed_option,
)
from execution.atomic_tools.iqiyi.search import (
    find_active_search_controls,
    find_focused_edit_text,
    find_search_button,
    find_search_entry,
    is_search_page_loading,
    send_query_with_fast_input_ime,
)
from execution.atomic_tools.iqiyi.seek_to_start import (
    find_progress_bar,
    progress_start_coordinates,
)
from execution.atomic_tools.iqiyi.set_quality import (
    find_current_quality,
    find_quality_control,
    find_quality_option,
)
from execution.timing import (
    extract_atomic_result,
    extract_atomic_timing,
)
from device.xml_hierarchy import (
    XmlArchiveWriter,
    XmlExecutionContext,
    center,
    dump_device_hierarchy,
    find_player_center,
    find_screen_center,
    load_hierarchy,
    xml_artifact_path,
)


class AdbConnectionTests(unittest.TestCase):
    def test_requested_device_is_selected_without_adb_connect(self):
        controller = AdbController(Path(r"D:\platform-tools\adb.exe"))
        ready = subprocess.CompletedProcess(
            [], 0, stdout="device\n", stderr=""
        )
        with patch.object(
            controller, "_run_text", return_value=ready
        ) as run_text:
            serial = controller.select_device("HA223YB6")

        self.assertEqual(serial, "HA223YB6")
        run_text.assert_called_once_with(
            ["get-state"], serial="HA223YB6", check=False
        )

    def test_offline_requested_device_is_rejected(self):
        controller = AdbController(Path(r"D:\platform-tools\adb.exe"))
        offline = subprocess.CompletedProcess(
            [], 1, stdout="offline\n", stderr=""
        )
        with patch.object(controller, "_run_text", return_value=offline):
            with self.assertRaisesRegex(RuntimeError, "Device is unavailable"):
                controller.select_device("10.239.194.16:5555")


class ApiResponseTests(unittest.TestCase):
    def test_local_model_url_accepts_an_empty_api_key(self):
        config = AgentConfig.from_values(
            api_key="",
            api_base="http://127.0.0.1:8000/v1",
            model="local-vla",
            adb_path="adb",
        )
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.api_base, "http://127.0.0.1:8000/v1")
        self.assertEqual(config.model, "local-vla")

    def test_client_omits_authorization_when_api_key_is_empty(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def read(self):
                return b"{}"

        client = VlaApiClient(
            api_key="",
            api_base="http://127.0.0.1:8000/v1",
            model="local-vla",
            timeout_seconds=1,
        )
        with patch(
            "engines.vla.client.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            self.assertEqual(client._post_json({"model": "local-vla"}), {})

        request = urlopen.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8000/v1/chat/completions",
        )

    def test_complete_chat_completions_url_is_not_extended(self):
        client = VlaApiClient(
            api_key="",
            api_base="http://127.0.0.1:8000/v1/chat/completions",
            model="local-vla",
            timeout_seconds=1,
        )
        self.assertEqual(
            client.completion_url,
            "http://127.0.0.1:8000/v1/chat/completions",
        )

    def test_plain_json_action(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action":"click",'
                            '"coordinate":[100,200]}'
                        )
                    }
                }
            ]
        }
        action = parse_action_response(response)
        self.assertEqual(action, ActionSelection("click", {"x": 100, "y": 200}))

    def test_tool_calls_are_not_an_action_interface(self):
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "click",
                                    "arguments": '{"x": 100, "y": 200}',
                                }
                            },
                            {"function": {"name": "player_pause", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        }
        with self.assertRaises(ApiError):
            parse_action_response(response)

    def test_gateway_json_fallback_does_not_retry(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '```json\n{"action_id":"reject",'
                            '"reason_type":"TARGET_NOT_VISIBLE"}\n```'
                        )
                    }
                }
            ]
        }
        action = parse_action_response(response)
        self.assertEqual(action.name, "reject")

    def test_parses_directional_swipe_into_endpoint_coordinates(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action":"swipe",'
                            '"start_coordinate":[500,700],'
                            '"direction":"up","distance":"medium"}'
                        )
                    }
                }
            ]
        }

        action = parse_action_response(response)

        self.assertEqual(
            action,
            ActionSelection(
                "swipe",
                {"x1": 500, "y1": 700, "x2": 500, "y2": 300},
            ),
        )

    def test_parses_flat_type_action(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": '{"action":"type","text":"海绵宝宝"}'
                    }
                }
            ]
        }
        self.assertEqual(
            parse_action_response(response),
            ActionSelection("type", {"text": "海绵宝宝"}),
        )

    def test_rejects_legacy_basic_action_envelope(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_id":"click",'
                            '"arguments":{"x":100,"y":200}}'
                        )
                    }
                }
            ]
        }
        with self.assertRaises(ApiError):
            parse_action_response(response)

    def test_parses_search_action_json_content(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action":"player_search",'
                            '"query":"海绵宝宝"}'
                        )
                    }
                }
            ]
        }
        action = parse_action_response(response)
        self.assertEqual(
            action,
            ActionSelection("player_search", {"query": "海绵宝宝"}),
        )

    def test_parses_flat_app_action_without_arguments(self):
        response = {
            "choices": [
                {"message": {"content": '{"action":"player_pause"}'}}
            ]
        }
        self.assertEqual(
            parse_action_response(response),
            ActionSelection("player_pause", {}),
        )

    def test_rejects_legacy_app_action_envelope(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_id":"player_search","arguments":'
                            '{"query":"海绵宝宝"}}'
                        )
                    }
                }
            ]
        }
        with self.assertRaises(ApiError):
            parse_action_response(response)

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.specs = build_action_specs(1200, 800)

    def test_valid_click(self):
        validate_action(
            ActionSelection("click", {"x": 1000, "y": 1000}),
            self.specs,
            1200,
            800,
        )

    def test_out_of_bounds_click(self):
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection("click", {"x": 1001, "y": 10}),
                self.specs,
                1200,
                800,
            )

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            validate_action(ActionSelection("done", {}), self.specs, 1200, 800)

    def test_swipe_requires_only_four_coordinates(self):
        selection = ActionSelection(
            "swipe", {"x1": 500, "y1": 700, "x2": 500, "y2": 300}
        )
        validate_action(selection, self.specs, 1200, 800)

        swipe = next(spec for spec in self.specs if spec.name == "swipe")
        self.assertEqual(
            swipe.parameters["required"],
            ["x1", "y1", "x2", "y2"],
        )
        self.assertNotIn("duration_ms", swipe.parameters["properties"])

    def test_type_requires_one_printable_text_argument(self):
        validate_action(
            ActionSelection("type", {"text": "海绵宝宝 12"}),
            self.specs,
            1200,
            800,
        )
        spec = next(spec for spec in self.specs if spec.name == "type")
        self.assertEqual(spec.parameters["required"], ["text"])
        self.assertEqual(
            spec.parameters["properties"]["text"]["maxLength"],
            1000,
        )
        for invalid in ("", "   ", "line\nbreak", "x" * 1001):
            with self.subTest(invalid=invalid[:20]), self.assertRaises(ValueError):
                validate_action(
                    ActionSelection("type", {"text": invalid}),
                    self.specs,
                    1200,
                    800,
                )

    def test_removed_reject_reasons_are_invalid(self):
        for reason in ("NOT_APPLICABLE", "UNSUPPORTED_REQUEST"):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                validate_action(
                    ActionSelection(
                        "reject", {"reason_type": reason}
                    ),
                    self.specs,
                    1200,
                    800,
                )

        reject = next(spec for spec in self.specs if spec.name == "reject")
        enum = reject.parameters["properties"]["reason_type"]["enum"]
        self.assertEqual(enum, ["TARGET_NOT_VISIBLE", "UNSUPPORTED_TARGET"])
        self.assertEqual(reject.parameters["required"], ["reason_type"])
        self.assertNotIn("message", reject.parameters["properties"])
        self.assertNotIn(
            "description",
            reject.parameters["properties"]["reason_type"],
        )
        self.assertIn(
            "reason_type 输出 TARGET_NOT_VISIBLE",
            reject.description,
        )
        self.assertIn(
            "用户提出的目标不受当前场景支持时输出 UNSUPPORTED_TARGET",
            reject.description,
        )

    def test_reject_message_is_generated_locally(self):
        executor = ActionExecutor(SimpleNamespace(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 1200, 800, None)
        result = executor.execute(
            ActionSelection(
                "reject", {"reason_type": "UNSUPPORTED_TARGET"}
            ),
            snapshot,
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(
            result.message,
            "用户提出的目标不受当前场景支持。",
        )
        self.assertEqual(
            result.rejection,
            {"source": "vla", "reason_type": "UNSUPPORTED_TARGET"},
        )

    def test_seek_to_start_is_parameterless_and_preserves_play_state(self):
        selection = ActionSelection("player_seek_to_start", {})
        validate_action(selection, self.specs, 1200, 800)

        spec = next(
            spec for spec in self.specs if spec.name == "player_seek_to_start"
        )
        self.assertEqual(spec.parameters["properties"], {})
        self.assertIn("不改变", spec.description)

    def test_episode_tools_are_parameterless_actions(self):
        for name in ("player_previous_episode", "player_next_episode"):
            with self.subTest(name=name):
                validate_action(
                    ActionSelection(name, {}), self.specs, 1200, 800
                )
                spec = next(spec for spec in self.specs if spec.name == name)
                self.assertEqual(spec.parameters["properties"], {})
        descriptions = {
            spec.name: spec.description
            for spec in self.specs
            if spec.name in {"player_previous_episode", "player_next_episode"}
        }
        self.assertIn("不可见时拒绝", descriptions["player_previous_episode"])
        self.assertIn("不可见时拒绝", descriptions["player_next_episode"])

    def test_quality_tool_requires_one_supported_quality(self):
        for quality in ("auto", "1080p", "720p", "480p"):
            with self.subTest(quality=quality):
                validate_action(
                    ActionSelection(
                        "player_set_quality", {"quality": quality}
                    ),
                    self.specs,
                    1200,
                    800,
                )
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection(
                    "player_set_quality", {"quality": "4k"}
                ),
                self.specs,
                1200,
                800,
            )

        spec = next(
            spec for spec in self.specs if spec.name == "player_set_quality"
        )
        self.assertEqual(
            spec.parameters["properties"]["quality"]["enum"],
            ["auto", "1080p", "720p", "480p"],
        )

    def test_playback_speed_tool_requires_one_supported_speed(self):
        supported = ["0.75x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x"]
        for speed in supported:
            with self.subTest(speed=speed):
                validate_action(
                    ActionSelection(
                        "player_set_playback_speed", {"speed": speed}
                    ),
                    self.specs,
                    1200,
                    800,
                )
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection(
                    "player_set_playback_speed", {"speed": "1.75x"}
                ),
                self.specs,
                1200,
                800,
            )

        spec = next(
            spec
            for spec in self.specs
            if spec.name == "player_set_playback_speed"
        )
        self.assertEqual(
            spec.parameters["properties"]["speed"]["enum"], supported
        )

    def test_search_tool_requires_one_nonempty_printable_query(self):
        validate_action(
            ActionSelection("player_search", {"query": "海绵宝宝"}),
            self.specs,
            1200,
            2670,
        )
        for query in ("", "   ", "line\nbreak", "x" * 101):
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_action(
                    ActionSelection("player_search", {"query": query}),
                    self.specs,
                    1200,
                    2670,
                )

        spec = next(spec for spec in self.specs if spec.name == "player_search")
        query_schema = spec.parameters["properties"]["query"]
        self.assertEqual(query_schema["minLength"], 1)
        self.assertEqual(query_schema["maxLength"], 100)

    def test_vla_click_maps_to_observed_target(self):
        self.assertEqual(vla_coordinate_to_pixel(268, 1200), 321)
        self.assertEqual(vla_coordinate_to_pixel(637, 2670), 1700)

    def test_decimal_coordinates_are_valid_and_map_to_pixels(self):
        selection = normalize_action(
            ActionSelection("click", {"x": "532.4", "y": 417.5})
        )
        validate_action(selection, self.specs, 1200, 800)
        self.assertEqual(selection.arguments, {"x": 532.4, "y": 417.5})
        self.assertEqual(vla_coordinate_to_pixel(532.4, 1200), 638)

    def test_vla_canvas_edges_map_to_original_image_edges(self):
        self.assertEqual(vla_coordinate_to_pixel(0, 1920), 0)
        self.assertEqual(vla_coordinate_to_pixel(1000, 1920), 1919)
        self.assertEqual(vla_coordinate_to_pixel(1000, 1080), 1079)


class VlaImageInputTests(unittest.TestCase):
    @staticmethod
    def _png(width: int, height: int) -> bytes:
        output = BytesIO()
        Image.new("RGB", (width, height), (30, 60, 90)).save(
            output, format="PNG"
        )
        return output.getvalue()

    def test_qwen_resize_preserves_aspect_ratio_and_token_budget(self):
        for width, height in ((1200, 2670), (1920, 1080), (400, 300)):
            with self.subTest(size=(width, height)):
                resized = resize_png_for_vla(self._png(width, height))
                with Image.open(BytesIO(resized)) as image:
                    resized_width, resized_height = image.size
                self.assertEqual(
                    resized_width % QWEN_VISUAL_TOKEN_SIDE_PIXELS,
                    0,
                )
                self.assertEqual(
                    resized_height % QWEN_VISUAL_TOKEN_SIDE_PIXELS,
                    0,
                )
                self.assertLessEqual(
                    resized_width
                    * resized_height
                    // QWEN_VISUAL_TOKEN_SIDE_PIXELS**2,
                    VLA_MAX_IMAGE_TOKENS,
                )
                self.assertAlmostEqual(
                    resized_width / resized_height,
                    width / height,
                    delta=0.03,
                )
        self.assertNotEqual(
            Image.open(BytesIO(resize_png_for_vla(self._png(1200, 2670)))).size,
            (1000, 1000),
        )

    def test_request_uses_preprocessed_image_without_server_resize_options(self):
        original = self._png(1920, 1080)
        snapshot = ScreenSnapshot(original, 1920, 1080, "device-1")
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"click",'
                                '"coordinate":[500,500]}'
                            )
                        }
                    }
                ]
            }

        client = VlaApiClient(
            api_key="test-key",
            api_base="https://example.invalid/v1",
            model="test-model",
            timeout_seconds=1,
        )
        with patch.object(client, "_post_json", side_effect=fake_post):
            selection, _ = client.choose_action(
                instruction="点击中心",
                snapshot=snapshot,
            )

        self.assertEqual(selection, ActionSelection("click", {"x": 500, "y": 500}))
        self.assertNotIn("tools", captured)
        self.assertNotIn("tool_choice", captured)
        self.assertNotIn("mm_processor_kwargs", captured)
        self.assertEqual(VLA_MAX_IMAGE_TOKENS, 1024)
        self.assertEqual(
            captured["chat_template_kwargs"], {"enable_thinking": False}
        )
        image_url = captured["messages"][1]["content"][0]["image_url"]["url"]
        encoded = image_url.removeprefix("data:image/png;base64,")
        with Image.open(BytesIO(base64.b64decode(encoded))) as model_image:
            self.assertEqual(model_image.size, (1344, 768))
            self.assertLessEqual(
                model_image.width
                * model_image.height
                // QWEN_VISUAL_TOKEN_SIDE_PIXELS**2,
                VLA_MAX_IMAGE_TOKENS,
            )
    def test_request_embeds_action_space_without_native_tools(self):
        snapshot = ScreenSnapshot(self._png(1200, 2670), 1200, 2670, "device-1")
        specs = build_action_specs(snapshot.width, snapshot.height)
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"player_search",'
                                '"query":"海绵宝宝"}'
                            )
                        }
                    }
                ]
            }

        client = VlaApiClient(
            api_key="test-key",
            api_base="https://example.invalid/v1",
            model="test-model",
            timeout_seconds=1,
        )
        with patch.object(client, "_post_json", side_effect=fake_post):
            selection, _ = client.choose_action(
                instruction="搜索海绵宝宝",
                snapshot=snapshot,
                app_package="com.qiyi.video.pad",
            )

        self.assertEqual(
            selection,
            ActionSelection("player_search", {"query": "海绵宝宝"}),
        )
        self.assertNotIn("tools", captured)
        self.assertNotIn("tool_choice", captured)
        self.assertEqual(
            captured["messages"][0]["content"],
            build_system_prompt("com.qiyi.video.pad"),
        )
        system_prompt = captured["messages"][0]["content"]
        self.assertIn(
            '"action":"player_search"',
            system_prompt,
        )
        self.assertIn('"query":"搜索词"', system_prompt)
        self.assertIn('"quality":"目标清晰度"', system_prompt)
        self.assertIn('"speed":"目标倍速"', system_prompt)
        self.assertNotIn("query:string[1..100]", system_prompt)
        self.assertIn(
            '"action":"player_set_quality"',
            system_prompt,
        )
        self.assertIn(
            '"action":"player_set_playback_speed"',
            system_prompt,
        )
        self.assertIn('720、720P 规范为 "720p"', system_prompt)
        self.assertIn('“智能”规范为 "auto"', system_prompt)
        self.assertIn('2倍为 "2.0x"', system_prompt)
        self.assertNotIn("quality:1080p|720p|480p", system_prompt)
        self.assertNotIn("speed:0.75x|1.0x", system_prompt)
        self.assertNotIn('"parameters"', system_prompt)
        self.assertNotIn('"additionalProperties"', system_prompt)
        for spec in specs:
            field = "action_id" if spec.name == "reject" else "action"
            self.assertIn(f'"{field}":"{spec.name}"', system_prompt)
        self.assertEqual(
            captured["messages"][1]["content"][1]["text"],
            build_user_prompt("搜索海绵宝宝"),
        )

    def test_action_space_is_embedded_directly_in_system_prompt(self):
        specs = build_action_specs(1000, 1000)
        compact = build_system_prompt("com.qiyi.video.pad")
        raw_catalog = json.dumps(
            [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                }
                for spec in specs
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertLess(len(compact), len(raw_catalog))
        self.assertIn('{"action":"click","coordinate":[x,y]}', compact)
        self.assertIn('"start_coordinate":[x,y]', compact)
        self.assertIn('{"action":"type","text":"要输入的文本"}', compact)
        self.assertIn("不自动点击输入框、不清空原内容、不提交", compact)
        self.assertIn("# Action Space", compact)
        self.assertIn(
            "你是一个单步 GUI Agent，根据当前截图和用户指令选择一个动作。",
            compact,
        )
        self.assertNotIn("严格的单步", compact)
        self.assertNotIn("没有历史", compact)
        self.assertIn("## 基础 GUI 动作", compact)
        self.assertIn("## 播放器专用动作", compact)
        self.assertIn("## 页面专用动作", compact)
        self.assertNotIn("爱奇艺专用", compact)
        self.assertNotIn("爱奇艺视频", compact)
        self.assertIn("## 拒绝动作", compact)
        self.assertIn(
            '{"action_id":"reject","reason_type":"拒绝类型"}:',
            compact,
        )
        self.assertNotIn("message:string", compact)
        self.assertNotIn("# Reject reason_type", compact)
        reject_definition = compact[compact.index("## 拒绝动作") :]
        self.assertIn("使用 TARGET_NOT_VISIBLE", reject_definition)
        self.assertIn("使用 UNSUPPORTED_TARGET", reject_definition)
        self.assertIn("用户提出的目标不受当前场景支持", reject_definition)
        self.assertIn("坐标以当前输入图为准", compact)
        self.assertNotIn("1000×1000", compact)
        self.assertNotIn("0 到 1000", compact)
        self.assertNotIn("x:number", compact)
        self.assertNotIn("x1?:number", compact)
        self.assertIn('{"action":"player_pause"}', compact)
        for spec in specs:
            field = "action_id" if spec.name == "reject" else "action"
            self.assertIn(f'"{field}":"{spec.name}"', compact)


class XmlHierarchyTests(unittest.TestCase):
    def test_screen_center_uses_full_hierarchy_extent(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node bounds="[0,60][1920,1140]" />
              <node bounds="[20,0][180,120]" />
              <node bounds="[1800,1080][1920,1200]" />
            </hierarchy>
            """
        )
        self.assertEqual(find_screen_center(root), (960, 600))

    def test_quality_control_uses_visible_smart_label_and_pad_resource_id(self):
        root = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/tv_play_rate_layout"
                    clickable="true" enabled="true" visible-to-user="true"
                    bounds="[2121,1435][2256,1600]">
                <node text="智能"
                      resource-id="com.qiyi.video.pad:id/tv_play_rate"
                      clickable="false" enabled="true" visible-to-user="true"
                      bounds="[2151,1476][2225,1525]" />
              </node>
            </hierarchy>
            """
        )

        control = find_quality_control(root)

        self.assertIsNotNone(control)
        self.assertEqual(center(control), (2188, 1517))

    def test_quality_control_supports_set_top_box_current_value_entry(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/play_rate"
                    class="android.widget.FrameLayout" clickable="false"
                    enabled="true" visible-to-user="true"
                    bounds="[1434,1051][1658,1121]">
                <node text="标清480P"
                      resource-id="com.qiyi.video.speaker:id/tv_play_rate"
                      class="android.widget.TextView" clickable="true"
                      enabled="true" visible-to-user="true"
                      bounds="[1434,1051][1658,1121]" />
              </node>
            </hierarchy>
            """
        )

        control = find_quality_control(root)
        self.assertIsNotNone(control)
        self.assertEqual(center(control), (1546, 1086))
        self.assertEqual(find_current_quality(root), "480p")

    def test_quality_options_include_smart_and_walk_to_clickable_cards(self):
        menu = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/tv_play_rate_layout"
                    clickable="true" enabled="true" visible-to-user="true"
                    bounds="[2121,1435][2256,1600]">
                <node text="智能"
                      resource-id="com.qiyi.video.pad:id/tv_play_rate"
                      clickable="false" enabled="true" visible-to-user="true"
                      bounds="[2151,1476][2225,1525]" />
              </node>
              <node resource-id="com.qiyi.video.pad:id/quality_panel"
                    clickable="false" enabled="true" visible-to-user="true"
                    bounds="[1500,300][2400,800]">
                <node clickable="true" enabled="true" visible-to-user="true"
                      bounds="[1500,350][1800,500]">
                  <node text="智能" clickable="false" enabled="true"
                        visible-to-user="true" bounds="[1600,400][1700,450]" />
                </node>
                <node clickable="true" enabled="true" visible-to-user="true"
                      bounds="[1800,350][2100,500]">
                  <node text="720P" clickable="false" enabled="true"
                        visible-to-user="true" bounds="[1900,400][2000,450]" />
                </node>
              </node>
            </hierarchy>
            """
        )

        smart = find_quality_option(menu, "auto")
        hd = find_quality_option(menu, "720p")

        self.assertIsNotNone(smart)
        self.assertEqual(center(smart), (1650, 425))
        self.assertIsNotNone(hd)
        self.assertEqual(center(hd), (1950, 425))

    def test_quality_current_collapses_smart_card_with_effective_resolution(self):
        panel = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node clickable="true" selected="true" enabled="true"
                    visible-to-user="true" bounds="[1790,479][2145,634]">
                <node text="智能" selected="true" enabled="true"
                      visible-to-user="true" bounds="[1815,503][1891,554]" />
                <node text="1080P" selected="true" enabled="true"
                      visible-to-user="true" bounds="[1815,563][1939,634]" />
              </node>
            </hierarchy>
            """
        )

        self.assertEqual(find_current_quality(panel), "auto")

    def test_quality_control_exists_only_in_fullscreen_sample(self):
        sample_dir = Path(__file__).parent.parent
        fullscreen = load_hierarchy(sample_dir / "1.xml")
        smallscreen = load_hierarchy(sample_dir / "2.xml")

        control = find_quality_control(fullscreen)
        self.assertIsNotNone(control)
        self.assertEqual(center(control), (2242, 1101))
        self.assertIsNone(find_quality_control(smallscreen))

    def test_quality_options_resolve_to_clickable_cards(self):
        menu = load_hierarchy(Path(__file__).parent.parent / "3.xml")
        expected = {
            "1080p": (2074, 438),
            "720p": (1768, 642),
            "480p": (2074, 642),
        }
        for quality, expected_center in expected.items():
            with self.subTest(quality=quality):
                option = find_quality_option(menu, quality)
                self.assertIsNotNone(option)
                self.assertEqual(option.get("clickable"), "true")
                self.assertEqual(center(option), expected_center)

    def test_progress_start_coordinates_support_large_and_small_players(self):
        project_root = Path(__file__).parent.parent
        cases = {
            project_root / "1.xml": (300, 1011),
            project_root / "2.xml": (221, 722),
        }
        optional_sample = (
            project_root / "tests" / "fixtures" / "iqiyi" / "14.xml"
        )
        if optional_sample.is_file():
            cases[optional_sample] = (269, 722)
        for path, expected in cases.items():
            with self.subTest(path=path.name):
                root = load_hierarchy(path)
                progress_bar = find_progress_bar(root)
                self.assertIsNotNone(progress_bar)
                self.assertEqual(
                    progress_start_coordinates(progress_bar), expected
                )

    def test_progress_bar_supports_set_top_box_package_prefix(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/play_progress"
                    class="android.widget.SeekBar" enabled="true"
                    visible-to-user="true" bounds="[174,868][1746,1026]" />
            </hierarchy>
            """
        )
        progress_bar = find_progress_bar(root)
        self.assertIsNotNone(progress_bar)
        self.assertEqual(progress_start_coordinates(progress_bar), (175, 947))

    def test_progress_bar_requires_seekbar_class(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/play_progress"
                    class="android.view.View" enabled="true"
                    visible-to-user="true" bounds="[174,868][1746,1026]" />
            </hierarchy>
            """
        )
        self.assertIsNone(find_progress_bar(root))

    def test_progress_bar_is_absent_from_paused_ad_overlay(self):
        path = (
            Path(__file__).parent
            / "fixtures"
            / "iqiyi"
            / "15.xml"
        )
        if not path.is_file():
            self.skipTest("Optional historical iQIYI fixture 15.xml is absent.")
        self.assertIsNone(find_progress_bar(load_hierarchy(path)))

    def test_episode_controls_exist_only_in_fullscreen_sample(self):
        project_root = Path(__file__).parent.parent
        fullscreen = load_hierarchy(project_root / "1.xml")
        smallscreen = load_hierarchy(project_root / "2.xml")

        next_control = find_next_episode_control(fullscreen)
        episode_menu = find_episode_menu_control(fullscreen)
        self.assertIsNotNone(next_control)
        self.assertIsNotNone(episode_menu)
        self.assertEqual(center(next_control), (433, 1111))
        self.assertEqual(center(episode_menu), (2422, 1122))
        self.assertIsNone(find_next_episode_control(smallscreen))
        self.assertIsNone(find_episode_menu_control(smallscreen))

    def test_episode_panel_resolves_current_and_previous_clickable_card(self):
        panel = load_hierarchy(Path(__file__).parent.parent / "4.xml")

        self.assertEqual(find_current_episode(panel), 3)
        previous_card = find_episode_card(panel, 2)
        self.assertIsNotNone(previous_card)
        self.assertEqual(previous_card.get("clickable"), "true")
        self.assertEqual(center(previous_card), (2074, 729))
        self.assertIsNone(find_episode_card(panel, 0))

    def test_episode_panel_supports_tablet_ids_and_playing_marker(self):
        panel = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/blockLayout"
                    clickable="false" enabled="true" visible-to-user="true"
                    bounds="[100,100][500,300]">
                <node resource-id="com.qiyi.video.pad:id/unused_res_a"
                      clickable="true" enabled="true" visible-to-user="true"
                      bounds="[100,100][500,200]">
                  <node text="2 第2集" clickable="false" enabled="true"
                        visible-to-user="true" bounds="[110,100][490,200]" />
                </node>
              </node>
              <node resource-id="com.qiyi.video.pad:id/blockLayout"
                    clickable="false" enabled="true" visible-to-user="true"
                    bounds="[100,200][500,300]">
                <node resource-id="com.qiyi.video.pad:id/unused_res_a"
                      clickable="true" enabled="true" visible-to-user="true"
                      bounds="[100,200][500,300]">
                  <node text="3 第3集" clickable="false" enabled="true"
                        visible-to-user="true" bounds="[110,200][490,300]" />
                  <node resource-id="com.qiyi.video.pad:id/playing"
                        clickable="false" enabled="true" visible-to-user="true"
                        bounds="[450,240][480,270]" />
                </node>
              </node>
            </hierarchy>
            """
        )

        self.assertEqual(find_current_episode(panel), 3)
        previous_card = find_episode_card(panel, 2)
        self.assertIsNotNone(previous_card)
        self.assertEqual(center(previous_card), (300, 150))

    def test_episode_panel_infers_number_replaced_by_speaker_playing_icon(self):
        panel = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/episodeGridView"
                    bounds="[100,100][700,300]">
                <node clickable="true" enabled="true" visible-to-user="true"
                      bounds="[100,100][300,300]">
                  <node resource-id="com.qiyi.video.speaker:id/episode_item_root"
                        bounds="[100,100][300,299]">
                    <node text="6"
                          resource-id="com.qiyi.video.speaker:id/episode_item"
                          visible-to-user="true" bounds="[180,160][220,210]" />
                  </node>
                </node>
                <node clickable="true" enabled="true" visible-to-user="true"
                      bounds="[300,100][500,300]">
                  <node resource-id="com.qiyi.video.speaker:id/episode_item_root"
                        bounds="[300,100][500,299]">
                    <node resource-id="com.qiyi.video.speaker:id/episode_item_playing"
                          visible-to-user="true" bounds="[350,140][450,240]" />
                  </node>
                </node>
                <node clickable="true" enabled="true" visible-to-user="true"
                      bounds="[500,100][700,300]">
                  <node resource-id="com.qiyi.video.speaker:id/episode_item_root"
                        bounds="[500,100][700,299]">
                    <node text="8"
                          resource-id="com.qiyi.video.speaker:id/episode_item"
                          visible-to-user="true" bounds="[580,160][620,210]" />
                  </node>
                </node>
              </node>
            </hierarchy>
            """
        )

        self.assertEqual(find_current_episode(panel), 7)
        previous_card = find_episode_card(panel, 6)
        self.assertIsNotNone(previous_card)
        self.assertEqual(center(previous_card), (200, 200))

    def test_search_controls_resolve_pad_semantics_and_resource_suffixes(self):
        root = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/layout_search"
                    clickable="false" enabled="true" visible-to-user="true"
                    bounds="[235,108][1418,188]">
                <node clickable="true" enabled="true" visible-to-user="true"
                      content-desc="搜索框 太古神尊"
                      bounds="[265,108][1313,188]" />
                <node resource-id="com.qiyi.video.pad:id/right_search_icon"
                      content-desc="搜索" clickable="true" enabled="true"
                      visible-to-user="true" bounds="[1313,108][1418,188]" />
              </node>
            </hierarchy>
            """
        )

        entry = find_search_entry(root)
        button = find_search_button(root)

        self.assertIsNotNone(entry)
        self.assertEqual(center(entry), (789, 148))
        self.assertIsNotNone(button)
        self.assertEqual(center(button), (1365, 148))

    def test_search_entry_supports_set_top_box_home_search(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/search_layout"
                    clickable="false" enabled="true" visible-to-user="true"
                    bounds="[1573,20][1761,100]">
                <node text="搜索"
                      resource-id="com.qiyi.video.speaker:id/id_home_search"
                      class="android.widget.TextView" clickable="true"
                      enabled="true" visible-to-user="true"
                      bounds="[1573,20][1761,100]" />
              </node>
            </hierarchy>
            """
        )

        entry = find_search_entry(root)
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.get("resource-id"),
            "com.qiyi.video.speaker:id/id_home_search",
        )
        self.assertEqual(center(entry), (1667, 60))

    def test_search_loading_overlay_is_detected(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node text="加载中" visible-to-user="true"
                    resource-id="com.qiyi.video.speaker:id/phone_custom_toast_text"
                    bounds="[849,648][1069,691]" />
            </hierarchy>
            """
        )
        self.assertTrue(is_search_page_loading(root))

    def test_speed_control_exists_only_in_fullscreen_sample(self):
        project_root = Path(__file__).parent.parent
        fullscreen = load_hierarchy(project_root / "1.xml")
        smallscreen = load_hierarchy(project_root / "2.xml")

        speed_control = find_speed_control(fullscreen)
        self.assertIsNotNone(speed_control)
        self.assertEqual(center(speed_control), (2063, 1122))
        self.assertIsNone(find_speed_control(smallscreen))

    def test_speed_control_finds_pad_text_and_clickable_bounds(self):
        root = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/btn_back"
                    content-desc="返回" clickable="true" enabled="true"
                    visible-to-user="true" bounds="[23,75][96,148]" />
              <node resource-id="com.qiyi.video.pad:id/unused_res_a"
                    clickable="true" enabled="true" visible-to-user="true"
                    bounds="[1900,1400][2150,1600]">
                <node text="倍速" clickable="false" enabled="true"
                      visible-to-user="true" bounds="[1948,1471][2088,1600]" />
              </node>
            </hierarchy>
            """
        )

        control = find_speed_control(root)

        self.assertIsNotNone(control)
        self.assertEqual(center(control), (2025, 1500))

    def test_match_mode_does_not_use_fixed_speed_resource_name(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node resource-id="com.qiyi.video.speaker:id/tv_change_speed_play"
                    clickable="true" enabled="true" visible-to-user="true"
                    bounds="[100,100][300,200]" />
            </hierarchy>
            """
        )
        with patch.dict(
            os.environ,
            {MODE_ENVIRONMENT_VARIABLE: "medium"},
        ):
            self.assertIsNotNone(find_speed_control(root))
        with patch.dict(
            os.environ,
            {MODE_ENVIRONMENT_VARIABLE: "match"},
        ):
            self.assertIsNone(find_speed_control(root))

    def test_speed_panel_resolves_current_and_all_options(self):
        panel = load_hierarchy(Path(__file__).parent.parent / "5.xml")
        expected = {
            "3.0x": (2074, 411),
            "2.0x": (2074, 579),
            "1.5x": (2074, 747),
            "1.25x": (2074, 915),
            "1.0x": (2074, 1083),
            "0.75x": (2074, 1191),
        }

        self.assertEqual(find_current_speed(panel), "1.0x")
        for speed, expected_center in expected.items():
            with self.subTest(speed=speed):
                option = find_speed_option(panel, speed)
                self.assertIsNotNone(option)
                self.assertEqual(option.get("clickable"), "true")
                self.assertEqual(center(option), expected_center)

    def test_speed_entries_use_text_and_clickable_parent_without_panel_id(self):
        panel = ElementTree.fromstring(
            """
            <hierarchy rotation="1">
              <node resource-id="com.qiyi.video.pad:id/unused_res_a"
                    clickable="true" enabled="true" visible-to-user="true"
                    selected="false" bounds="[100,100][400,200]">
                <node text="2.0X" content-desc="2.0倍速" clickable="false"
                      enabled="true" visible-to-user="true"
                      bounds="[100,100][400,200]" />
              </node>
              <node resource-id="com.qiyi.video.pad:id/unused_res_a"
                    clickable="true" enabled="true" visible-to-user="true"
                    selected="true" bounds="[100,220][400,320]">
                <node text="1.0X" content-desc="1.0倍速" clickable="false"
                      enabled="true" visible-to-user="true"
                      selected="true" bounds="[100,220][400,320]" />
              </node>
            </hierarchy>
            """
        )

        self.assertEqual(find_current_speed(panel), "1.0x")
        option = find_speed_option(panel, "2.0x")
        self.assertIsNotNone(option)
        self.assertEqual(center(option), (250, 150))

    def test_search_nodes_resolve_across_entry_and_focused_samples(self):
        project_root = Path(__file__).parent.parent
        entry_page = load_hierarchy(project_root / "6.xml")
        focused_page = load_hierarchy(project_root / "7.xml")

        entry = find_search_entry(entry_page)
        self.assertIsNotNone(entry)
        self.assertEqual(center(entry), (597, 179))
        self.assertIsNone(find_focused_edit_text(entry_page))

        edit_text = find_focused_edit_text(focused_page)
        button = find_search_button(focused_page)
        self.assertIsNotNone(edit_text)
        self.assertIsNotNone(button)
        self.assertEqual(edit_text.get("text"), "逃亡兔")
        self.assertEqual(center(button), (1033, 317))
        self.assertEqual(
            find_active_search_controls(focused_page), (edit_text, button)
        )

    def test_player_center_uses_largest_visible_candidate(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "iqiyi"
        if not fixture_dir.is_dir():
            self.skipTest("Optional historical iQIYI fixtures are absent.")
        expected = {
            "12.xml": (599, 453),
            "13.xml": (1334, 600),
            "15.xml": (599, 453),
            "16.xml": (1334, 600),
        }
        for filename, center in expected.items():
            with self.subTest(filename=filename):
                root = load_hierarchy(fixture_dir / filename)
                self.assertEqual(find_player_center(root), center)

    def test_xml_writer_saves_successive_snapshots(self):
        xml = '<hierarchy rotation="0"><node bounds="[0,0][10,10]" /></hierarchy>'
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            writer = XmlArchiveWriter(
                output_dir=output_dir,
                case_id="CASE-1",
                start_index=0,
            )
            first = writer.save(xml)
            second = writer.save(xml)

            self.assertEqual(first, output_dir / "CASE-1_0.xml")
            self.assertEqual(second, output_dir / "CASE-1_1.xml")
            self.assertEqual(first.read_text(encoding="utf-8"), xml)
            self.assertFalse((output_dir / "CASE-1_0.xml.tmp").exists())

    def test_device_dump_is_archived_with_next_sequence_number(self):
        xml = '<hierarchy rotation="0"><node bounds="[0,0][10,10]" /></hierarchy>'

        class FakeDevice:
            def dump_hierarchy(self, **arguments):
                self.arguments = arguments
                return xml

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            writer = XmlArchiveWriter(
                output_dir=output_dir,
                case_id="CASE-2",
                start_index=1,
            )
            device = FakeDevice()
            hierarchy = dump_device_hierarchy(device, writer=writer)

            self.assertEqual(hierarchy.path, output_dir / "CASE-2_1.xml")
            self.assertTrue(hierarchy.path.is_file())
            self.assertFalse(device.arguments["root_in_active"])

    def test_xml_artifact_path_uses_case_and_zero_index(self):
        directory = Path("screenshots") / "IQY010"
        self.assertEqual(
            xml_artifact_path(directory, "IQY010", 0),
            directory / "IQY010_0.xml",
        )


class PauseAtomicToolTests(unittest.TestCase):
    def test_action_mode_defaults_to_medium_and_validates_values(self):
        self.assertEqual(normalize_action_mode(None), "medium")
        self.assertEqual(normalize_action_mode(" MATCH "), "match")
        with self.assertRaises(ValueError):
            normalize_action_mode("automatic")

    def test_pause_taps_the_screen_center_twice_without_an_extra_dump(self):
        root = ElementTree.fromstring(
            '<hierarchy><node bounds="[0,0][1920,1200]" /></hierarchy>'
        )
        with patch.object(
            pause_iqiyi,
            "parse_arguments",
            return_value=SimpleNamespace(serial="device-1", initial_xml=Path("0.xml")),
        ), patch.object(
            pause_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            pause_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            pause_iqiyi, "load_hierarchy", return_value=root
        ), patch.object(
            pause_iqiyi, "dump_device_hierarchy"
        ) as dump, patch.object(
            pause_iqiyi, "adb_tap", side_effect=[0.10, 0.12]
        ) as tap, patch.object(
            pause_iqiyi, "emit_atomic_timing"
        ) as emit:
            return_code = pause_iqiyi.main()

        self.assertEqual(return_code, 0)
        self.assertEqual(
            tap.call_args_list,
            [call("device-1", 960, 600), call("device-1", 960, 600)],
        )
        dump.assert_not_called()
        details = emit.call_args.kwargs["adb_details"]
        self.assertEqual(
            [entry["operation"] for entry in details],
            ["show_player_controls", "pause"],
        )

    def test_match_mode_also_taps_screen_center_twice(self):
        root = ElementTree.fromstring(
            '<hierarchy><node bounds="[0,0][2560,1600]" /></hierarchy>'
        )
        arguments = SimpleNamespace(
            serial="device-1",
            initial_xml=Path("0.xml"),
        )
        with patch.dict(
            os.environ,
            {MODE_ENVIRONMENT_VARIABLE: "match"},
        ), patch.object(
            pause_iqiyi, "parse_arguments", return_value=arguments
        ), patch.object(
            pause_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            pause_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            pause_iqiyi, "load_hierarchy", return_value=root
        ), patch.object(
            pause_iqiyi, "adb_tap", side_effect=[0.04, 0.05]
        ) as tap, patch.object(
            pause_iqiyi, "emit_atomic_timing"
        ):
            return_code = pause_iqiyi.main()

        self.assertEqual(return_code, 0)
        self.assertEqual(
            tap.call_args_list,
            [
                call("device-1", 1280, 800),
                call("device-1", 1280, 800),
            ],
        )


class IqiyiLocatorModeTests(unittest.TestCase):
    @staticmethod
    def _resource_only_node(resource_id: str, class_name: str = "android.view.View"):
        return ElementTree.fromstring(
            f"""
            <hierarchy>
              <node resource-id="{resource_id}" class="{class_name}"
                    clickable="true" enabled="true" visible-to-user="true"
                    bounds="[100,100][300,200]" />
            </hierarchy>
            """
        )

    def test_match_mode_disables_fixed_quality_control(self):
        root = self._resource_only_node(
            "com.qiyi.video.speaker:id/tv_play_rate_layout"
        )
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "medium"}):
            self.assertIsNotNone(find_quality_control(root))
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "match"}):
            self.assertIsNone(find_quality_control(root))

    def test_match_mode_disables_fixed_episode_control(self):
        root = self._resource_only_node(
            "com.qiyi.video.speaker:id/im_play_next"
        )
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "medium"}):
            self.assertIsNotNone(find_next_episode_control(root))
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "match"}):
            self.assertIsNone(find_next_episode_control(root))

    def test_match_mode_disables_fixed_search_entry(self):
        root = self._resource_only_node(
            "com.qiyi.video.speaker:id/id_home_search"
        )
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "medium"}):
            self.assertIsNotNone(find_search_entry(root))
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "match"}):
            self.assertIsNone(find_search_entry(root))

    def test_match_mode_accepts_seekbar_without_fixed_resource_name(self):
        root = self._resource_only_node(
            "other.player:id/progress",
            class_name="android.widget.SeekBar",
        )
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "medium"}):
            self.assertIsNone(find_progress_bar(root))
        with patch.dict(os.environ, {MODE_ENVIRONMENT_VARIABLE: "match"}):
            self.assertIsNotNone(find_progress_bar(root))


class EpisodeAtomicToolTests(unittest.TestCase):
    @staticmethod
    def _arguments(direction: str) -> Namespace:
        return Namespace(
            serial="device-1",
            direction=direction,
            attempts=3,
            retry_delay=0.05,
            initial_xml=Path("CASE-EP_0.xml"),
        )

    def test_previous_episode_uses_an_already_open_panel(self):
        panel = load_hierarchy(Path(__file__).parent.parent / "4.xml")
        with patch.object(
            change_episode_iqiyi,
            "parse_arguments",
            return_value=self._arguments("previous"),
        ), patch.object(
            change_episode_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            change_episode_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            change_episode_iqiyi, "load_hierarchy", return_value=panel
        ), patch.object(
            change_episode_iqiyi, "adb_tap", return_value=0.04
        ) as tap, patch.object(
            change_episode_iqiyi, "emit_atomic_result"
        ) as result_protocol, patch.object(
            change_episode_iqiyi, "emit_atomic_timing"
        ):
            return_code = change_episode_iqiyi.main()

        self.assertEqual(return_code, 0)
        tap.assert_called_once_with("device-1", 2074, 729)
        self.assertEqual(result_protocol.call_args.kwargs["status"], "executed")

    def test_next_episode_uses_an_already_visible_control(self):
        controls = load_hierarchy(Path(__file__).parent.parent / "1.xml")
        with patch.object(
            change_episode_iqiyi,
            "parse_arguments",
            return_value=self._arguments("next"),
        ), patch.object(
            change_episode_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            change_episode_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            change_episode_iqiyi, "load_hierarchy", return_value=controls
        ), patch.object(
            change_episode_iqiyi, "adb_tap", return_value=0.04
        ) as tap, patch.object(
            change_episode_iqiyi, "emit_atomic_result"
        ) as result_protocol, patch.object(
            change_episode_iqiyi, "emit_atomic_timing"
        ):
            return_code = change_episode_iqiyi.main()

        self.assertEqual(return_code, 0)
        tap.assert_called_once_with("device-1", 433, 1111)
        self.assertEqual(result_protocol.call_args.kwargs["status"], "executed")

    def test_previous_episode_rejects_when_current_episode_is_one(self):
        panel = load_hierarchy(Path(__file__).parent.parent / "4.xml")
        for node in panel.iter("node"):
            if node.get("selected") == "true":
                node.set("selected", "false")
            if node.get("text", "").startswith("1 "):
                node.set("selected", "true")

        with patch.object(
            change_episode_iqiyi,
            "parse_arguments",
            return_value=self._arguments("previous"),
        ), patch.object(
            change_episode_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            change_episode_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            change_episode_iqiyi, "load_hierarchy", return_value=panel
        ), patch.object(
            change_episode_iqiyi, "adb_tap"
        ) as tap, patch.object(
            change_episode_iqiyi, "emit_atomic_result"
        ) as result_protocol, patch.object(
            change_episode_iqiyi, "emit_atomic_timing"
        ):
            return_code = change_episode_iqiyi.main()

        self.assertEqual(return_code, 0)
        tap.assert_not_called()
        self.assertEqual(result_protocol.call_args.kwargs["status"], "rejected")
        self.assertEqual(
            result_protocol.call_args.kwargs["rejection"]["stage"],
            "previous_episode",
        )


class PlaybackSpeedAtomicToolTests(unittest.TestCase):
    @staticmethod
    def _arguments(speed: str) -> Namespace:
        return Namespace(
            serial="device-1",
            speed=speed,
            attempts=3,
            retry_delay=0.05,
            initial_xml=Path("CASE-SPEED_0.xml"),
        )

    def _run_with_open_panel(self, speed: str):
        panel = load_hierarchy(Path(__file__).parent.parent / "5.xml")
        patches = (
            patch.object(
                set_playback_speed_iqiyi,
                "parse_arguments",
                return_value=self._arguments(speed),
            ),
            patch.object(
                set_playback_speed_iqiyi,
                "select_device",
                return_value="device-1",
            ),
            patch.object(
                set_playback_speed_iqiyi,
                "writer_from_arguments",
                return_value=None,
            ),
            patch.object(
                set_playback_speed_iqiyi,
                "load_hierarchy",
                return_value=panel,
            ),
            patch.object(
                set_playback_speed_iqiyi, "adb_tap", return_value=0.04
            ),
            patch.object(set_playback_speed_iqiyi, "emit_atomic_result"),
            patch.object(set_playback_speed_iqiyi, "emit_atomic_timing"),
        )
        started = [item.start() for item in patches]
        try:
            return_code = set_playback_speed_iqiyi.main()
            tap = started[4]
            result_protocol = started[5]
            return return_code, tap, result_protocol
        finally:
            for item in reversed(patches):
                item.stop()

    def test_changes_speed_using_an_already_open_panel(self):
        return_code, tap, result_protocol = self._run_with_open_panel("1.5x")

        self.assertEqual(return_code, 0)
        tap.assert_called_once_with("device-1", 2074, 747)
        self.assertEqual(result_protocol.call_args.kwargs["status"], "executed")

    def test_same_speed_sends_no_option_click(self):
        return_code, tap, result_protocol = self._run_with_open_panel("1.0x")

        self.assertEqual(return_code, 0)
        tap.assert_not_called()
        self.assertEqual(result_protocol.call_args.kwargs["status"], "executed")
        self.assertIn("already 1.0x", result_protocol.call_args.kwargs["message"])


class SearchAtomicToolTests(unittest.TestCase):
    @staticmethod
    def _arguments() -> Namespace:
        return Namespace(
            serial="device-1",
            query="海绵宝宝",
            page_delay=0.0,
            initial_xml=Path("CASE-SEARCH_0.xml"),
        )

    def test_search_page_uses_one_delayed_dump_by_default(self):
        with patch.object(
            sys,
            "argv",
            ["search.py", "--query", "海绵宝宝"],
        ):
            arguments = search_iqiyi.parse_arguments()
        self.assertEqual(arguments.page_delay, 1.0)
        self.assertFalse(hasattr(arguments, "attempts"))
        self.assertFalse(hasattr(arguments, "retry_delay"))

    def test_unicode_query_uses_fast_input_ime_and_restores_it(self):
        class FakeDevice:
            def __init__(self):
                self.calls = []

            def set_fastinput_ime(self, enabled):
                self.calls.append(("fast_ime", enabled))

            def send_keys(self, text, clear=False):
                self.calls.append(("send_keys", text, clear))

        device = FakeDevice()
        elapsed = send_query_with_fast_input_ime(device, "海绵宝宝")

        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(
            device.calls,
            [
                ("fast_ime", True),
                ("send_keys", "海绵宝宝", True),
                ("fast_ime", False),
            ],
        )

    def test_entry_page_runs_click_type_click_and_archives_intermediate_xml(self):
        project_root = Path(__file__).parent.parent
        entry_page = load_hierarchy(project_root / "6.xml")
        focused_page = load_hierarchy(project_root / "7.xml")

        class FakeDevice:
            def __init__(self):
                self.send_keys_calls = []

            def send_keys(self, text, clear=False):
                self.send_keys_calls.append((text, clear))

        device = FakeDevice()
        hierarchy = SimpleNamespace(
            root=focused_page,
            dump_seconds=0.08,
            path=Path("CASE-SEARCH_1.xml"),
        )
        with patch.object(
            search_iqiyi,
            "parse_arguments",
            return_value=self._arguments(),
        ), patch.object(
            search_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            search_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            search_iqiyi, "load_hierarchy", return_value=entry_page
        ), patch.object(
            search_iqiyi, "connect_uiautomator2", return_value=device
        ), patch.object(
            search_iqiyi, "dump_device_hierarchy", return_value=hierarchy
        ) as dump, patch.object(
            search_iqiyi, "adb_tap", side_effect=(0.02, 0.03)
        ) as tap, patch.object(
            search_iqiyi, "emit_atomic_result"
        ) as result_protocol, patch.object(
            search_iqiyi, "emit_atomic_timing"
        ) as timing_protocol:
            return_code = search_iqiyi.main()

        self.assertEqual(return_code, 0)
        self.assertEqual(
            tap.call_args_list,
            [
                call("device-1", 597, 179),
                call("device-1", 1033, 317),
            ],
        )
        self.assertEqual(device.send_keys_calls, [("海绵宝宝", True)])
        dump.assert_called_once_with(device, writer=None)
        self.assertEqual(result_protocol.call_args.kwargs["status"], "executed")
        operations = [
            entry["operation"]
            for entry in timing_protocol.call_args.kwargs["adb_details"]
        ]
        self.assertEqual(
            operations,
            ["open_search_page", "type_search_query", "submit_search"],
        )

    def test_already_focused_page_skips_open_click_and_dump(self):
        focused_page = load_hierarchy(Path(__file__).parent.parent / "7.xml")

        class FakeDevice:
            def __init__(self):
                self.send_keys_calls = []

            def send_keys(self, text, clear=False):
                self.send_keys_calls.append((text, clear))

        device = FakeDevice()
        with patch.object(
            search_iqiyi,
            "parse_arguments",
            return_value=self._arguments(),
        ), patch.object(
            search_iqiyi, "select_device", return_value="device-1"
        ), patch.object(
            search_iqiyi, "writer_from_arguments", return_value=None
        ), patch.object(
            search_iqiyi, "load_hierarchy", return_value=focused_page
        ), patch.object(
            search_iqiyi, "connect_uiautomator2", return_value=device
        ), patch.object(
            search_iqiyi, "dump_device_hierarchy"
        ) as dump, patch.object(
            search_iqiyi, "adb_tap", return_value=0.03
        ) as tap, patch.object(
            search_iqiyi, "emit_atomic_result"
        ), patch.object(
            search_iqiyi, "emit_atomic_timing"
        ):
            return_code = search_iqiyi.main()

        self.assertEqual(return_code, 0)
        dump.assert_not_called()
        tap.assert_called_once_with("device-1", 1033, 317)
        self.assertEqual(device.send_keys_calls, [("海绵宝宝", True)])


class ArtifactTests(unittest.TestCase):
    @staticmethod
    def _png(
        width: int, height: int, color: tuple[int, int, int] = (30, 60, 90)
    ) -> bytes:
        output = BytesIO()
        Image.new("RGB", (width, height), color).save(
            output, format="PNG"
        )
        return output.getvalue()

    def test_saves_original_and_done_images_with_expected_names(self):
        original = self._png(400, 300)
        snapshot = ScreenSnapshot(original, 400, 300, "device-1")
        model_content = '{"action":"click","coordinate":[500,500]}'
        response = {
            "id": "completion-1",
            "model": "test-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": model_content,
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = save_run_artifacts(
                project_root=Path(directory),
                case_id="IQY-001",
                instruction="返回上一个界面",
                snapshot=snapshot,
                raw_response=response,
            )

            self.assertEqual(paths.original_image.name, "IQY-001.png")
            self.assertEqual(paths.done_image.name, "IQY-001_done.png")
            self.assertEqual(paths.draw_image.name, "IQY-001_draw.png")
            self.assertEqual(paths.original_image.read_bytes(), original)
            self.assertFalse(paths.done_image.exists())
            self.assertFalse(paths.draw_image.exists())
            self.assertEqual(
                paths.prompt.read_text(encoding="utf-8"),
                f"{build_system_prompt()}\n\n"
                f"{build_user_prompt('返回上一个界面')}",
            )
            self.assertEqual(
                json.loads(paths.response.read_text(encoding="utf-8")),
                {"content": model_content},
            )
            save_execution_result(
                paths=paths,
                selection=ActionSelection(
                    "swipe",
                    {
                        "x1": 500,
                        "y1": 700,
                        "x2": 500,
                        "y2": 300,
                    },
                ),
                status="executed",
                message="done",
                timings_seconds={
                    "vla_call": 1.2345678,
                    "dump_xml": 0.305,
                    "adb_execution": 0.1525,
                    "total": 2.0,
                },
                timing_details={
                    "dump_xml": [
                        {
                            "sequence": 0,
                            "file": "IQY-001_0.xml",
                            "seconds": 0.3045678,
                        }
                    ],
                    "adb_execution": [
                        {
                            "operation": "swipe",
                            "command": "input swipe",
                            "seconds": 0.1524567,
                        }
                    ],
                },
            )
            saved_result = json.loads(paths.result.read_text(encoding="utf-8"))
            self.assertEqual(
                saved_result["action"],
                {
                    "action": "swipe",
                    "start_coordinate": [500, 700],
                    "direction": "up",
                    "distance": "medium",
                },
            )
            self.assertEqual(
                saved_result["timing_seconds"]["vla_call"], 1.234568
            )
            self.assertEqual(
                saved_result["timing_details"]["dump_xml"][0],
                {
                    "sequence": 0,
                    "file": "IQY-001_0.xml",
                    "seconds": 0.304568,
                },
            )
            save_draw_screenshot(
                paths=paths,
                snapshot=snapshot,
                selection=ActionSelection("click", {"x": 500, "y": 500}),
            )
            with Image.open(paths.draw_image) as drawn:
                self.assertEqual(drawn.size, (400, 300))
                self.assertNotEqual(drawn.getpixel((200, 150)), (30, 60, 90))
                self.assertEqual(drawn.getpixel((10, 10)), (30, 60, 90))

            done_png = self._png(600, 300, (90, 20, 10))
            save_done_screenshot(
                paths=paths,
                snapshot=ScreenSnapshot(done_png, 600, 300, "device-1"),
            )
            self.assertEqual(paths.done_image.read_bytes(), done_png)
            with Image.open(paths.done_image) as done:
                self.assertEqual(done.size, (600, 300))
                self.assertEqual(done.getpixel((300, 150)), (90, 20, 10))

    def test_rejects_case_id_path_traversal(self):
        with self.assertRaises(ValueError):
            validate_case_id("../outside")

    def test_saves_atomic_tool_rejection_in_result(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_run_artifacts(
                project_root=Path(directory), case_id="IQY-REJECT"
            )
            rejection = {
                "source": "atomic_tool",
                "reason_type": "TARGET_NOT_VISIBLE",
                "stage": "quality_control",
                "requested_quality": "720p",
                "message": "当前播放界面没有清晰度控件。",
            }
            save_execution_result(
                paths=paths,
                selection=ActionSelection(
                    "player_set_quality", {"quality": "720p"}
                ),
                status="rejected",
                message=rejection["message"],
                timings_seconds={},
                rejection=rejection,
            )

            saved_result = json.loads(paths.result.read_text(encoding="utf-8"))
            self.assertEqual(saved_result["status"], "rejected")
            self.assertEqual(saved_result["rejection"], rejection)

    def test_reused_case_removes_old_numbered_xml_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            paths = prepare_run_artifacts(
                project_root=project_root, case_id="CASE-XML"
            )
            stale_xml = paths.directory / "CASE-XML_0.xml"
            unrelated_xml = paths.directory / "manual.xml"
            stale_xml.write_text("<old />", encoding="utf-8")
            unrelated_xml.write_text("<keep />", encoding="utf-8")

            prepare_run_artifacts(
                project_root=project_root, case_id="CASE-XML"
            )

            self.assertFalse(stale_xml.exists())
            self.assertTrue(unrelated_xml.exists())

    def test_prompt_archive_contains_the_actual_action_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_run_artifacts(
                project_root=Path(directory), case_id="CASE-PROMPT"
            )
            save_prompt(
                paths=paths,
                instruction="搜索海绵宝宝",
                app_package="com.qiyi.video.pad",
            )

            expected = (
                build_system_prompt("com.qiyi.video.pad")
                + "\n\n"
                + build_user_prompt("搜索海绵宝宝")
            )
            saved = paths.prompt.read_text(encoding="utf-8")
            self.assertEqual(saved, expected)
            self.assertIn('"action":"player_search"', saved)
            self.assertIn('"action":"动作名称"', saved)
            self.assertNotIn('"additionalProperties"', saved)

            model_content = (
                '{"action":"player_search","query":"海绵宝宝"}'
            )
            save_model_response(
                paths=paths,
                raw_response={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": model_content,
                            }
                        }
                    ]
                },
            )
            self.assertEqual(
                json.loads(paths.response.read_text(encoding="utf-8")),
                {"content": model_content},
            )

    def test_artifacts_can_be_saved_incrementally(self):
        model_content = '{"action":"click","coordinate":[500,500]}'
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": model_content,
                    }
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_run_artifacts(
                project_root=Path(directory), case_id="CASE-1"
            )
            self.assertTrue(paths.directory.is_dir())
            self.assertFalse(paths.original_image.exists())

            save_prompt(paths=paths, instruction="点击中心")
            self.assertTrue(paths.prompt.is_file())
            self.assertFalse(paths.original_image.exists())

            snapshot = ScreenSnapshot(self._png(400, 300), 400, 300, "device-1")
            save_original_screenshot(paths=paths, snapshot=snapshot)
            self.assertTrue(paths.original_image.is_file())
            self.assertFalse(paths.response.exists())

            save_model_response(paths=paths, raw_response=response)
            self.assertTrue(paths.response.is_file())
            self.assertFalse(paths.response.with_name("response.json.tmp").exists())

            save_execution_result(
                paths=paths,
                selection=None,
                status="calling_vla",
                message="waiting",
                timings_seconds={"total": 0.1},
            )
            progress = json.loads(paths.result.read_text(encoding="utf-8"))
            self.assertEqual(progress["status"], "calling_vla")
            self.assertNotIn("action", progress)


class TimingAndConsoleTests(unittest.TestCase):
    def test_quality_action_maps_requested_enum_to_atomic_tool(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 2670, 1200, "device-1")
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-Q_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-Q",
        )
        with patch.object(
            executor,
            "_run_iqiyi_tool",
            return_value=(
                "selected",
                {"dump_xml": 0.2, "adb_execution": 0.1},
                {"dump_xml": [], "adb_execution": []},
                None,
            ),
        ) as run:
            result = executor.execute(
                ActionSelection(
                    "player_set_quality", {"quality": "720p"}
                ),
                snapshot,
                xml_context,
            )

        self.assertEqual(result.status, "executed")
        run.assert_called_once_with(
            "execution.atomic_tools.iqiyi.set_quality",
            "device-1",
            xml_context,
            ("--quality", "720p"),
        )

    def test_playback_speed_action_maps_requested_enum_to_atomic_tool(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 2670, 1200, "device-1")
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-SPEED_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-SPEED",
        )
        with patch.object(
            executor,
            "_run_iqiyi_tool",
            return_value=(
                "changed",
                {"dump_xml": 0.2, "adb_execution": 0.1},
                {"dump_xml": [], "adb_execution": []},
                None,
            ),
        ) as run:
            result = executor.execute(
                ActionSelection(
                    "player_set_playback_speed", {"speed": "1.5x"}
                ),
                snapshot,
                xml_context,
            )

        self.assertEqual(result.status, "executed")
        run.assert_called_once_with(
            "execution.atomic_tools.iqiyi.set_playback_speed",
            "device-1",
            xml_context,
            ("--speed", "1.5x"),
        )

    def test_search_action_maps_query_to_atomic_tool_without_shell_splitting(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 1200, 2670, "device-1")
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-SEARCH_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-SEARCH",
        )
        with patch.object(
            executor,
            "_run_iqiyi_tool",
            return_value=(
                "searched",
                {"dump_xml": 0.1, "adb_execution": 0.2},
                {"dump_xml": [], "adb_execution": []},
                None,
            ),
        ) as run:
            result = executor.execute(
                ActionSelection("player_search", {"query": "海绵宝宝 12"}),
                snapshot,
                xml_context,
            )

        self.assertEqual(result.status, "executed")
        run.assert_called_once_with(
            "execution.atomic_tools.iqiyi.search",
            "device-1",
            xml_context,
            ("--query=海绵宝宝 12",),
        )

    def test_seek_to_start_action_maps_to_atomic_tool(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 1200, 2670, "device-1")
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-START_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-START",
        )
        with patch.object(
            executor,
            "_run_iqiyi_tool",
            return_value=(
                "moved",
                {"dump_xml": 0.0, "adb_execution": 0.1},
                {"dump_xml": [], "adb_execution": []},
                None,
            ),
        ) as run:
            result = executor.execute(
                ActionSelection("player_seek_to_start", {}),
                snapshot,
                xml_context,
            )

        self.assertEqual(result.status, "executed")
        run.assert_called_once_with(
            "execution.atomic_tools.iqiyi.seek_to_start",
            "device-1",
            xml_context,
            (),
        )

    def test_episode_actions_map_to_shared_module_with_fixed_directions(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        snapshot = ScreenSnapshot(b"", 2670, 1200, "device-1")
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-EP_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-EP",
        )
        cases = {
            "player_previous_episode": "previous",
            "player_next_episode": "next",
        }
        for action, direction in cases.items():
            with self.subTest(action=action), patch.object(
                executor,
                "_run_iqiyi_tool",
                return_value=(
                    "selected",
                    {"dump_xml": 0.1, "adb_execution": 0.1},
                    {"dump_xml": [], "adb_execution": []},
                    None,
                ),
            ) as run:
                result = executor.execute(
                    ActionSelection(action, {}), snapshot, xml_context
                )
                self.assertEqual(result.status, "executed")
                run.assert_called_once_with(
                    "execution.atomic_tools.iqiyi.change_episode",
                    "device-1",
                    xml_context,
                    ("--direction", direction),
                )

    def test_atomic_quality_rejection_becomes_execution_rejection(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        executor = ActionExecutor(FakeAdb(), Path.cwd())
        context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-Q_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-Q",
        )
        rejection = {
            "source": "atomic_tool",
            "reason_type": "TARGET_NOT_VISIBLE",
            "stage": "quality_control",
            "requested_quality": "720p",
            "message": "清晰度入口不可见",
        }
        with patch.object(
            executor,
            "_run_iqiyi_tool",
            return_value=(
                "清晰度入口不可见",
                {"dump_xml": 0.2, "adb_execution": 0.1},
                {"dump_xml": [], "adb_execution": []},
                {
                    "status": "rejected",
                    "message": "清晰度入口不可见",
                    "rejection": rejection,
                },
            ),
        ):
            result = executor.execute(
                ActionSelection(
                    "player_set_quality", {"quality": "720p"}
                ),
                ScreenSnapshot(b"", 1200, 2670, "device-1"),
                context,
            )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection, rejection)

    def test_swipe_executor_does_not_pass_a_duration(self):
        class FakeAdb:
            def __init__(self):
                self.arguments = None

            def swipe(self, **arguments):
                self.arguments = arguments

        adb = FakeAdb()
        executor = ActionExecutor(adb, Path.cwd())
        result = executor.execute(
            ActionSelection(
                "swipe", {"x1": 500, "y1": 700, "x2": 500, "y2": 300}
            ),
            ScreenSnapshot(b"", 1200, 2670, "device-1"),
        )

        self.assertEqual(result.status, "executed")
        self.assertEqual(
            adb.arguments,
            {
                "serial": "device-1",
                "x1": 600,
                "y1": 1868,
                "x2": 600,
                "y2": 801,
            },
        )
        self.assertEqual(
            result.timing_details["adb_execution"][0]["operation"], "swipe"
        )
        self.assertEqual(
            result.timing_details["adb_execution"][0]["x1"], 600
        )

    def test_type_executor_uses_uiautomator2_without_clearing(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        class FakeDevice:
            def __init__(self):
                self.calls = []

            def send_keys(self, text, clear=False):
                self.calls.append((text, clear))

        device = FakeDevice()
        executor = ActionExecutor(FakeAdb(), Path.cwd())
        with patch(
            "execution.executor.connect_uiautomator2",
            return_value=device,
        ) as connect:
            result = executor.execute(
                ActionSelection("type", {"text": "海绵宝宝"}),
                ScreenSnapshot(b"", 1200, 2670, "device-1"),
            )

        connect.assert_called_once_with(FakeAdb.adb_path, "device-1")
        self.assertEqual(device.calls, [("海绵宝宝", False)])
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.timings_seconds["dump_xml"], 0.0)
        detail = result.timing_details["adb_execution"][0]
        self.assertEqual(detail["operation"], "type")
        self.assertEqual(detail["command"], "uiautomator2 send_keys")
        self.assertEqual(detail["characters"], 4)

    def test_atomic_tool_runs_as_module_and_inherits_adb_path(self):
        class FakeAdb:
            adb_path = Path(r"D:\custom-adb\adb.exe")

        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "Paused.\n__GUI_AGENT_TIMING__="
                    '{"dump_xml":0.3,"adb_execution":0.1,"atomic_total":0.8,'
                    '"details":{"dump_xml":[{"sequence":1,"file":"CASE-1_1.xml",'
                    '"seconds":0.3}],"adb_execution":[{"operation":"pause",'
                    '"command":"input tap","x":100,"y":200,"seconds":0.1}]}}\n'
                ),
                "stderr": "",
            },
        )()
        executor = ActionExecutor(FakeAdb(), Path.cwd())
        xml_context = XmlExecutionContext(
            initial_xml=Path("archive") / "CASE-1_0.xml",
            output_dir=Path("archive"),
            case_id="CASE-1",
        )
        with patch(
            "execution.executor.subprocess.run",
            return_value=completed,
        ) as run:
            output, timings, details, outcome = executor._run_iqiyi_tool(
                "execution.atomic_tools.iqiyi.pause",
                "device-1",
                xml_context,
            )

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            command[1:3],
            ["-m", "execution.atomic_tools.iqiyi.pause"],
        )
        self.assertEqual(
            command[command.index("--initial-xml") + 1],
            str(xml_context.initial_xml),
        )
        self.assertEqual(
            command[command.index("--xml-case-id") + 1], "CASE-1"
        )
        self.assertEqual(
            command[command.index("--xml-start-index") + 1], "1"
        )
        self.assertEqual(environment["ADB_PATH"], str(FakeAdb.adb_path))
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")
        self.assertEqual(output, "Paused.")
        self.assertEqual(timings["dump_xml"], 0.3)
        self.assertEqual(details["dump_xml"][0]["file"], "CASE-1_1.xml")
        self.assertEqual(details["adb_execution"][0]["operation"], "pause")
        self.assertIsNone(outcome)

    def test_non_coordinate_action_label_contains_action_json(self):
        label = _action_label(
            ActionSelection("player_pause", {})
        )
        self.assertEqual(label, '{"action":"player_pause"}')
        reject_label = _action_label(
            ActionSelection("reject", {"reason_type": "TARGET_NOT_VISIBLE"})
        )
        self.assertEqual(
            reject_label,
            '{"action_id":"reject","reason_type":"TARGET_NOT_VISIBLE"}',
        )

    def test_atomic_timing_line_is_removed_from_visible_output(self):
        visible, timings, details = extract_atomic_timing(
            'Paused.\n__GUI_AGENT_TIMING__={"dump_xml":30.5,'
            '"adb_execution":12.25,"atomic_total":80.0,'
            '"details":{"dump_xml":[{"sequence":2,"file":"CASE_2.xml",'
            '"seconds":30.5}],"adb_execution":[{"operation":"pause",'
            '"seconds":12.25}]}}\n'
        )
        self.assertEqual(visible, "Paused.")
        self.assertEqual(timings["dump_xml"], 30.5)
        self.assertEqual(timings["adb_execution"], 12.25)
        self.assertEqual(details["dump_xml"][0]["sequence"], 2)
        self.assertEqual(
            details["adb_execution"][0]["operation"], "pause"
        )

    def test_atomic_local_rejection_is_parsed_separately(self):
        visible, outcome = extract_atomic_result(
            'Quality control missing.\n__GUI_AGENT_RESULT__={"status":"rejected",'
            '"message":"清晰度入口不可见",'
            '"rejection":{"source":"atomic_tool",'
            '"reason_type":"TARGET_NOT_VISIBLE",'
            '"stage":"quality_control"}}\n'
        )
        self.assertEqual(visible.strip(), "Quality control missing.")
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(outcome["rejection"]["stage"], "quality_control")


class EnvironmentFileTests(unittest.TestCase):
    def test_loads_key_without_overriding_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.example"
            path.write_text(
                "YUNAI_API_KEY=file-key\nYUNAI_MODEL=file-model\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"YUNAI_MODEL": "process-model"}, clear=True):
                self.assertTrue(load_env_file(path))
                self.assertEqual(os.environ["YUNAI_API_KEY"], "file-key")
                self.assertEqual(os.environ["YUNAI_MODEL"], "process-model")


if __name__ == "__main__":
    unittest.main()
