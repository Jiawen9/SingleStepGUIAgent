"""Serialize internal actions to the public prompt JSON protocol."""

from __future__ import annotations

from typing import Any

from contracts import ActionSelection


def action_as_prompt_object(selection: ActionSelection) -> dict[str, Any]:
    if selection.prompt_action is not None:
        return dict(selection.prompt_action)
    name = selection.name
    arguments = selection.arguments
    if name == "click":
        return {"action": "click", "coordinate": [arguments["x"], arguments["y"]]}
    if name == "type":
        return {"action": "type", "text": arguments["text"]}
    if name == "swipe":
        x1, y1 = arguments["x1"], arguments["y1"]
        dx = arguments["x2"] - x1
        dy = arguments["y2"] - y1
        if abs(dx) >= abs(dy):
            direction = "right" if dx > 0 else "left"
            length = abs(dx)
        else:
            direction = "down" if dy > 0 else "up"
            length = abs(dy)
        distance = "short" if length <= 200 else "medium" if length <= 400 else "long"
        return {"action": "swipe", "start_coordinate": [x1, y1], "direction": direction, "distance": distance}
    if name == "reject":
        return {"action": "reject"}
    return {"action": name, **arguments}
