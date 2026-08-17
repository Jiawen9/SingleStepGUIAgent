"""XML-engine preprocessing that never mutates run input."""

from __future__ import annotations

from typing import Protocol

from contracts import EngineContext
from .instruction import parse_click_instruction


class Preprocessor(Protocol):
    name: str

    def supports(self, context: EngineContext) -> bool: ...

    def process(self, context: EngineContext) -> None: ...


class XmlInstructionPreprocessor:
    name = "xml_instruction"

    def supports(self, context: EngineContext) -> bool:
        return context.execution_input.xml_root is not None

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
