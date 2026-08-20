"""Prompt actions available only while Ximalaya Pad is foreground."""

APP_ID = "ximalaya"

ACTION_NAMES = (
    "player_search",
    "player_set_playback_speed",
    "player_set_sleep_timer",
)

PROMPT = """## 播放器专用动作
- {"action":"player_search","query":"搜索词"}: 搜索节目、专辑、主播或声音。query 只填写实际搜索内容，不包含“搜索、查找、播放、帮我找”等意图词。
- {"action":"player_set_playback_speed","speed":"目标倍速"}: 设置当前内容的播放倍速。speed 只能是 "0.5x"、"1.0x"、"1.5x"、"2.0x"、"2.5x"、"3.0x"。
- {"action":"player_set_sleep_timer","minutes":30}: 设置定时暂停播放。minutes 只能是 15、30、60、90，单位为分钟。"""
