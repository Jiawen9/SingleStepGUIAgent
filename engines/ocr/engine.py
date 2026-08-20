"""Select a click action by exact text matching over PaddleOCR results."""

from __future__ import annotations

import json
import time

import requests

from contracts import ActionSelection, EngineContext, EngineResult
from engines.instruction import clean_ui_text
from engines.preprocessing import CLICK_INSTRUCTION_KEY
from .client import OcrClient, OcrItem


def _match_key(value: str) -> str:
    return clean_ui_text(value).casefold()


def _click_action(item: OcrItem, *, width: int, height: int) -> ActionSelection:
    left, top, right, bottom = item.box
    x = min(max((left + right) / 2, 0), max(width - 1, 0))
    y = min(max((top + bottom) / 2, 0), max(height - 1, 0))
    return ActionSelection(
        "click",
        {
            "x": round(x * 1000 / (width - 1)) if width > 1 else 0,
            "y": round(y * 1000 / (height - 1)) if height > 1 else 0,
        },
    )


class OcrEngine:
    name = "ocr"
    priority = 200

    def __init__(
        self,
        client: OcrClient,
        *,
        min_score: float = 0.5,
        diagnostic_top_n: int = 3,
    ):
        if not 0 <= min_score <= 1:
            raise ValueError("OCR_MIN_SCORE must be between 0 and 1.")
        if diagnostic_top_n < 0:
            raise ValueError("OCR_DIAGNOSTIC_TOP_N cannot be negative.")
        self.client = client
        self.min_score = min_score
        self.diagnostic_top_n = diagnostic_top_n

    def supports(self, context: EngineContext) -> bool:
        return True

    def run(self, context: EngineContext) -> EngineResult:
        started = time.perf_counter()
        intent = context.prepared.get(CLICK_INSTRUCTION_KEY)
        if intent is None:
            return EngineResult(
                "no_match",
                self.name,
                diagnostics={"reason": "not_a_simple_click_instruction"},
                timings_seconds={"engine": time.perf_counter() - started},
            )
        try:
            items = self.client.recognize(context.execution_input.snapshot.png)
            context.runtime["ocr_items"] = [item.as_dict() for item in items]
            target_keys = {
                _match_key(candidate)
                for candidate in (intent.target_candidates or (intent.target_text,))
            }
            matches = [item for item in items if _match_key(item.text) in target_keys]
            matches.sort(key=lambda item: (-item.score, item.source_index))
            diagnostics: dict[str, object] = {
                "reason": "no_text_match",
                "intent": "click_text",
                "target_text": intent.target_text,
                "provider": self.client.provider,
                "min_score": self.min_score,
                "matching_candidates": [
                    item.as_dict() for item in matches[: self.diagnostic_top_n]
                ],
            }
            if not matches:
                return EngineResult(
                    "no_match",
                    self.name,
                    diagnostics=diagnostics,
                    timings_seconds={"engine": time.perf_counter() - started},
                )
            selected = matches[0]
            if selected.score < self.min_score:
                diagnostics["reason"] = "confidence_below_threshold"
                return EngineResult(
                    "no_match",
                    self.name,
                    diagnostics=diagnostics,
                    timings_seconds={"engine": time.perf_counter() - started},
                )
            diagnostics.update(
                {"reason": "exact_text_match", "selected": selected.as_dict()}
            )
            snapshot = context.execution_input.snapshot
            return EngineResult(
                "selected",
                self.name,
                action=_click_action(selected, width=snapshot.width, height=snapshot.height),
                diagnostics=diagnostics,
                timings_seconds={"engine": time.perf_counter() - started},
            )
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
            requests.RequestException,
        ) as error:
            context.runtime["ocr_error"] = str(error)
            return EngineResult(
                "error",
                self.name,
                diagnostics={
                    "error": str(error),
                    "recoverable": True,
                    "provider": self.client.provider,
                },
                timings_seconds={"engine": time.perf_counter() - started},
            )
