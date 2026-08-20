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
    ocr_provider: str = ""
    ocr_cloud_job_url: str = ""
    ocr_cloud_token: str = ""
    ocr_local_url: str = ""
    ocr_min_score: float = 0.5
    ocr_diagnostic_top_n: int = 3
    ocr_timeout_seconds: float = 120.0
    ocr_poll_interval_seconds: float = 5.0
    ocr_connection_retries: int = 3
    ocr_retry_backoff_seconds: float = 2.0

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
        ocr_provider: str | None = None,
        ocr_cloud_job_url: str | None = None,
        ocr_cloud_token: str | None = None,
        ocr_local_url: str | None = None,
        ocr_min_score: float | None = None,
        ocr_diagnostic_top_n: int | None = None,
        ocr_timeout_seconds: float | None = None,
        ocr_poll_interval_seconds: float | None = None,
        ocr_connection_retries: int | None = None,
        ocr_retry_backoff_seconds: float | None = None,
    ) -> "AgentConfig":
        key = api_key.strip()
        if timeout_seconds <= 0:
            raise ValueError("API timeout must be positive.")

        resolved_ocr_provider = (
            ocr_provider if ocr_provider is not None else os.environ.get("OCR_PROVIDER", "")
        ).strip().lower()
        if resolved_ocr_provider not in {"", "cloud", "local"}:
            raise ValueError("OCR_PROVIDER must be 'cloud' or 'local'.")

        def _float_setting(value: float | None, name: str, default: float) -> float:
            raw = value if value is not None else os.environ.get(name, str(default))
            try:
                return float(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a number.") from error

        resolved_ocr_min_score = _float_setting(
            ocr_min_score, "OCR_MIN_SCORE", 0.5
        )
        resolved_ocr_timeout = _float_setting(
            ocr_timeout_seconds, "OCR_TIMEOUT_SECONDS", 120.0
        )
        resolved_ocr_poll = _float_setting(
            ocr_poll_interval_seconds, "OCR_POLL_INTERVAL_SECONDS", 5.0
        )
        resolved_ocr_retry_backoff = _float_setting(
            ocr_retry_backoff_seconds, "OCR_RETRY_BACKOFF_SECONDS", 2.0
        )
        raw_top_n = (
            ocr_diagnostic_top_n
            if ocr_diagnostic_top_n is not None
            else os.environ.get("OCR_DIAGNOSTIC_TOP_N", "3")
        )
        try:
            resolved_ocr_top_n = int(raw_top_n)
        except (TypeError, ValueError) as error:
            raise ValueError("OCR_DIAGNOSTIC_TOP_N must be an integer.") from error
        raw_retries = (
            ocr_connection_retries
            if ocr_connection_retries is not None
            else os.environ.get("OCR_CONNECTION_RETRIES", "3")
        )
        try:
            resolved_ocr_retries = int(raw_retries)
        except (TypeError, ValueError) as error:
            raise ValueError("OCR_CONNECTION_RETRIES must be an integer.") from error
        if not 0 <= resolved_ocr_min_score <= 1:
            raise ValueError("OCR_MIN_SCORE must be between 0 and 1.")
        if resolved_ocr_top_n < 0:
            raise ValueError("OCR_DIAGNOSTIC_TOP_N cannot be negative.")
        if resolved_ocr_retries < 0:
            raise ValueError("OCR_CONNECTION_RETRIES cannot be negative.")
        if resolved_ocr_retry_backoff < 0:
            raise ValueError("OCR_RETRY_BACKOFF_SECONDS cannot be negative.")
        if resolved_ocr_timeout <= 0 or resolved_ocr_poll <= 0:
            raise ValueError("OCR timeout and poll interval must be positive.")

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
            ocr_provider=resolved_ocr_provider,
            ocr_cloud_job_url=(
                ocr_cloud_job_url
                if ocr_cloud_job_url is not None
                else os.environ.get("OCR_CLOUD_JOB_URL", "")
            ).strip(),
            ocr_cloud_token=(
                ocr_cloud_token
                if ocr_cloud_token is not None
                else os.environ.get("OCR_CLOUD_TOKEN", "")
            ).strip(),
            ocr_local_url=(
                ocr_local_url
                if ocr_local_url is not None
                else os.environ.get("OCR_LOCAL_URL", "")
            ).strip(),
            ocr_min_score=resolved_ocr_min_score,
            ocr_diagnostic_top_n=resolved_ocr_top_n,
            ocr_timeout_seconds=resolved_ocr_timeout,
            ocr_poll_interval_seconds=resolved_ocr_poll,
            ocr_connection_retries=resolved_ocr_retries,
            ocr_retry_backoff_seconds=resolved_ocr_retry_backoff,
        )
