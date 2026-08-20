#!/usr/bin/env python3
"""Capture one Android screenshot and one UI hierarchy from a single command."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import load_env_file


PROJECT_ROOT = Path(__file__).resolve().parent
TESTS_DIR = PROJECT_ROOT / "tests"
CAPTURE_SCRIPT = TESTS_DIR / "capture_phone_screen.py"
DUMP_SCRIPT = TESTS_DIR / "fast_dump_uiautomator2.py"
OUTPUT_DIR = PROJECT_ROOT / "device_captures"
SAFE_NAME_PATTERN = re.compile(r"[^0-9A-Za-z._-]+")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture the Android screen and UI XML into device_captures."
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Optional filename stem. The current timestamp is used by default.",
    )
    parser.add_argument(
        "-s",
        "--serial",
        default=os.environ.get("DEVICE_ID"),
        help="adb device serial (default: DEVICE_ID from the environment file).",
    )
    parser.add_argument(
        "--compressed",
        action="store_true",
        help="Omit unimportant UI nodes from the XML hierarchy.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=50,
        help="Maximum XML hierarchy depth (default: 50).",
    )
    return parser.parse_args()


def safe_stem(requested_name: str | None) -> str:
    if not requested_name:
        return f"device_{datetime.now():%Y%m%d_%H%M%S_%f}"

    stem = SAFE_NAME_PATTERN.sub("_", requested_name.strip()).strip("._-")
    if not stem:
        raise ValueError("name must contain at least one letter or number.")
    return stem


def available_stem(requested_name: str | None) -> str:
    base = safe_stem(requested_name)
    candidate = base
    index = 1
    while (OUTPUT_DIR / f"{candidate}.png").exists() or (
        OUTPUT_DIR / f"{candidate}.xml"
    ).exists():
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def run_script(script: Path, arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{script.name} failed with exit code {result.returncode}.")


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_arguments()
    if args.max_depth < 1:
        raise ValueError("--max-depth must be at least 1.")
    if not CAPTURE_SCRIPT.is_file() or not DUMP_SCRIPT.is_file():
        raise FileNotFoundError("The screenshot or XML dump helper script is missing.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = available_stem(args.name)
    screenshot_path = OUTPUT_DIR / f"{stem}.png"
    xml_path = OUTPUT_DIR / f"{stem}.xml"

    common_arguments: list[str] = []
    if args.serial:
        common_arguments.extend(["--serial", args.serial])

    print(f"Output directory: {OUTPUT_DIR}", flush=True)
    print("Capturing screenshot...", flush=True)
    run_script(
        CAPTURE_SCRIPT,
        [*common_arguments, "--output", str(screenshot_path)],
    )

    dump_arguments = [
        *common_arguments,
        "--output",
        str(xml_path),
        "--max-depth",
        str(args.max_depth),
    ]
    if args.compressed:
        dump_arguments.append("--compressed")

    print("Dumping UI XML...", flush=True)
    run_script(DUMP_SCRIPT, dump_arguments)

    print("Capture completed:")
    print(f"  Screenshot: {screenshot_path}")
    print(f"  UI XML:    {xml_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
