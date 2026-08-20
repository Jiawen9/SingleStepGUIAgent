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
    resolve_report_image,
    swipe_end,
)


class EvaluationReportToolTests(unittest.TestCase):
    def test_load_report_prefers_pixel_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "result.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "评测明细"
            sheet.append([
                "源行号", "任务指令", "图片ID", "期望结果", "系统标准动作",
                "系统像素动作", "执行主体", "是否正确", "判分说明", "错误",
            ])
            sheet.append([
                7, "点击设置", "CASE.png",
                json.dumps({"action": "click", "bbox": [[10, 20, 30, 40]]}),
                json.dumps({"action": "click", "coordinate": [500, 500]}),
                json.dumps({"action": "click", "coordinate": [20, 30]}),
                "vla", True, "inside", "",
            ])
            workbook.save(report)

            cases = load_report(report)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].source_row, 7)
            self.assertEqual(parse_action(cases[0].model_raw)["coordinate"], [20, 30])
            self.assertTrue(cases[0].correct)

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
        self.assertEqual(action_name({"action_id": "reject"}), "reject")
        self.assertEqual(
            action_summary({"action_id": "reject", "reason_type": "TARGET_NOT_VISIBLE"}),
            '{"action_id":"reject","reason_type":"TARGET_NOT_VISIBLE"}',
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
