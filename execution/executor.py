"""Execute converted commands against ADB or atomic tool modules."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

from device.adb import AdbController
from device.xml_hierarchy import XmlExecutionContext
from contracts import ActionSelection, ExecutionCommand, ExecutionResult, ScreenSnapshot
from .atomic_tools.iqiyi.mode import MODE_ENVIRONMENT_VARIABLE, normalize_action_mode
from .timing import extract_atomic_result, extract_atomic_timing

VLA_COORDINATE_MAX = 1000


def vla_coordinate_to_pixel(value: int | float, size: int) -> int:
    if not 0 <= value <= VLA_COORDINATE_MAX:
        raise ValueError("VLA coordinate must be from 0 to 1000.")
    if size <= 0:
        raise ValueError("Original image size must be positive.")
    return round(value * (size - 1) / VLA_COORDINATE_MAX)


normalized_to_pixel = vla_coordinate_to_pixel


def connect_uiautomator2(adb_path: Path, serial: str):
    os.environ["ADBUTILS_ADB_PATH"] = str(adb_path)
    try:
        import uiautomator2 as u2
    except ImportError as error:
        raise RuntimeError("uiautomator2 is required for type actions.") from error
    device = u2.connect(serial)
    device.jsonrpc.setConfigurator({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
    return device


class ActionExecutor:
    def __init__(self, adb: AdbController, project_root: Path, *, iqiyi_action_mode: str = "medium"):
        self.adb = adb
        self.project_root = project_root
        self.iqiyi_action_mode = normalize_action_mode(iqiyi_action_mode)

    def execute_command(self, command: ExecutionCommand, snapshot: ScreenSnapshot, xml_context: XmlExecutionContext | None = None) -> ExecutionResult:
        action = command.action
        if command.kind == "evaluation":
            raise RuntimeError("Evaluation-only commands cannot be executed on a device.")
        if command.kind == "reject":
            message = "当前状态下无法可靠完成用户指令。"
            return ExecutionResult(action.name, "rejected", message, {"dump_xml": 0.0, "adb_execution": 0.0}, {"dump_xml": [], "adb_execution": []}, {"source": "vla", "message": message})

        serial = snapshot.serial or self.adb.select_device()
        if command.kind == "adb":
            started = time.perf_counter()
            if command.target == "tap":
                self.adb.tap(serial, command.arguments["x"], command.arguments["y"])
                message = f"Clicked pixel ({command.arguments['x']}, {command.arguments['y']}) on {serial}."
            elif command.target == "swipe":
                self.adb.swipe(serial=serial, **command.arguments)
                message = f"Swiped on {serial}."
            elif command.target == "type":
                text = command.arguments["text"]
                connect_uiautomator2(self.adb.adb_path, serial).send_keys(text, clear=False)
                message = f"Typed {len(text)} characters on {serial}."
            else:
                raise ValueError(f"Unknown ADB command target: {command.target}")
            seconds = time.perf_counter() - started
            detail = {"operation": command.target, **command.arguments, "seconds": seconds}
            if command.target == "type":
                detail["command"] = "uiautomator2 send_keys"
                detail["characters"] = len(command.arguments["text"])
                detail.pop("text", None)
            return ExecutionResult(action.name, "executed", message, {"dump_xml": 0.0, "adb_execution": seconds}, {"dump_xml": [], "adb_execution": [detail]})

        if xml_context is None:
            raise RuntimeError("An atomic tool requires the initial XML snapshot.")
        output, timings, details, outcome = self._run_iqiyi_tool(command.target, serial, xml_context, tuple(command.arguments.get("argv", ())))
        status = outcome["status"] if outcome is not None else "executed"
        message = outcome["message"] if outcome is not None else output
        rejection = outcome.get("rejection") if outcome is not None else None
        return ExecutionResult(action.name, status, message, timings, details, rejection)

    def execute(self, selection: ActionSelection, snapshot: ScreenSnapshot, xml_context: XmlExecutionContext | None = None) -> ExecutionResult:
        """Compatibility adapter; Pipeline callers use execute_command."""
        from output.commands import CommandBuilder
        return self.execute_command(CommandBuilder().build(selection, snapshot), snapshot, xml_context)

    def _run_atomic_tool(self, module_name: str, serial: str, xml_context: XmlExecutionContext | None = None, extra_arguments: tuple[str, ...] = ()) -> tuple[str, dict[str, float], dict[str, list[dict[str, object]]], dict[str, object] | None]:
        if importlib.util.find_spec(module_name) is None:
            raise FileNotFoundError(f"Atomic tool module was not found: {module_name}")
        command = [sys.executable, "-m", module_name, "--serial", serial, *extra_arguments]
        if xml_context is not None:
            command.extend(["--initial-xml", str(xml_context.initial_xml), "--xml-output-dir", str(xml_context.output_dir), "--xml-case-id", xml_context.case_id, "--xml-start-index", str(xml_context.start_index)])
        result = subprocess.run(command, cwd=self.project_root, text=True, encoding="utf-8", errors="strict", capture_output=True, check=False, env={**os.environ, "ADB_PATH": str(self.adb.adb_path), "GUI_AGENT_CAPTURE_TIMING": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", MODE_ENVIRONMENT_VARIABLE: self.iqiyi_action_mode})
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"Atomic tool failed: {detail}")
        timing_stdout, outcome = extract_atomic_result(result.stdout)
        output, timings, details = extract_atomic_timing(timing_stdout)
        if not timings:
            raise RuntimeError(f"Atomic tool returned no timing data: {module_name}")
        return output or f"{module_name} completed.", timings, details, outcome

    _run_iqiyi_tool = _run_atomic_tool
