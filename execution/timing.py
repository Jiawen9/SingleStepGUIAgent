"""Machine-readable timing exchange with local iQIYI atomic scripts."""

from __future__ import annotations

import json
import os
from typing import Any


TIMING_PREFIX = "__GUI_AGENT_TIMING__="
RESULT_PREFIX = "__GUI_AGENT_RESULT__="


def emit_atomic_result(
    *,
    status: str,
    message: str,
    rejection: dict[str, Any] | None = None,
) -> None:
    if os.environ.get("GUI_AGENT_CAPTURE_TIMING") != "1":
        return
    payload: dict[str, Any] = {"status": status, "message": message}
    if rejection is not None:
        payload["rejection"] = rejection
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def extract_atomic_result(
    stdout: str,
) -> tuple[str, dict[str, Any] | None]:
    visible_lines: list[str] = []
    outcome: dict[str, Any] | None = None
    for line in stdout.splitlines():
        if not line.startswith(RESULT_PREFIX):
            visible_lines.append(line)
            continue
        try:
            payload: Any = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError as error:
            raise RuntimeError("iQIYI tool returned malformed result JSON.") from error
        if not isinstance(payload, dict):
            raise RuntimeError("iQIYI tool result payload must be an object.")
        status = payload.get("status")
        message = payload.get("message")
        if status not in {"executed", "rejected"}:
            raise RuntimeError("iQIYI tool result status is invalid.")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("iQIYI tool result message is invalid.")
        rejection = payload.get("rejection")
        if rejection is not None and not isinstance(rejection, dict):
            raise RuntimeError("iQIYI tool rejection payload is invalid.")
        outcome = payload
    return "\n".join(visible_lines), outcome


def emit_atomic_timing(
    *,
    dump_xml_seconds: float,
    adb_seconds: float,
    total_seconds: float,
    dump_details: list[dict[str, Any]] | None = None,
    adb_details: list[dict[str, Any]] | None = None,
) -> None:
    if os.environ.get("GUI_AGENT_CAPTURE_TIMING") != "1":
        return
    payload = {
        "dump_xml": round(dump_xml_seconds, 6),
        "adb_execution": round(adb_seconds, 6),
        "atomic_total": round(total_seconds, 6),
        "details": {
            "dump_xml": _rounded_details(dump_details or []),
            "adb_execution": _rounded_details(adb_details or []),
        },
    }
    print(TIMING_PREFIX + json.dumps(payload, separators=(",", ":")))


def extract_atomic_timing(
    stdout: str,
) -> tuple[str, dict[str, float], dict[str, list[dict[str, Any]]]]:
    visible_lines: list[str] = []
    timings: dict[str, float] = {}
    details: dict[str, list[dict[str, Any]]] = {
        "dump_xml": [],
        "adb_execution": [],
    }
    for line in stdout.splitlines():
        if not line.startswith(TIMING_PREFIX):
            visible_lines.append(line)
            continue
        try:
            payload: Any = json.loads(line[len(TIMING_PREFIX) :])
        except json.JSONDecodeError as error:
            raise RuntimeError("iQIYI tool returned malformed timing JSON.") from error
        if not isinstance(payload, dict):
            raise RuntimeError("iQIYI tool timing payload must be an object.")
        for key in ("dump_xml", "adb_execution", "atomic_total"):
            value = payload.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RuntimeError(f"iQIYI tool timing field is invalid: {key}")
            timings[key] = float(value)
        raw_details = payload.get("details", {})
        if not isinstance(raw_details, dict):
            raise RuntimeError("iQIYI tool timing details must be an object.")
        for key in details:
            entries = raw_details.get(key, [])
            if not isinstance(entries, list) or not all(
                isinstance(entry, dict) for entry in entries
            ):
                raise RuntimeError(f"iQIYI tool timing details are invalid: {key}")
            for entry in entries:
                seconds = entry.get("seconds")
                if (
                    not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool)
                    or seconds < 0
                ):
                    raise RuntimeError(
                        f"iQIYI tool timing detail seconds are invalid: {key}"
                    )
            details[key].extend(entries)
    return "\n".join(visible_lines).strip(), timings, details


def _rounded_details(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rounded: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        seconds = item.get("seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            item["seconds"] = round(float(seconds), 6)
        rounded.append(item)
    return rounded
