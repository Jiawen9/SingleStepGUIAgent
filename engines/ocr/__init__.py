"""PaddleOCR engine and HTTP client adapters."""

from .client import OcrClient, OcrItem
from .engine import OcrEngine

__all__ = ["OcrClient", "OcrEngine", "OcrItem"]
