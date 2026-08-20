"""Shared instruction preprocessing for deterministic engines."""

from __future__ import annotations

from typing import Protocol

from contracts import EngineContext
from .instruction import parse_click_instruction


CLICK_INSTRUCTION_KEY = "click_instruction"


class Preprocessor(Protocol):
    name: str

    def supports(self, context: EngineContext) -> bool: ...

    def process(self, context: EngineContext) -> None: ...


class ClickInstructionPreprocessor:
    """Parse a click intent once for UITree and OCR engines."""

    name = CLICK_INSTRUCTION_KEY

    def supports(self, context: EngineContext) -> bool:
        return True

    def process(self, context: EngineContext) -> None:
        context.prepared[self.name] = parse_click_instruction(
            context.execution_input.instruction
        )


def run_preprocessors(
    context: EngineContext, preprocessors: tuple[Preprocessor, ...]
) -> None:
    for preprocessor in preprocessors:
        if preprocessor.supports(context):
            preprocessor.process(context)
