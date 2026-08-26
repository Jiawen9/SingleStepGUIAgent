"""Prompt actions available only while an iQIYI package is foreground."""

APP_ID = "iqiyi"

ACTION_NAMES = (
    "player_pause",
    "player_seek_to_start",
    "player_previous_episode",
    "player_next_episode",
    "player_set_quality",
    "player_set_playback_speed",
    "player_search",
)

PROMPT = """## 播放器专用动作
- {"action":"player_pause"}: 暂停当前播放器中正在播放的视频。
- {"action":"player_seek_to_start"}: 将当前视频定位到开头，不改变播放/暂停状态。
- {"action":"player_previous_episode"}: 播放当前视频的上一集。
- {"action":"player_next_episode"}: 播放当前视频的下一集。
- {"action":"player_set_quality","quality":"目标清晰度"}: 切换当前视频的清晰度。720、720P 规范为 "720p"；“智能”规范为 "auto"。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 切换当前视频的播放倍速。倍数规范为“数字+小写x”，整数倍保留一位小数，例如 2倍为 "2.0x"。

## 页面专用动作
- {"action":"player_search","query":"搜索词"}: 在未出现输入键盘时执行。query 只填写实际搜索词。"""
