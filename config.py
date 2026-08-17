"""Runtime configuration for the GUI agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> bool:
    """Load a small dotenv-style file without adding a dependency.

    Existing process environment variables always win. The function never
    prints values, which keeps API keys out of normal program output.
    """
    if not path.is_file():
        return False

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid environment entry at {path}:{line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ValueError(f"Invalid environment name at {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return True


@dataclass(frozen=True)
class AgentConfig:
    api_base: str
    model: str
    adb_path: Path
    api_key: str = ""
    device_id: str | None = None
    timeout_seconds: float = 120.0

    @classmethod
    def from_values(
        cls,
        *,
        api_key: str = "",
        api_base: str | None = None,
        model: str | None = None,
        adb_path: str | Path | None = None,
        device_id: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> "AgentConfig":
        key = api_key.strip()
        if timeout_seconds <= 0:
            raise ValueError("API timeout must be positive.")

        resolved_api_base = (
            api_base
            or os.environ.get("MODEL_URL")
            or os.environ.get("YUNAI_API_BASE")
            or ""
        ).strip()
        resolved_model = (
            model
            or os.environ.get("MODEL_NAME")
            or os.environ.get("YUNAI_MODEL")
            or ""
        ).strip()
        resolved_adb_value = str(
            adb_path or os.environ.get("ADB_PATH") or ""
        ).strip()
        if not resolved_api_base:
            raise ValueError("MODEL_URL is required in .env or --model-url.")
        if not resolved_model:
            raise ValueError("MODEL_NAME is required in .env or --model-name.")
        if not resolved_adb_value:
            raise ValueError("ADB_PATH is required in .env or --adb.")

        return cls(
            api_key=key,
            api_base=resolved_api_base.rstrip("/"),
            model=resolved_model,
            adb_path=Path(resolved_adb_value).expanduser(),
            device_id=(
                device_id.strip()
                if device_id is not None and device_id.strip()
                else None
            ),
            timeout_seconds=timeout_seconds,
        )
