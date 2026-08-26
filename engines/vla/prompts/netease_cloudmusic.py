"""Prompt actions available only while NetEase Cloud Music IoT is foreground."""

APP_ID = "netease_cloudmusic"

ACTION_NAMES = ("player_search",)

PROMPT = """## 页面专用动作
- {"action":"player_search","query":"搜索词"}: 在未出现输入键盘时执行。query 只填写实际搜索词。"""
