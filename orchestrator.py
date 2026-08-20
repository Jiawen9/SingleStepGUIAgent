"""Single-step pipeline orchestration and command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from engines.base import Engine
from engines.ocr.client import OcrClient
from engines.ocr.engine import OcrEngine
from engines.preprocessing import ClickInstructionPreprocessor, run_preprocessors
from engines.registry import order_engines
from engines.validation import build_action_specs, normalize_action, validate_action
from engines.vla.client import VlaApiClient
from engines.vla.engine import VlaEngine
from engines.vla.prompts import load_app_prompt
from engines.xml.engine import XmlEngine
from execution.atomic_tools.iqiyi.mode import (
    ACTION_MODES,
    MODE_ENVIRONMENT_VARIABLE,
    normalize_action_mode,
)
from execution.executor import ActionExecutor
from device.adb import AdbController, AdbError
from storage.artifacts import (
    save_done_screenshot,
    save_draw_screenshot,
    save_ocr_screenshot,
    save_pipeline_result,
    save_prompt,
)
from config import AgentConfig, load_env_file
from device.xml_hierarchy import XmlExecutionContext
from input.collector import InputCollector
from contracts import DecisionOutcome, EngineContext, EngineResult, ExecutionInput, PipelineResult
from output.commands import CommandBuilder
from output.serialization import action_as_prompt_object


DEFAULT_ENGINE_ORDER = ("xml", "ocr", "vla")
PROJECT_ROOT = Path(__file__).resolve().parent


class Pipeline:
    def __init__(
        self,
        *,
        config: AgentConfig,
        project_root: Path,
        engine_order: tuple[str, ...] = DEFAULT_ENGINE_ORDER,
        engines: tuple[Engine, ...] | None = None,
        iqiyi_action_mode: str = "medium",
    ):
        self.config = config
        self.project_root = project_root
        self.adb = AdbController(config.adb_path)
        self.input_collector = InputCollector(self.adb, project_root)
        self.client = VlaApiClient(
            api_key=config.api_key,
            api_base=config.api_base,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        self.ocr_client = OcrClient(
            provider=config.ocr_provider,
            cloud_job_url=config.ocr_cloud_job_url,
            cloud_token=config.ocr_cloud_token,
            local_url=config.ocr_local_url,
            timeout_seconds=config.ocr_timeout_seconds,
            poll_interval_seconds=config.ocr_poll_interval_seconds,
            connection_retries=config.ocr_connection_retries,
            retry_backoff_seconds=config.ocr_retry_backoff_seconds,
        )
        available = engines or (
            XmlEngine(),
            OcrEngine(
                self.ocr_client,
                min_score=config.ocr_min_score,
                diagnostic_top_n=config.ocr_diagnostic_top_n,
            ),
            VlaEngine(self.client),
        )
        self.engines = order_engines(available, engine_order)
        self.preprocessors = (ClickInstructionPreprocessor(),)
        self.command_builder = CommandBuilder()
        self.executor = ActionExecutor(
            self.adb,
            project_root,
            iqiyi_action_mode=iqiyi_action_mode,
        )

    def decide(
        self,
        execution_input: ExecutionInput,
        *,
        paths=None,
    ) -> DecisionOutcome:
        """Run preprocessing and the configured engine chain without execution."""
        started = time.perf_counter()
        context = EngineContext(execution_input)
        run_preprocessors(context, self.preprocessors)
        engine_results: list[EngineResult] = []
        selected: EngineResult | None = None

        for engine in self.engines:
            if not engine.supports(context):
                continue
            try:
                engine_result = engine.run(context)
            except (OSError, RuntimeError, ValueError) as error:
                engine_result = EngineResult(
                    "error",
                    engine.name,
                    diagnostics={"error": str(error), "recoverable": True},
                )
            engine_results.append(engine_result)
            if engine_result.source == "ocr" and paths is not None:
                save_ocr_screenshot(
                    paths=paths,
                    snapshot=execution_input.snapshot,
                    items=list(context.runtime.get("ocr_items", [])),
                    error=context.runtime.get("ocr_error"),
                )
            if engine_result.status == "selected":
                selected = engine_result
                break

        if selected is None or selected.action is None:
            return DecisionOutcome(
                tuple(engine_results),
                None,
                None,
                {
                    "engines": sum(
                        result.timings_seconds.get("engine", 0.0)
                        for result in engine_results
                    ),
                    "decision": time.perf_counter() - started,
                },
            )

        action = normalize_action(selected.action)
        app_prompt = load_app_prompt(execution_input.app_package)
        action_names = app_prompt.action_names if app_prompt is not None else frozenset()
        specs = build_action_specs(
            execution_input.snapshot.width,
            execution_input.snapshot.height,
            action_names,
        )
        validate_action(
            action,
            specs,
            execution_input.snapshot.width,
            execution_input.snapshot.height,
        )
        if action is not selected.action:
            selected = type(selected)(
                selected.status,
                selected.source,
                action,
                selected.diagnostics,
                selected.timings_seconds,
            )
            engine_results[-1] = selected
        command = self.command_builder.build(
            action,
            execution_input.snapshot,
            app_prompt.app_id if app_prompt is not None else "",
        )
        return DecisionOutcome(
            tuple(engine_results),
            selected,
            command,
            {
                "engines": sum(
                    result.timings_seconds.get("engine", 0.0)
                    for result in engine_results
                ),
                "decision": time.perf_counter() - started,
            },
        )

    def run(
        self,
        *,
        case_id: str,
        instruction: str,
        serial: str | None = None,
        screenshot_path: Path | None = None,
        app_package: str | None = None,
        dry_run: bool = False,
        done_delay: float = 1.0,
    ) -> PipelineResult:
        if done_delay < 0:
            raise ValueError("done_delay cannot be negative.")
        started = time.perf_counter()
        execution_input, paths = self.input_collector.collect(
            case_id=case_id,
            instruction=instruction,
            serial=serial or self.config.device_id,
            screenshot_path=screenshot_path,
            app_package=app_package,
        )
        save_prompt(
            paths=paths,
            instruction=execution_input.instruction,
            app_package=execution_input.app_package,
        )
        decision = self.decide(execution_input, paths=paths)
        engine_results = list(decision.engine_results)
        selected = decision.selected_engine_result

        if selected is None or selected.action is None:
            elapsed = {"total": time.perf_counter() - started}
            message = "No engine selected an action."
            save_pipeline_result(
                paths=paths,
                status="failed",
                execution_input=execution_input,
                engine_results=tuple(engine_results),
                timings_seconds=elapsed,
                error=message,
            )
            raise RuntimeError(message)

        action = selected.action
        command = decision.command
        if command is None:
            raise RuntimeError("Selected engine did not produce a command.")
        save_draw_screenshot(paths=paths, snapshot=execution_input.snapshot, selection=action)

        xml_context = None
        if execution_input.initial_xml is not None:
            xml_context = XmlExecutionContext(
                initial_xml=execution_input.initial_xml,
                output_dir=execution_input.artifact_directory,
                case_id=execution_input.case_id,
                start_index=1,
            )
        if dry_run:
            execution = None
            status = "dry_run"
            save_done_screenshot(paths=paths, snapshot=execution_input.snapshot)
        else:
            execution = self.executor.execute_command(
                command, execution_input.snapshot, xml_context
            )
            status = execution.status
            if execution.status == "executed" or (
                execution.rejection is not None
                and execution.rejection.get("source") == "atomic_tool"
            ):
                if done_delay:
                    time.sleep(done_delay)
                done_snapshot = self.adb.capture(
                    execution_input.snapshot.serial or serial
                )
            else:
                done_snapshot = execution_input.snapshot
            save_done_screenshot(paths=paths, snapshot=done_snapshot)

        timings = {
            "capture": float(execution_input.input_diagnostics.get("capture_seconds", 0.0)),
            "dump_xml": float(execution_input.input_diagnostics.get("xml_dump_seconds", 0.0)),
            "engines": sum(
                result.timings_seconds.get("engine", 0.0)
                for result in engine_results
            ),
            "decision": decision.timings_seconds.get("decision", 0.0),
            "execution": (
                execution.timings_seconds.get("adb_execution", 0.0)
                if execution is not None
                else 0.0
            ),
            "total": time.perf_counter() - started,
        }
        save_pipeline_result(
            paths=paths,
            status=status,
            execution_input=execution_input,
            engine_results=tuple(engine_results),
            selected_engine_result=selected,
            command=command,
            execution=execution,
            timings_seconds=timings,
        )
        return PipelineResult(
            status,
            execution_input,
            tuple(engine_results),
            selected,
            command,
            execution,
            timings,
            paths.result,
        )


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect one input, decide one action, execute once."
    )
    parser.add_argument("case_id")
    parser.add_argument("instruction")
    parser.add_argument("-s", "--serial", default=os.environ.get("DEVICE_ID"))
    parser.add_argument("--app-package")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-key")
    parser.add_argument(
        "--model-url",
        "--api-base",
        dest="model_url",
        default=os.environ.get("MODEL_URL")
        or os.environ.get("YUNAI_API_BASE")
        or None,
    )
    parser.add_argument(
        "--model-name",
        "--model",
        dest="model_name",
        default=os.environ.get("MODEL_NAME")
        or os.environ.get("YUNAI_MODEL")
        or None,
    )
    parser.add_argument("--adb", default=os.environ.get("ADB_PATH"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--raw-response", action="store_true")
    parser.add_argument("--done-delay", type=float, default=1.0)
    parser.add_argument(
        "--engine",
        dest="engines",
        action="append",
        choices=("xml", "ocr", "vla"),
        help="Engine in priority order; repeat to configure the chain.",
    )
    parser.add_argument(
        "--iqiyi-mode",
        choices=ACTION_MODES,
        default=normalize_action_mode(
            os.environ.get(MODE_ENVIRONMENT_VARIABLE, "medium")
        ),
    )
    return parser.parse_args(argv)


def _resolve_api_key(command_line_key: str | None) -> str:
    if command_line_key is not None:
        return command_line_key.strip()
    if "MODEL_API_KEY" in os.environ:
        return os.environ["MODEL_API_KEY"].strip()
    return os.environ.get("YUNAI_API_KEY", "").strip()


def main(argv: list[str] | None = None) -> int:
    try:
        load_env_file(PROJECT_ROOT / ".env")
        args = parse_arguments(argv)
        config = AgentConfig.from_values(
            api_key=_resolve_api_key(args.api_key),
            api_base=args.model_url,
            model=args.model_name,
            adb_path=args.adb,
            device_id=args.serial,
            timeout_seconds=args.timeout,
        )
        pipeline = Pipeline(
            config=config,
            project_root=PROJECT_ROOT,
            engine_order=tuple(args.engines or DEFAULT_ENGINE_ORDER),
            iqiyi_action_mode=args.iqiyi_mode,
        )
        result = pipeline.run(
            case_id=args.case_id,
            instruction=args.instruction,
            serial=args.serial,
            screenshot_path=args.screenshot,
            app_package=args.app_package,
            dry_run=args.dry_run,
            done_delay=args.done_delay,
        )
        selected = result.selected_engine_result
        if selected is not None and selected.action is not None:
            print(f"执行主体：{selected.source}")
            print(
                "标准动作："
                + json.dumps(
                    action_as_prompt_object(selected.action), ensure_ascii=False
                )
            )
            if args.raw_response and selected.source == "vla":
                print(
                    json.dumps(
                        selected.diagnostics.get("raw_response"),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        if result.execution is not None:
            prefix = "已拒绝" if result.execution.status == "rejected" else "执行完成"
            print(f"{prefix}：{result.execution.message}")
        else:
            print("dry-run：未执行动作。")
        print(f"统一结果：{result.result_path}")
        return 0
    except (AdbError, FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
