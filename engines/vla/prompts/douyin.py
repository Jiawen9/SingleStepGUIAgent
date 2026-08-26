"""Prompt actions available only while Douyin is foreground."""

APP_ID = "douyin"

ACTION_NAMES = (
    "player_search",
    "player_set_playback_speed",
    "player_pause",
    "player_next_episode",
)

PROMPT = """## 播放器专用动作
- {"action":"player_search","query":"搜索词"}: 在未出现输入键盘时执行。query 只填写实际搜索词。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 设置当前视频的播放倍速。倍数规范为“数字+小写x”，整数倍保留一位小数，例如 2倍为 "2.0x"。
- {"action":"player_next_episode"}: 播放当前短剧或连续内容的下一集。"""
