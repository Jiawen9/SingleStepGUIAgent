"""Parse the shared click intent used by deterministic GUI engines."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ClickIntent:
    """A click instruction with noun-like target candidates retained."""

    target_text: str
    source_text: str
    target_candidates: tuple[str, ...] = ()


# These are interaction words, not target words. They may occur after polite
# prefixes ("请帮我") or after the target ("爱奇艺点开").
_CLICK_VERB = re.compile(
    r"点击一下|点按一下|点一下|按一下|点开|选中|选择|选集|点击|点按|按下|"
    r"打开|进入|查看|浏览|播放|收听|搜索|筛选|预约|使用|退出|取消|关注|回关|"
    r"继续|进行|设置|看|按(?!钮)|点"
)
_LEADING_FILLERS = re.compile(
    r"^(?:(?:请|麻烦|拜托|帮我|帮忙|给我|替我|在页面上|在页面中|"
    r"在界面上|在界面中|页面上|页面中|界面上|界面中|一下|一下一下|一下子)\s*)+"
)
_TRAILING_FILLERS = re.compile(
    r"(?:\s*(?:一下|就好|就行|吧|呗|呢|可以吗|好吗))+$"
)
_CONTROL_SUFFIXES = (
    "按钮",
    "控件",
    "入口",
    "图标",
    "选项",
    "菜单项",
    "标签",
    "文字",
    "卡片",
)
_QUOTE_CHARACTERS = "“”‘’「」『』\"'"
_NAMED_SUBJECT = re.compile(
    r"(?:名为|叫做|叫|名称是)\s*(.+?)\s*的(?:按钮|控件|入口|图标|选项|菜单项|卡片)"
)
_POSSESSIVE_CONTROL = re.compile(
    r"(.+?)的(?:按钮|控件|入口|图标|选项|菜单项|卡片)$"
)
_COMPOUND_MARKERS = re.compile(
    r"(?:并且|然后|之后|以后|同时|并|和|以及|接着|再)"
)
_WRAPPING_QUOTES = {
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ('"', '"'),
    ("'", "'"),
}
_DIRECT_VISIBLE_TARGETS = frozenset({"立即续费", "10分钟新闻早餐"})


def clean_ui_text(value: str) -> str:
    """Normalize text for exact UITree matching without fuzzy inference."""
    normalized = unicodedata.normalize("NFKC", value or "")
    return " ".join(normalized.split()).strip()


def _strip_wrapping_quotes(value: str) -> str:
    value = clean_ui_text(value)
    changed = True
    while changed and len(value) >= 2:
        changed = False
        for left, right in _WRAPPING_QUOTES:
            if value.startswith(left) and value.endswith(right):
                value = clean_ui_text(value[1:-1])
                changed = True
                break
    return value


def _strip_punctuation(value: str) -> str:
    value = clean_ui_text(value)
    return value.strip(" \t\r\n，。！？；：、,.!?;:（）()[]【】")


def _remove_quote_characters(value: str) -> str:
    return clean_ui_text(value).translate(
        {ord(character): None for character in _QUOTE_CHARACTERS}
    )


def _candidate_variants(value: str) -> tuple[str, ...]:
    """Return full target text first, then safe control-suffix variants."""
    value = _strip_punctuation(value)
    value = _remove_quote_characters(_strip_wrapping_quotes(value))
    value = _strip_punctuation(value)
    value = _TRAILING_FILLERS.sub("", value)
    value = _strip_wrapping_quotes(_strip_punctuation(value))
    if not value:
        return ()
    base_value = value
    named_match = _NAMED_SUBJECT.search(value)
    if named_match is not None:
        base_value = _remove_quote_characters(
            _strip_wrapping_quotes(_strip_punctuation(named_match.group(1)))
        )
    possessive_match = _POSSESSIVE_CONTROL.fullmatch(value)
    if possessive_match is not None and base_value == value:
        base_value = _remove_quote_characters(
            _strip_wrapping_quotes(_strip_punctuation(possessive_match.group(1)))
        )
    for suffix in _CONTROL_SUFFIXES:
        if (
            value.endswith(suffix)
            and len(value) > len(suffix)
            and base_value == value
        ):
            base_value = _remove_quote_characters(
                _strip_wrapping_quotes(
                    _strip_punctuation(value[: -len(suffix)].rstrip())
                )
            )
            break
    # Prefer the noun-like subject. The full phrase remains a fallback for a
    # UITree whose text includes the control-type suffix.
    candidates = [base_value, _remove_quote_characters(value)]
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _extract_target_candidates(source: str, verb: re.Match[str]) -> tuple[str, ...]:
    before = source[: verb.start()]
    after = source[verb.end() :]

    # "点击一下爱奇艺" and "请帮我点击爱奇艺按钮".
    after = _LEADING_FILLERS.sub("", after)
    after_candidates = _candidate_variants(after)
    if after_candidates:
        return after_candidates

    # Also accept natural wording such as "爱奇艺点开" or "请帮我把爱奇艺打开".
    before = _LEADING_FILLERS.sub("", before)
    before = re.sub(r"^.*?(?:把|将)\s*", "", before)
    return _candidate_variants(before)


def parse_click_instruction(instruction: str) -> ClickIntent | None:
    """Extract noun-like target candidates from varied click wording.

    This remains deliberately conservative: compound instructions are left to
    VLA, while the returned candidates are still matched exactly against the
    UITree ``text`` attribute.
    """
    source = clean_ui_text(instruction)
    if not source:
        return None
    if _COMPOUND_MARKERS.search(source):
        return None
    matches = list(_CLICK_VERB.finditer(source))
    if not matches:
        if source in _DIRECT_VISIBLE_TARGETS:
            return ClickIntent(source, source, (source,))
        return None
    if len(matches) > 1:
        between = source[matches[0].end() : matches[1].start()]
        if "后" in between:
            return None
    # A visible label may itself contain another interaction word, such as
    # “点击全部播放” or “点击关注”. Only the first verb describes the requested
    # interaction; the remaining words belong to the target label.
    candidates = _extract_target_candidates(source, matches[0])
    if not candidates:
        # Standalone action labels such as “取消” and colloquial “筛选一下”
        # still refer to the visible control named by the verb itself.
        candidates = _candidate_variants(matches[0].group())
    if not candidates:
        return None
    # A few controls are literally labelled with a leading interaction word.
    # Preserve those exact labels without broadly adding complete instructions
    # such as “请帮我点击一下设置” to the target candidates.
    if source.startswith(("立即播放", "继续播放", "取消下载")):
        candidates = tuple(dict.fromkeys((*candidates, source)))
    return ClickIntent(
        target_text=candidates[0],
        source_text=source,
        target_candidates=candidates,
    )
