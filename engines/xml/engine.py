"""Select a click action by deterministic XML matching."""

from __future__ import annotations

import time

from contracts import EngineContext, EngineResult
from engines.preprocessing import CLICK_INSTRUCTION_KEY
from .matcher import resolve_click_match
from .router import click_action_for_target


class XmlEngine:
    name = "xml"
    priority = 100

    def supports(self, context: EngineContext) -> bool:
        return context.execution_input.xml_root is not None

    def run(self, context: EngineContext) -> EngineResult:
        started = time.perf_counter()
        execution_input = context.execution_input
        intent = context.prepared.get(CLICK_INSTRUCTION_KEY)
        if intent is None:
            return EngineResult("no_match", self.name, diagnostics={"reason": "not_a_simple_click_instruction"}, timings_seconds={"engine": time.perf_counter() - started})
        try:
            match = resolve_click_match(execution_input.xml_root, intent)
        except (RuntimeError, ValueError) as error:
            return EngineResult("error", self.name, diagnostics={"error": str(error), "recoverable": True}, timings_seconds={"engine": time.perf_counter() - started})
        diagnostics: dict[str, object] = {"reason": match.reason, "intent": "click_text", "target_text": intent.target_text, "candidate_count": match.candidate_count}
        if match.target is None:
            return EngineResult("no_match", self.name, diagnostics=diagnostics, timings_seconds={"engine": time.perf_counter() - started})
        diagnostics.update({"matched_text": match.target.matched_text, "bounds": "[%d,%d][%d,%d]" % match.target.bounds})
        return EngineResult("selected", self.name, action=click_action_for_target(match.target, width=execution_input.snapshot.width, height=execution_input.snapshot.height), diagnostics=diagnostics, timings_seconds={"engine": time.perf_counter() - started})
