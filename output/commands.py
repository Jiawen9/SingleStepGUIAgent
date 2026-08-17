"""Pure conversion from validated standard actions to execution commands."""

from __future__ import annotations

from execution.executor import vla_coordinate_to_pixel
from contracts import ActionSelection, ExecutionCommand, ScreenSnapshot


ATOMIC_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "player_pause": ("execution.atomic_tools.iqiyi.pause", ()),
    "player_seek_to_start": (
        "execution.atomic_tools.iqiyi.seek_to_start",
        (),
    ),
    "player_previous_episode": (
        "execution.atomic_tools.iqiyi.change_episode",
        ("--direction", "previous"),
    ),
    "player_next_episode": (
        "execution.atomic_tools.iqiyi.change_episode",
        ("--direction", "next"),
    ),
    "player_set_quality": (
        "execution.atomic_tools.iqiyi.set_quality",
        ("--quality", "{quality}"),
    ),
    "player_set_playback_speed": (
        "execution.atomic_tools.iqiyi.set_playback_speed",
        ("--speed", "{speed}"),
    ),
    "player_search": (
        "execution.atomic_tools.iqiyi.search",
        ("--query={query}",),
    ),
}


class CommandBuilder:
    def build(
        self, action: ActionSelection, snapshot: ScreenSnapshot
    ) -> ExecutionCommand:
        name = action.name
        arguments = action.arguments
        if name == "reject":
            return ExecutionCommand("reject", "local", dict(arguments), action)
        if name == "click":
            return ExecutionCommand(
                "adb",
                "tap",
                {
                    "x": vla_coordinate_to_pixel(arguments["x"], snapshot.width),
                    "y": vla_coordinate_to_pixel(arguments["y"], snapshot.height),
                    "vla_x": arguments["x"],
                    "vla_y": arguments["y"],
                },
                action,
            )
        if name == "swipe":
            return ExecutionCommand(
                "adb",
                "swipe",
                {
                    "x1": vla_coordinate_to_pixel(arguments["x1"], snapshot.width),
                    "y1": vla_coordinate_to_pixel(arguments["y1"], snapshot.height),
                    "x2": vla_coordinate_to_pixel(arguments["x2"], snapshot.width),
                    "y2": vla_coordinate_to_pixel(arguments["y2"], snapshot.height),
                },
                action,
            )
        if name == "type":
            return ExecutionCommand("adb", "type", {"text": arguments["text"]}, action)
        tool = ATOMIC_TOOLS.get(name)
        if tool is None:
            raise ValueError(f"No command mapping is registered for action: {name}")
        module_name, templates = tool
        rendered = tuple(template.format(**arguments) for template in templates)
        return ExecutionCommand(
            "atomic_tool",
            module_name,
            {"argv": list(rendered)},
            action,
        )
