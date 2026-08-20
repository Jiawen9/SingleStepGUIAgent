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
- {"action":"player_search","query":"搜索词"}: 搜索电视剧、电影、综艺、动漫或其他视频。query 只填写实际搜索内容，不包含“搜索、查找、播放、帮我找”等意图词。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 设置当前视频的播放倍速。speed 只能是 "0.5x"、"0.75x"、"1.0x"、"1.25x"、"1.5x"；用户说“0.5倍”时输出 "0.5x"。
- {"action":"player_pause"}: 暂停当前正在播放的视频。
- {"action":"player_resume"}: 继续播放当前已暂停的视频。
- {"action":"player_previous_episode"}: 播放当前视频的上一集；上一集入口或剧集信息不可见时选择 reject。
- {"action":"player_next_episode"}: 播放当前视频的下一集；下一集入口或剧集信息不可见时选择 reject。"""
