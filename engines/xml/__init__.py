"""Deterministic XML action engine."""

from .engine import XmlEngine
from engines.instruction import ClickIntent, clean_ui_text, parse_click_instruction
from .matcher import ClickTarget, resolve_click_match
from .router import RuleRoute, click_action_for_target, route_click_instruction

__all__ = [
    "ClickIntent",
    "ClickTarget",
    "RuleRoute",
    "XmlEngine",
    "clean_ui_text",
    "click_action_for_target",
    "parse_click_instruction",
    "resolve_click_match",
    "route_click_instruction",
]
