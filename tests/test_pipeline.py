from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

from output.commands import CommandBuilder
from config import AgentConfig
from contracts import (
    ActionSelection,
    EngineContext,
    EngineResult,
    ExecutionInput,
    ScreenSnapshot,
)
from orchestrator import Pipeline
from engines.xml.preprocessor import XmlInstructionPreprocessor
from engines.xml.engine import XmlEngine


class StaticEngine:
    priority = 100

    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0

    def supports(self, context):
        return True

    def run(self, context):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PipelineContractTests(unittest.TestCase):
    def test_engine_result_json_contract(self):
        result = EngineResult(
            "selected",
            "test",
            ActionSelection("click", {"x": 1, "y": 2}),
            {"matched": True},
            {"engine": 0.25},
        )
        self.assertEqual(result.as_dict()["schema_version"], 1)
        self.assertEqual(result.as_dict()["action"]["name"], "click")
        with self.assertRaises(ValueError):
            EngineResult("no_match", "test", ActionSelection("click", {}))

    def test_command_builder_separates_atomic_tool_mapping(self):
        command = CommandBuilder().build(
            ActionSelection("player_set_quality", {"quality": "720p"}),
            ScreenSnapshot(b"", 100, 100, "device"),
        )
        self.assertEqual(command.kind, "atomic_tool")
        self.assertEqual(command.arguments["argv"], ["--quality", "720p"])

    def test_swipe_coordinates_map_from_thousandths_to_original_pixels(self):
        command = CommandBuilder().build(
            ActionSelection(
                "swipe",
                {"x1": 0, "y1": 0, "x2": 1000, "y2": 1000},
            ),
            ScreenSnapshot(b"", 1920, 1080, "device"),
        )
        self.assertEqual(
            command.arguments,
            {"x1": 0, "y1": 0, "x2": 1919, "y2": 1079},
        )
    def test_xml_preprocessing_isolated_from_input(self):
        root = ElementTree.fromstring(
            '<hierarchy><node text="设置" clickable="true" '
            'enabled="true" visible-to-user="true" bounds="[0,0][100,100]"/></hierarchy>'
        )
        run_input = ExecutionInput(
            "CASE",
            "点击设置",
            ScreenSnapshot(b"", 200, 200, "device"),
            None,
            Path("."),
            Path("CASE_0.xml"),
            root,
        )
        context = EngineContext(run_input)
        XmlInstructionPreprocessor().process(context)
        result = XmlEngine().run(context)
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.action.name, "click")

    def test_pipeline_falls_back_and_writes_new_result(self):
        first = StaticEngine("first", RuntimeError("recoverable failure"))
        second = StaticEngine(
            "second",
            EngineResult(
                "selected",
                "second",
                ActionSelection("click", {"x": 500, "y": 500}),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "input.png"
            Image.new("RGB", (40, 30), "white").save(image_path)
            pipeline = Pipeline(
                config=AgentConfig(api_base="http://model.test/v1", model="test-model", adb_path=root / "missing-adb.exe"),
                project_root=root,
                engine_order=("first", "second"),
                engines=(first, second),
            )
            result = pipeline.run(
                case_id="CASE-1",
                instruction="点击中央",
                screenshot_path=image_path,
                dry_run=True,
                done_delay=0,
            )
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "dry_run")
            self.assertEqual([item["source"] for item in payload["engine_results"]], ["first", "second"])
            self.assertEqual(payload["engine_results"][0]["status"], "error")
            self.assertEqual(payload["command"]["kind"], "adb")
            self.assertIsNone(payload["execution"])
            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 1)


if __name__ == "__main__":
    unittest.main()
