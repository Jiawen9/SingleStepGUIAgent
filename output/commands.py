"""Pure conversion from validated standard actions to execution commands."""

from __future__ import annotations

from execution.executor import vla_coordinate_to_pixel
from contracts import ActionSelection, ExecutionCommand, ScreenSnapshot


IQIYI_ATOMIC_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
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

APP_ATOMIC_TOOLS = {
    "iqiyi": IQIYI_ATOMIC_TOOLS,
    "netease_cloudmusic": {},
    "ximalaya": {},
    "douyin": {},
    # Tencent Video deliberately has no implementation yet. Its generic
    # player_* actions must never fall through to iQIYI's same-named tools.
    "tencent_video": {},
}


class CommandBuilder:
    def __init__(self, *, allow_unmapped: bool = False):
        self.allow_unmapped = allow_unmapped

    def build(
        self,
        action: ActionSelection,
        snapshot: ScreenSnapshot,
        app_id: str = "iqiyi",
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
        tool = APP_ATOMIC_TOOLS.get(app_id, {}).get(name)
        if tool is None:
            if self.allow_unmapped:
                return ExecutionCommand(
                    "evaluation",
                    "unmapped_action",
                    dict(arguments),
                    action,
                )
            raise ValueError(f"No command mapping is registered for action: {name}")
        module_name, templates = tool
        rendered = tuple(template.format(**arguments) for template in templates)
        return ExecutionCommand(
            "atomic_tool",
            module_name,
            {"argv": list(rendered)},
            action,
        )
