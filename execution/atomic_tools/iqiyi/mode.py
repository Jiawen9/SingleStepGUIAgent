"""Shared locator-mode configuration for iQIYI composite actions."""

from __future__ import annotations

import os


MEDIUM_MODE = "medium"
MATCH_MODE = "match"
ACTION_MODES = (MEDIUM_MODE, MATCH_MODE)
MODE_ENVIRONMENT_VARIABLE = "IQIYI_ACTION_MODE"


def normalize_action_mode(value: str | None) -> str:
    """Return one supported mode, defaulting to the existing medium layout."""
    mode = (value or MEDIUM_MODE).strip().lower()
    if mode not in ACTION_MODES:
        raise ValueError(
            f"Unsupported iQIYI action mode: {value!r}; "
            f"expected one of {', '.join(ACTION_MODES)}."
        )
    return mode


def current_action_mode() -> str:
    return normalize_action_mode(os.environ.get(MODE_ENVIRONMENT_VARIABLE))


def uses_semantic_matching() -> bool:
    return current_action_mode() == MATCH_MODE
