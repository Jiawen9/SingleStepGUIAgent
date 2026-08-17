"""Shared engine interface."""

from __future__ import annotations

from typing import Protocol

from contracts import EngineContext, EngineResult


class Engine(Protocol):
    name: str
    priority: int

    def supports(self, context: EngineContext) -> bool: ...

    def run(self, context: EngineContext) -> EngineResult: ...
