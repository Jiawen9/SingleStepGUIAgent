"""Runtime App-specific prompt fragments."""

from .registry import AppPrompt, load_app_prompt

__all__ = ["AppPrompt", "load_app_prompt"]
