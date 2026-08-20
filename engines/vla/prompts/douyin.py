"""Prompt actions available only while Douyin is foreground."""

APP_ID = "douyin"

ACTION_NAMES = (
    "player_search",
    "player_set_playback_speed",
    "player_pause",
    "player_next_episode",
)

PROMPT = """## 播放器专用动作
- {"action":"player_search","query":"搜索词"}: 搜索视频、用户、话题或内容。query 只填写实际搜索内容，不包含“搜索、查找、播放、帮我找”等意图词。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 设置当前视频的播放倍速。speed 只能是 "0.5x"、"1.0x"、"1.25x"、"1.5x"；用户要求“正常”时输出 "1.0x"。
- {"action":"player_pause"}: 暂停当前正在播放的视频。
- {"action":"player_next_episode"}: 播放当前短剧或连续内容的下一集；下一集入口或连续剧集信息不可见时选择 reject。"""
