"""Deterministic XML action engine."""

from .engine import XmlEngine
from .instruction import ClickIntent, clean_ui_text, parse_click_instruction
from .matcher import ClickTarget, match_click_intent, resolve_click_match
from .router import RuleRoute, click_action_for_target, route_click_instruction

__all__ = [
    "ClickIntent",
    "ClickTarget",
    "RuleRoute",
    "XmlEngine",
    "clean_ui_text",
    "click_action_for_target",
    "match_click_intent",
    "parse_click_instruction",
    "resolve_click_match",
    "route_click_instruction",
]
