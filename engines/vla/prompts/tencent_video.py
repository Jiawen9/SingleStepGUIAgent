"""Prompt actions available only while Tencent Video AudioBox is foreground."""

APP_ID = "tencent_video"

ACTION_NAMES = (
    "player_search",
    "player_set_playback_speed",
    "player_pause",
    "player_resume",
    "player_previous_episode",
    "player_next_episode",
)

PROMPT = """## 播放器专用动作
- {"action":"player_search","query":"搜索词"}: 在未出现输入键盘时执行。query 只填写实际搜索词。。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 设置当前视频的播放倍速。倍数规范为“数字+小写x”，整数倍保留一位小数，例如 2倍为 "2.0x"。
- {"action":"player_pause"}: 暂停当前正在播放的视频。
- {"action":"player_resume"}: 继续播放当前已暂停的视频。
- {"action":"player_previous_episode"}: 播放当前视频的上一集。
- {"action":"player_next_episode"}: 播放当前视频的下一集。"""
