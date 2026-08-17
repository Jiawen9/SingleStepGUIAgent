"""Exact text matching and clickable-ancestor resolution for UITree XML."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

from device.xml_hierarchy import (
    bounds,
    center,
    nearest_clickable_ancestor,
    parent_map,
)
from .instruction import ClickIntent, clean_ui_text


@dataclass(frozen=True)
class ClickTarget:
    """A unique clickable target resolved from one UITree text node."""

    matched_text: str
    bounds: tuple[int, int, int, int]
    center: tuple[int, int]


@dataclass(frozen=True)
class MatchResult:
    target: ClickTarget | None
    reason: str
    candidate_count: int


def _match_key(value: str) -> str:
    return clean_ui_text(value).casefold()


def _resolve_one_target(
    root: ElementTree.Element, target_text: str
) -> MatchResult:
    """Resolve exact text nodes to unique clickable ancestors.

    Only the UITree ``text`` attribute participates. Content descriptions,
    resource IDs, class names and spatial guesses are intentionally excluded.
    """
    expected = _match_key(target_text)
    parents = parent_map(root)
    candidates: dict[tuple[int, int, int, int], ClickTarget] = {}

    for node in root.iter("node"):
        if node.get("visible-to-user") == "false":
            continue
        if _match_key(node.get("text", "")) != expected:
            continue
        clickable = nearest_clickable_ancestor(node, parents)
        if clickable is None:
            continue
        try:
            target_bounds = bounds(clickable)
            target_center = center(clickable)
        except RuntimeError:
            continue
        candidates[target_bounds] = ClickTarget(
            matched_text=clean_ui_text(node.get("text", "")),
            bounds=target_bounds,
            center=target_center,
        )

    if not candidates:
        return MatchResult(None, "no_unique_clickable_text_match", 0)
    if len(candidates) != 1:
        return MatchResult(None, "ambiguous_text_match", len(candidates))
    return MatchResult(next(iter(candidates.values())), "unique_text_match", 1)


def resolve_click_match(root: ElementTree.Element, intent: ClickIntent) -> MatchResult:
    """Try extracted target forms in priority order, using exact text matches."""
    candidates = intent.target_candidates or (intent.target_text,)
    for target_text in candidates:
        result = _resolve_one_target(root, target_text)
        if result.target is not None:
            return result
        if result.reason == "ambiguous_text_match":
            return result
    return MatchResult(None, "no_unique_clickable_text_match", 0)


def match_click_intent(
    root: ElementTree.Element, intent: ClickIntent
) -> ClickTarget | None:
    """Compatibility helper returning only a successfully resolved target."""
    return resolve_click_match(root, intent).target
