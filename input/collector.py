"""Collect the immutable screenshot, package and first XML input for one run."""

from __future__ import annotations

import time
from pathlib import Path

from device.adb import AdbController
from storage.artifacts import (
    ArtifactPaths,
    prepare_run_artifacts,
    save_original_screenshot,
)
from device.xml_hierarchy import fast_dump_to_file, xml_artifact_path
from contracts import ExecutionInput, ScreenSnapshot


class InputCollector:
    def __init__(self, adb: AdbController, project_root: Path):
        self.adb = adb
        self.project_root = project_root

    def collect(
        self,
        *,
        case_id: str,
        instruction: str,
        serial: str | None = None,
        screenshot_path: Path | None = None,
        app_package: str | None = None,
    ) -> tuple[ExecutionInput, ArtifactPaths]:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("Instruction cannot be empty.")
        paths = prepare_run_artifacts(project_root=self.project_root, case_id=case_id)
        normalized_case_id = paths.directory.name
        capture_started = time.perf_counter()
        if screenshot_path is not None:
            resolved = screenshot_path.expanduser().resolve()
            png = resolved.read_bytes()
            width, height = self.adb.png_dimensions(png)
            snapshot = ScreenSnapshot(png, width, height, None)
            source = str(resolved)
        else:
            snapshot = self.adb.capture(serial)
            source = f"adb:{snapshot.serial}"
        capture_seconds = time.perf_counter() - capture_started

        detected_package = (
            app_package.strip()
            if app_package is not None and app_package.strip()
            else (
                self.adb.foreground_package(snapshot.serial)
                if snapshot.serial is not None
                else None
            )
        )
        save_original_screenshot(paths=paths, snapshot=snapshot)

        initial_xml = None
        xml_root = None
        diagnostics: dict[str, object] = {
            "source": source,
            "capture_seconds": capture_seconds,
            "xml_dump_seconds": 0.0,
        }
        if snapshot.serial is not None:
            initial_xml = xml_artifact_path(paths.directory, normalized_case_id, 0)
            try:
                hierarchy = fast_dump_to_file(
                    adb_path=self.adb.adb_path,
                    serial=snapshot.serial,
                    output_path=initial_xml,
                )
            except (OSError, RuntimeError, ValueError) as error:
                initial_xml = None
                diagnostics["xml_error"] = str(error)
            else:
                xml_root = hierarchy.root
                diagnostics["xml_dump_seconds"] = hierarchy.dump_seconds

        return (
            ExecutionInput(
                case_id=normalized_case_id,
                instruction=instruction,
                snapshot=snapshot,
                app_package=detected_package,
                artifact_directory=paths.directory,
                initial_xml=initial_xml,
                xml_root=xml_root,
                input_diagnostics=diagnostics,
            ),
            paths,
        )
