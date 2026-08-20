from __future__ import annotations

import json
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

from contracts import EngineContext, ExecutionInput, ScreenSnapshot
from engines.ocr.client import OcrClient, OcrItem, parse_local_ocr_items, parse_ocr_items
from engines.ocr.engine import OcrEngine
from engines.preprocessing import ClickInstructionPreprocessor
from storage.artifacts import annotate_ocr_screenshot


class FakeOcrClient:
    provider = "local"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def recognize(self, png):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeResponse:
    def __init__(self, payload=None, *, text="", status_code=200):
        self.payload = payload
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, *, posts, gets):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self.posts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_context(instruction="点击设置", *, width=1001, height=501):
    context = EngineContext(
        ExecutionInput(
            "CASE",
            instruction,
            ScreenSnapshot(b"png-data", width, height, "device"),
            None,
            Path("."),
            xml_root=None,
        )
    )
    ClickInstructionPreprocessor().process(context)
    return context


class OcrEngineTests(unittest.TestCase):
    def test_ocr_annotation_draws_all_boxes(self):
        source = BytesIO()
        Image.new("RGB", (200, 100), "white").save(source, format="PNG")
        snapshot = ScreenSnapshot(source.getvalue(), 200, 100, None)
        annotated = annotate_ocr_screenshot(
            snapshot,
            [
                {"text": "设置", "score": 0.95, "box": [10, 20, 80, 60]},
                {"text": "搜索", "score": 0.9, "box": [100, 30, 180, 80]},
            ],
        )
        with Image.open(BytesIO(annotated)) as image:
            self.assertEqual(image.size, (200, 100))
            self.assertNotEqual(image.convert("RGB").getpixel((10, 20)), (255, 255, 255))

    def test_preprocessing_does_not_require_xml(self):
        context = make_context()
        self.assertIsNotNone(context.prepared["click_instruction"])

    def test_highest_confidence_exact_match_is_clicked(self):
        client = FakeOcrClient(
            [
                OcrItem("设置", 0.75, (0, 0, 100, 100), 0),
                OcrItem("设置", 0.95, (800, 300, 1000, 500), 1),
                OcrItem("搜索", 0.99, (0, 0, 50, 50), 2),
            ]
        )
        result = OcrEngine(client, min_score=0.5).run(make_context())
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.action.arguments, {"x": 900, "y": 800})
        self.assertEqual(result.diagnostics["selected"]["score"], 0.95)

    def test_below_threshold_falls_back_and_keeps_matching_top_n(self):
        client = FakeOcrClient(
            [
                OcrItem("设置", 0.4, (0, 0, 10, 10), 0),
                OcrItem("设置", 0.3, (10, 10, 20, 20), 1),
                OcrItem("设置", 0.2, (20, 20, 30, 30), 2),
            ]
        )
        result = OcrEngine(
            client, min_score=0.5, diagnostic_top_n=2
        ).run(make_context())
        self.assertEqual(result.status, "no_match")
        self.assertEqual(result.diagnostics["reason"], "confidence_below_threshold")
        self.assertEqual(len(result.diagnostics["matching_candidates"]), 2)

    def test_non_click_instruction_does_not_call_service(self):
        client = FakeOcrClient([])
        result = OcrEngine(client).run(make_context("暂停视频"))
        self.assertEqual(result.status, "no_match")
        self.assertEqual(client.calls, 0)

    def test_configuration_error_is_recoverable(self):
        client = FakeOcrClient(ValueError("OCR_PROVIDER is not configured."))
        result = OcrEngine(client).run(make_context())
        self.assertEqual(result.status, "error")
        self.assertTrue(result.diagnostics["recoverable"])

    def test_empty_recognition_is_a_no_match(self):
        client = FakeOcrClient([])
        result = OcrEngine(client).run(make_context())
        self.assertEqual(result.status, "no_match")
        self.assertEqual(result.diagnostics["reason"], "no_text_match")

    def test_parse_pruned_result_with_polygon(self):
        items = parse_ocr_items(
            [
                {
                    "result": {
                        "ocrResults": [
                            {
                                "prunedResult": {
                                    "rec_texts": ["设置"],
                                    "rec_scores": [0.9],
                                    "rec_polys": [[[1, 2], [11, 2], [11, 12], [1, 12]]],
                                }
                            }
                        ]
                    }
                }
            ]
        )
        self.assertEqual(items[0].box, (1.0, 2.0, 11.0, 12.0))

    def test_local_client_posts_multipart_predict_request(self):
        payload = {
            "metrics": {"total_count": 1},
            "results": [{
                "id": 0,
                "text": "设置",
                "rec_score": 0.9,
                "det_score": 0.8,
                "bounding_box": [1, 2, 11, 12],
            }],
        }
        session = FakeSession(posts=[FakeResponse(payload)], gets=[])
        items = OcrClient(
            provider="local", local_url="http://localhost:8080/predict", session=session
        ).recognize(b"png")
        self.assertEqual(items[0].text, "设置")
        request_files = session.post_calls[0][1]["files"]
        self.assertEqual(request_files["file"], ("screenshot.png", b"png", "image/png"))
        self.assertNotIn("json", session.post_calls[0][1])

    def test_local_parser_supports_polygon_and_empty_results(self):
        items = parse_local_ocr_items({"results": [{
            "text": "设置",
            "rec_score": 0.91,
            "polygon_box": [[2, 3], [12, 3], [12, 13], [2, 13]],
        }]})
        self.assertEqual(items[0].box, (2.0, 3.0, 12.0, 13.0))
        self.assertEqual(parse_local_ocr_items({"results": []}), [])

    def test_transient_tls_failure_is_retried(self):
        payload = {
            "results": [{
                "text": "设置",
                "rec_score": 0.9,
                "bounding_box": [1, 2, 11, 12],
            }]
        }
        session = FakeSession(
            posts=[requests.exceptions.SSLError("temporary TLS failure"), FakeResponse(payload)],
            gets=[],
        )
        client = OcrClient(
            provider="local",
            local_url="http://localhost:8080/predict",
            connection_retries=1,
            session=session,
        )
        with patch("engines.ocr.client.time.sleep"):
            items = client.recognize(b"png")
        self.assertEqual(items[0].text, "设置")
        self.assertEqual(len(session.post_calls), 2)

    def test_cloud_client_submits_polls_and_downloads_jsonl(self):
        jsonl = json.dumps(
            {
                "result": {
                    "ocrResults": [
                        {
                            "prunedResult": {
                                "rec_texts": ["设置"],
                                "rec_scores": [0.91],
                                "rec_boxes": [[1, 2, 11, 12]],
                            }
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
        session = FakeSession(
            posts=[FakeResponse({"data": {"jobId": "job-1"}})],
            gets=[
                FakeResponse(
                    {
                        "data": {
                            "state": "done",
                            "resultUrl": {"jsonUrl": "https://result.test/out.jsonl"},
                        }
                    }
                ),
                FakeResponse(text=jsonl),
            ],
        )
        client = OcrClient(
            provider="cloud",
            cloud_job_url="https://cloud.test/jobs",
            cloud_token="secret",
            poll_interval_seconds=0.001,
            session=session,
        )
        items = client.recognize(b"png")
        self.assertEqual(items[0].text, "设置")
        self.assertEqual(items[0].score, 0.91)
        post_kwargs = session.post_calls[0][1]
        self.assertEqual(post_kwargs["data"]["model"], "PP-OCRv6")
        self.assertEqual(post_kwargs["headers"]["Authorization"], "bearer secret")
        self.assertEqual(post_kwargs["files"]["file"][1], b"png")

    def test_cloud_failed_job_is_reported_by_engine_as_recoverable(self):
        session = FakeSession(
            posts=[FakeResponse({"data": {"jobId": "job-1"}})],
            gets=[
                FakeResponse(
                    {"data": {"state": "failed", "errorMsg": "model error"}}
                )
            ],
        )
        client = OcrClient(
            provider="cloud",
            cloud_job_url="https://cloud.test/jobs",
            cloud_token="secret",
            session=session,
        )
        result = OcrEngine(client).run(make_context())
        self.assertEqual(result.status, "error")
        self.assertTrue(result.diagnostics["recoverable"])
        self.assertIn("model error", result.diagnostics["error"])


if __name__ == "__main__":
    unittest.main()
