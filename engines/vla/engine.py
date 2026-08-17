"""Select an action with the configured vision-language model."""

from __future__ import annotations

import time

from contracts import EngineContext, EngineResult
from .client import VlaApiClient


class VlaEngine:
    name = "vla"
    priority = 200

    def __init__(self, client: VlaApiClient):
        self.client = client

    def supports(self, context: EngineContext) -> bool:
        return True

    def run(self, context: EngineContext) -> EngineResult:
        started = time.perf_counter()
        execution_input = context.execution_input
        try:
            action, raw_response = self.client.choose_action(instruction=execution_input.instruction, snapshot=execution_input.snapshot, app_package=execution_input.app_package)
        except (OSError, RuntimeError, ValueError) as error:
            return EngineResult("error", self.name, diagnostics={"error": str(error), "recoverable": False}, timings_seconds={"engine": time.perf_counter() - started})
        return EngineResult("selected", self.name, action=action, diagnostics={"raw_response": raw_response}, timings_seconds={"engine": time.perf_counter() - started})
