"""Route a user instruction through the deterministic UITree click path."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

from contracts import ActionSelection
from engines.instruction import ClickIntent, parse_click_instruction
from .matcher import ClickTarget, resolve_click_match


@dataclass(frozen=True)
class RuleRoute:
    intent: ClickIntent | None
    target: ClickTarget | None
    reason: str
    candidate_count: int = 0

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"reason": self.reason}
        if self.intent is not None:
            payload["intent"] = "click_text"
            payload["target_text"] = self.intent.target_text
            if self.intent.target_candidates:
                payload["target_candidates"] = list(self.intent.target_candidates)
        if self.target is not None:
            payload["matched_text"] = self.target.matched_text
            payload["bounds"] = (
                "[%d,%d][%d,%d]" % self.target.bounds
            )
            payload["pixel_center"] = {
                "x": self.target.center[0],
                "y": self.target.center[1],
            }
        if self.candidate_count:
            payload["candidate_count"] = self.candidate_count
        return payload


def route_click_instruction(
    instruction: str, root: ElementTree.Element
) -> RuleRoute:
    intent = parse_click_instruction(instruction)
    if intent is None:
        return RuleRoute(None, None, "not_a_simple_click_instruction")
    result = resolve_click_match(root, intent)
    return RuleRoute(
        intent,
        result.target,
        result.reason,
        result.candidate_count,
    )


def click_action_for_target(
    target: ClickTarget, *, width: int, height: int
) -> ActionSelection:
    """Represent a pixel target in the shared click action coordinate space."""
    x, y = target.center
    return ActionSelection(
        "click",
        {
            # The executor maps the shared click coordinates back with
            # round(value * (size - 1) / 1000), so this inverse keeps the
            # direct UITree tap within one pixel of the XML center.
            "x": round(x * 1000 / (width - 1)) if width > 1 else 0,
            "y": round(y * 1000 / (height - 1)) if height > 1 else 0,
        },
    )
