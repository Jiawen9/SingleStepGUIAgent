from __future__ import annotations

import unittest
from xml.etree import ElementTree

from engines.xml import (
    click_action_for_target,
    clean_ui_text,
    parse_click_instruction,
    route_click_instruction,
)


class UiTreeRuleTests(unittest.TestCase):
    def test_click_instruction_removes_only_leading_verb(self):
        self.assertEqual(
            parse_click_instruction("请点击‘播放列表’").target_text,
            "播放列表",
        )
        self.assertEqual(
            parse_click_instruction("点击取消下载").target_text,
            "取消下载",
        )
        self.assertEqual(parse_click_instruction("搜索三国演义").target_text, "三国演义")
        self.assertIsNone(parse_click_instruction("打开后点击播放"))

    def test_task_style_verbs_produce_noun_first_and_full_text_fallback(self):
        cases = {
            "查看收藏": "收藏",
            "播放大力水手": "大力水手",
            "选集“2026-08-07期”": "2026-08-07期",
            "搜索海绵宝宝": "海绵宝宝",
            "预约731真相": "731真相",
            "选择迷幻电音": "迷幻电音",
            "收听诡秘之主": "诡秘之主",
            "使用AI搜": "AI搜",
            "进入个人中心": "个人中心",
            "浏览同城": "同城",
            "回关白眼中山琅": "白眼中山琅",
            "看电视": "电视",
            "进行屏保设置": "屏保设置",
            "筛选一下": "筛选",
            "取消": "取消",
        }
        for instruction, target in cases.items():
            with self.subTest(instruction=instruction):
                intent = parse_click_instruction(instruction)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.target_text, target)

    def test_visible_action_label_remains_an_exact_fallback(self):
        for instruction in (
            "立即播放", "继续播放", "取消下载", "立即续费", "10分钟新闻早餐",
        ):
            with self.subTest(instruction=instruction):
                intent = parse_click_instruction(instruction)
                self.assertIsNotNone(intent)
                self.assertIn(instruction, intent.target_candidates)

    def test_click_instruction_extracts_target_from_varied_wording(self):
        cases = {
            "请帮我点击一下‘爱奇艺’按钮": ("爱奇艺", "爱奇艺按钮"),
            "麻烦在页面上点开搜索图标": ("搜索", "搜索图标"),
            "把全屏按钮打开": ("全屏", "全屏按钮"),
            "爱奇艺，点一下": ("爱奇艺",),
            "请按下播放控件": ("播放", "播放控件"),
            "请按爱奇艺": ("爱奇艺",),
            "选择名为设置的按钮": ("设置", "名为设置的按钮"),
            "点击爱奇艺的入口": ("爱奇艺", "爱奇艺的入口"),
        }
        for instruction, expected in cases.items():
            with self.subTest(instruction=instruction):
                intent = parse_click_instruction(instruction)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.target_candidates, expected)

    def test_click_extraction_rejects_compound_instructions(self):
        for instruction in (
            "点击搜索并输入三国演义",
            "打开菜单然后点击设置",
            "点击播放后再点击全屏",
        ):
            with self.subTest(instruction=instruction):
                self.assertIsNone(parse_click_instruction(instruction))

    def test_text_cleaning_normalizes_spaces_and_width(self):
        self.assertEqual(clean_ui_text("  ＡＢＣ　 1\n2 "), "ABC 1 2")

    def test_unique_text_uses_nearest_clickable_ancestor(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node clickable="true" enabled="true" visible-to-user="true"
                    bounds="[100,200][500,600]">
                <node text="爱奇艺" clickable="false" enabled="true"
                      visible-to-user="true" bounds="[180,300][400,380]" />
              </node>
            </hierarchy>
            """
        )

        route = route_click_instruction("点击 爱奇艺", root)

        self.assertIsNotNone(route.target)
        self.assertEqual(route.reason, "unique_text_match")
        self.assertEqual(route.target.center, (300, 400))
        action = click_action_for_target(route.target, width=1000, height=800)
        self.assertEqual(action.arguments, {"x": 300, "y": 501})

    def test_ambiguous_or_non_clickable_text_falls_back(self):
        root = ElementTree.fromstring(
            """
            <hierarchy>
              <node text="爱奇艺" clickable="false" enabled="true"
                    visible-to-user="true" bounds="[0,0][100,100]" />
              <node clickable="true" enabled="true" visible-to-user="true"
                    bounds="[100,0][200,100]">
                <node text="爱奇艺" clickable="false" enabled="true"
                      visible-to-user="true" bounds="[110,10][190,90]" />
              </node>
              <node clickable="true" enabled="true" visible-to-user="true"
                    bounds="[300,0][400,100]">
                <node text="爱奇艺" clickable="false" enabled="true"
                      visible-to-user="true" bounds="[310,10][390,90]" />
              </node>
            </hierarchy>
            """
        )

        route = route_click_instruction("点击爱奇艺", root)

        self.assertIsNone(route.target)
        self.assertEqual(route.reason, "ambiguous_text_match")
        self.assertEqual(route.candidate_count, 2)


if __name__ == "__main__":
    unittest.main()
