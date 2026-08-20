"""Shared data contracts between pipeline layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


@dataclass(frozen=True)
class ScreenSnapshot:
    png: bytes
    width: int
    height: int
    serial: str | None


@dataclass(frozen=True)
class ActionSelection:
    name: str
    arguments: dict[str, Any]
    prompt_action: dict[str, Any] | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class ExecutionResult:
    action_name: str
    status: str
    message: str
    timings_seconds: dict[str, float] = field(default_factory=dict)
    timing_details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    rejection: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionInput:
    """Immutable input shared by every engine in one run."""

    case_id: str
    instruction: str
    snapshot: ScreenSnapshot
    app_package: str | None
    artifact_directory: Path
    initial_xml: Path | None = None
    xml_root: ElementTree.Element | None = field(default=None, compare=False, repr=False)
    input_diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineContext:
    """Execution input plus isolated products created by preprocessors."""

    execution_input: ExecutionInput
    prepared: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class EngineResult:
    """Result returned by every action-producing engine."""

    status: str
    source: str
    action: ActionSelection | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    timings_seconds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"selected", "no_match", "error"}:
            raise ValueError(f"Invalid decision status: {self.status}")
        if (self.status == "selected") != (self.action is not None):
            raise ValueError("Only a selected decision may contain an action.")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "status": self.status,
            "source": self.source,
            "diagnostics": dict(self.diagnostics),
            "timings_seconds": {
                key: round(value, 6) for key, value in self.timings_seconds.items()
            },
        }
        if self.action is not None:
            payload["action"] = {
                "name": self.action.name,
                "arguments": dict(self.action.arguments),
            }
        return payload


@dataclass(frozen=True)
class ExecutionCommand:
    """Pure, serializable command produced from one validated action."""

    kind: str
    target: str
    arguments: dict[str, Any]
    action: ActionSelection

    def __post_init__(self) -> None:
        if self.kind not in {"adb", "atomic_tool", "reject", "evaluation"}:
            raise ValueError(f"Invalid execution command kind: {self.kind}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "arguments": dict(self.arguments),
            "action": {
                "name": self.action.name,
                "arguments": dict(self.action.arguments),
            },
        }


@dataclass(frozen=True)
class PipelineResult:
    status: str
    execution_input: ExecutionInput
    engine_results: tuple[EngineResult, ...]
    selected_engine_result: EngineResult | None
    command: ExecutionCommand | None
    execution: ExecutionResult | None
    timings_seconds: dict[str, float]
    result_path: Path


@dataclass(frozen=True)
class DecisionOutcome:
    """Pure decision output shared by normal runs and offline evaluation."""

    engine_results: tuple[EngineResult, ...]
    selected_engine_result: EngineResult | None
    command: ExecutionCommand | None
    timings_seconds: dict[str, float]
