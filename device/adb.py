"""Small ADB adapter used by screenshots and primitive GUI actions."""

from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

from contracts import ScreenSnapshot


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FOREGROUND_PACKAGE_PATTERNS = (
    re.compile(r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9.$_]+)"),
    re.compile(r"mFocusedApp=.*?\s([A-Za-z0-9._]+)/(?:[A-Za-z0-9.$_]+)"),
    re.compile(
        r"(?:mResumedActivity|topResumedActivity|ResumedActivity):"
        r".*?\su\d+\s+([A-Za-z0-9._]+)/[A-Za-z0-9.$_]+"
    ),
)


class AdbError(RuntimeError):
    """Raised when ADB cannot complete an operation."""


class AdbController:
    def __init__(self, adb_path: Path):
        self.adb_path = adb_path

    def _base_command(self, serial: str | None = None) -> list[str]:
        command = [str(self.adb_path)]
        if serial:
            command.extend(["-s", serial])
        return command

    def _check_adb(self) -> None:
        if not self.adb_path.is_file():
            raise FileNotFoundError(f"adb was not found: {self.adb_path}")

    def _run_text(
        self,
        arguments: list[str],
        *,
        serial: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._check_adb()
        result = subprocess.run(
            self._base_command(serial) + arguments,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise AdbError(f"adb command failed: {detail}")
        return result

    def select_device(
        self,
        requested_serial: str | None = None,
    ) -> str:
        if requested_serial:
            result = self._run_text(
                ["get-state"], serial=requested_serial, check=False
            )
            if result.returncode != 0 or result.stdout.strip() != "device":
                raise AdbError(f"Device is unavailable: {requested_serial}")
            return requested_serial

        result = self._run_text(["devices"])
        devices: list[str] = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        if not devices:
            raise AdbError("No authorized adb device is connected.")
        if len(devices) > 1:
            raise AdbError(
                "Multiple devices found; use --serial: " + ", ".join(devices)
            )
        return devices[0]

    @staticmethod
    def png_dimensions(png: bytes) -> tuple[int, int]:
        if len(png) < 24 or not png.startswith(PNG_SIGNATURE):
            raise ValueError("The screenshot is not a valid PNG file.")
        if png[12:16] != b"IHDR":
            raise ValueError("The PNG screenshot has no IHDR header.")
        width, height = struct.unpack(">II", png[16:24])
        if width <= 0 or height <= 0:
            raise ValueError("The PNG screenshot has invalid dimensions.")
        return width, height

    def capture(
        self,
        requested_serial: str | None = None,
    ) -> ScreenSnapshot:
        self._check_adb()
        serial = self.select_device(requested_serial)
        result = subprocess.run(
            self._base_command(serial) + ["exec-out", "screencap", "-p"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise AdbError(f"Screenshot failed: {detail or 'unknown error'}")
        width, height = self.png_dimensions(result.stdout)
        return ScreenSnapshot(result.stdout, width, height, serial)

    def foreground_package(self, serial: str) -> str | None:
        """Return the focused Android package, or None if it is unavailable."""
        for service in ("window windows", "activity activities"):
            result = self._run_text(
                ["shell", "dumpsys", *service.split()],
                serial=serial,
                check=False,
            )
            if result.returncode != 0:
                continue
            for pattern in FOREGROUND_PACKAGE_PATTERNS:
                match = pattern.search(result.stdout)
                if match:
                    return match.group(1)
        return None

    def tap(self, serial: str, x: int, y: int) -> None:
        self._run_text(
            ["shell", "input", "tap", str(x), str(y)], serial=serial
        )

    def swipe(
        self,
        serial: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> None:
        self._run_text(
            [
                "shell",
                "input",
                "swipe",
                str(x1),
                str(y1),
                str(x2),
                str(y2),
            ],
            serial=serial,
        )
