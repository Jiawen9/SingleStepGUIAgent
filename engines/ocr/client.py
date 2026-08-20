"""HTTP adapters for PaddleOCR cloud jobs and local serving."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


@dataclass(frozen=True)
class OcrItem:
    text: str
    score: float
    box: tuple[float, float, float, float]
    source_index: int

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "score": self.score,
            "box": list(self.box),
        }


def _box_from_value(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("OCR box must be a list.")
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        left, top, right, bottom = (float(item) for item in value)
    elif len(value) >= 3 and all(
        isinstance(point, (list, tuple))
        and len(point) >= 2
        and isinstance(point[0], (int, float))
        and isinstance(point[1], (int, float))
        for point in value
    ):
        xs = [float(point[0]) for point in value]
        ys = [float(point[1]) for point in value]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    else:
        raise ValueError("OCR box has an unsupported shape.")
    if right < left or bottom < top:
        raise ValueError("OCR box coordinates are reversed.")
    return left, top, right, bottom


def _result_bundles(value: Any) -> Iterable[dict[str, Any]]:
    """Find PaddleOCR pruned results in cloud and local response envelopes."""
    if isinstance(value, dict):
        if "rec_texts" in value and "rec_scores" in value:
            yield value
            return
        for child in value.values():
            yield from _result_bundles(child)
    elif isinstance(value, list):
        for child in value:
            yield from _result_bundles(child)


def parse_ocr_items(payloads: Iterable[Any]) -> list[OcrItem]:
    items: list[OcrItem] = []
    source_index = 0
    found_result = False
    for payload in payloads:
        for result in _result_bundles(payload):
            found_result = True
            texts = result.get("rec_texts")
            scores = result.get("rec_scores")
            boxes = result.get("rec_boxes", result.get("rec_polys"))
            if not isinstance(texts, list) or not isinstance(scores, list):
                raise ValueError("OCR texts and scores must be lists.")
            if not isinstance(boxes, list):
                raise ValueError("OCR result does not contain rec_boxes or rec_polys.")
            if not (len(texts) == len(scores) == len(boxes)):
                raise ValueError("OCR texts, scores, and boxes have different lengths.")
            for text, score, box in zip(texts, scores, boxes):
                if not isinstance(text, str) or not isinstance(score, (int, float)):
                    raise ValueError("OCR text or confidence has an invalid type.")
                items.append(
                    OcrItem(text, float(score), _box_from_value(box), source_index)
                )
                source_index += 1
    if not found_result:
        raise ValueError("OCR response contains no recognition result fields.")
    return items


def parse_local_ocr_items(payload: Any) -> list[OcrItem]:
    """Parse responses produced by the local synchronous /predict service."""
    if not isinstance(payload, dict):
        raise ValueError("Local OCR response must be a JSON object.")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Local OCR response does not contain a results list.")

    items: list[OcrItem] = []
    for source_index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError("Local OCR result item must be a JSON object.")
        text = result.get("text")
        score = result.get("rec_score")
        box = result.get("bounding_box", result.get("polygon_box"))
        if not isinstance(text, str):
            raise ValueError("Local OCR result text has an invalid type.")
        if not isinstance(score, (int, float)):
            raise ValueError("Local OCR result rec_score has an invalid type.")
        items.append(OcrItem(text, float(score), _box_from_value(box), source_index))
    return items


class OcrClient:
    """Call either AI Studio cloud OCR or a local PaddleOCR endpoint."""

    def __init__(
        self,
        *,
        provider: str,
        cloud_job_url: str = "",
        cloud_token: str = "",
        local_url: str = "",
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 5.0,
        connection_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        session: requests.Session | None = None,
    ):
        self.provider = provider.strip().lower()
        self.cloud_job_url = cloud_job_url.strip().rstrip("/")
        self.cloud_token = cloud_token.strip()
        self.local_url = local_url.strip()
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        if connection_retries < 0:
            raise ValueError("OCR_CONNECTION_RETRIES cannot be negative.")
        if retry_backoff_seconds < 0:
            raise ValueError("OCR_RETRY_BACKOFF_SECONDS cannot be negative.")
        self.connection_retries = connection_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        # Match PaddleOCR's official sample by using the module-level
        # requests API by default. A transport can still be injected in tests.
        self.transport = session or requests

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Retry transient connection failures that have no HTTP response."""
        for attempt in range(self.connection_retries + 1):
            try:
                request = getattr(self.transport, method.lower())
                return request(url, **kwargs)
            except requests.ConnectionError as error:
                if attempt >= self.connection_retries:
                    raise requests.ConnectionError(
                        "OCR connection failed after "
                        f"{self.connection_retries + 1} attempts: {error}"
                    ) from error
                time.sleep(self.retry_backoff_seconds * (2**attempt))
        raise RuntimeError("Unreachable OCR retry state.")

    def recognize(self, png: bytes) -> list[OcrItem]:
        if self.provider == "cloud":
            return self._recognize_cloud(png)
        if self.provider == "local":
            return self._recognize_local(png)
        if not self.provider:
            raise ValueError("OCR_PROVIDER is not configured.")
        raise ValueError("OCR_PROVIDER must be 'cloud' or 'local'.")

    def _recognize_cloud(self, png: bytes) -> list[OcrItem]:
        if not self.cloud_job_url:
            raise ValueError("OCR_CLOUD_JOB_URL is required for cloud OCR.")
        if not self.cloud_token:
            raise ValueError("OCR_CLOUD_TOKEN is required for cloud OCR.")
        headers = {"Authorization": f"bearer {self.cloud_token}"}
        optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": False,
        }
        response = self._request(
            "post",
            self.cloud_job_url,
            headers=headers,
            data={
                "model": "PP-OCRv6",
                "optionalPayload": json.dumps(optional_payload),
            },
            files={"file": ("screenshot.png", png, "image/png")},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            job_id = response.json()["data"]["jobId"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Cloud OCR job response does not contain data.jobId.") from error

        deadline = time.monotonic() + self.timeout_seconds
        result_url = ""
        while time.monotonic() < deadline:
            poll = self._request(
                "get",
                f"{self.cloud_job_url}/{job_id}",
                headers=headers,
                timeout=min(self.timeout_seconds, max(0.1, deadline - time.monotonic())),
            )
            poll.raise_for_status()
            try:
                data = poll.json()["data"]
                state = data["state"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Cloud OCR status response is invalid.") from error
            if state == "done":
                try:
                    result_url = data["resultUrl"]["jsonUrl"]
                except (KeyError, TypeError) as error:
                    raise ValueError("Cloud OCR result URL is missing.") from error
                break
            if state == "failed":
                raise RuntimeError(
                    f"Cloud OCR job failed: {data.get('errorMsg', 'unknown error')}"
                )
            if state not in {"pending", "running"}:
                raise RuntimeError(f"Cloud OCR returned unknown job state: {state}")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval_seconds, remaining))
        if not result_url:
            raise TimeoutError("Cloud OCR job polling timed out.")

        result = self._request("get", result_url, timeout=self.timeout_seconds)
        result.raise_for_status()
        payloads: list[Any] = []
        try:
            jsonl_text = result.content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Cloud OCR JSONL result is not valid UTF-8.") from error
        for line in jsonl_text.splitlines():
            if line.strip():
                payloads.append(json.loads(line))
        return parse_ocr_items(payloads)

    def _recognize_local(self, png: bytes) -> list[OcrItem]:
        if not self.local_url:
            raise ValueError("OCR_LOCAL_URL is required for local OCR.")
        response = self._request(
            "post",
            self.local_url,
            files={"file": ("screenshot.png", png, "image/png")},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError("Local OCR response is not valid JSON.") from error
        return parse_local_ocr_items(payload)
