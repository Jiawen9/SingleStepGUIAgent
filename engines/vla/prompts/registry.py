"""Map a foreground Android package to its App-specific action prompt."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class AppPrompt:
    app_id: str
    package_name: str
    prompt: str
    action_names: frozenset[str]


# Exact package names remain explicit so a similarly named third-party App
# cannot accidentally receive privileged App-specific actions.
_PACKAGE_MODULES = {
    "com.qiyi.video": ".iqiyi",
    "com.qiyi.video.pad": ".iqiyi",
    "com.qiyi.video.speaker": ".iqiyi",
    "com.netease.cloudmusic.iot": ".netease_cloudmusic",
    "com.ximalayaos.pad": ".ximalaya",
    "com.ss.android.ugc.aweme": ".douyin",
    "com.tencent.qqlive.audiobox": ".tencent_video",
}


def load_app_prompt(package_name: str | None) -> AppPrompt | None:
    """Load the prompt fragment registered for one exact foreground package."""
    normalized = (package_name or "").strip()
    module_name = _PACKAGE_MODULES.get(normalized)
    if module_name is None:
        return None
    module = import_module(module_name, package=__package__)
    return AppPrompt(
        app_id=module.APP_ID,
        package_name=normalized,
        prompt=module.PROMPT.strip(),
        action_names=frozenset(module.ACTION_NAMES),
    )
