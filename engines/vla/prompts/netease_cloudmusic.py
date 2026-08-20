"""Prompt actions available only while NetEase Cloud Music IoT is foreground."""

APP_ID = "netease_cloudmusic"

ACTION_NAMES = ("player_search",)

PROMPT = """## 页面专用动作
- {"action":"player_search","query":"搜索词"}: 搜索歌曲、歌手、专辑、歌单或播客。query 只填写实际搜索内容，不包含“搜索、查找、播放、帮我找”等意图词。"""
