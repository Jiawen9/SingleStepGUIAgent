from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from PIL import Image

from evaluation_report_ttk import (
    action_name,
    action_summary,
    engine_actions,
    load_report,
    parse_action,
    parse_optional_bool,
    resolve_report_image,
    swipe_end,
    vla_result_text,
)


class EvaluationReportToolTests(unittest.TestCase):
    def test_load_report_reads_pixel_action_and_vla_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "result.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "评测明细"
            sheet.append([
                "源行号", "任务指令", "图片ID", "期望结果", "系统标准动作",
                "系统像素动作", "是否正确", "VLA正确", "判分说明", "错误",
            ])
            sheet.append([
                7, "点击设置", "CASE.png",
                json.dumps({"action": "click", "bbox": [[10, 20, 30, 40]]}),
                json.dumps({"action": "click", "coordinate": [500, 500]}),
                json.dumps({"action": "click", "coordinate": [20, 30]}),
                True, "FALSE", "VLA 点击点不在期望框内", "",
            ])
            sheet.append([
                8, "返回", "CASE2.png",
                json.dumps({"action": "click", "bbox": [[1, 2, 3, 4]]}),
                json.dumps({"action": "click", "coordinate": [100, 200]}),
                "", False, None, "VLA 未执行", "",
            ])
            workbook.save(report)

            cases = load_report(report)

            self.assertEqual(len(cases), 2)
            self.assertEqual(cases[0].source_row, 7)
            self.assertEqual(parse_action(cases[0].system_pixel_raw)["coordinate"], [20, 30])
            self.assertFalse(cases[0].vla_correct)
            self.assertEqual(cases[0].comparison, "VLA 点击点不在期望框内")
            self.assertEqual(cases[1].system_pixel_raw, "")
            self.assertIsNone(cases[1].vla_correct)

    def test_vla_result_is_parsed_as_three_states(self):
        self.assertTrue(parse_optional_bool(True))
        self.assertTrue(parse_optional_bool("TRUE"))
        self.assertFalse(parse_optional_bool(False))
        self.assertFalse(parse_optional_bool("FALSE"))
        self.assertIsNone(parse_optional_bool(""))
        self.assertEqual(vla_result_text(True), "正确")
        self.assertEqual(vla_result_text(False), "错误")
        self.assertEqual(vla_result_text(None), "未执行")

    def test_resolve_image_uses_report_device_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captures = root / "device_captures"
            captures.mkdir()
            image = captures / "CASE.png"
            Image.new("RGB", (20, 10), "white").save(image)
            self.assertEqual(resolve_report_image("CASE.png", root), image.resolve())

    def test_swipe_end_supports_distance_and_explicit_end(self):
        self.assertEqual(
            swipe_end({"start_coordinate": [50, 50], "direction": "up", "distance": "short"}, 100, 100),
            (50.0, 35.0),
        )
        self.assertEqual(
            swipe_end({"start_coordinate": [10, 20], "end_coordinate": [30, 40]}, 100, 100),
            (30.0, 40.0),
        )

    def test_action_name_accepts_reject(self):
        self.assertEqual(action_name({"action": "reject"}), "reject")
        self.assertEqual(
            action_summary({"action": "reject"}),
            '{"action":"reject"}',
        )

    def test_engine_actions_reads_multi_engine_union(self):
        actions = engine_actions(json.dumps({
            "xml": None,
            "ocr": {"action": "click", "coordinate": [20, 30]},
            "vla": {"action": "swipe", "start_coordinate": [10, 20], "direction": "up"},
        }))
        self.assertEqual([engine for engine, _action in actions], ["ocr", "vla"])


if __name__ == "__main__":
    unittest.main()
