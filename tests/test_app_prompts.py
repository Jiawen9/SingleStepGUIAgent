from __future__ import annotations

import unittest

from engines.validation import build_action_specs
from engines.vla.client import build_system_prompt
from engines.vla.prompts import load_app_prompt


class RuntimeAppPromptTests(unittest.TestCase):
    def test_iqiyi_package_adds_its_actions(self):
        prompt = build_system_prompt("com.qiyi.video.pad")
        self.assertIn("## 播放器专用动作", prompt)
        self.assertIn('{"action":"player_pause"}', prompt)
        self.assertIn('{"action":"player_search","query":"搜索词"}', prompt)
        self.assertIn('{"action":"player_set_quality","quality":"目标清晰度"}', prompt)
        self.assertIn('{"action":"player_set_playback_speed","speed":"目标倍速"}', prompt)
        self.assertNotIn("iqiyi_toggle_screen", prompt)
        self.assertNotIn("iqiyi_resume", prompt)
        self.assertNotIn("iqiyi_fast_forward", prompt)
        self.assertNotIn("iqiyi_rewind", prompt)
        self.assertNotIn("小屏", prompt)

    def test_iqiyi_set_top_box_package_adds_player_actions(self):
        app_prompt = load_app_prompt("com.qiyi.video.speaker")
        self.assertIsNotNone(app_prompt)
        self.assertIn("player_pause", app_prompt.action_names)
        self.assertIn(
            '{"action":"player_pause"}',
            build_system_prompt("com.qiyi.video.speaker"),
        )

    def test_unknown_package_uses_only_common_actions(self):
        prompt = build_system_prompt("com.example.other")
        self.assertIn('{"action":"click","coordinate":[x,y]}', prompt)
        self.assertIn('"action_id":"reject"', prompt)
        self.assertNotIn("player_pause", prompt)
        self.assertNotIn("## 页面专用动作", prompt)

    def test_local_catalog_is_restricted_to_detected_app(self):
        app_prompt = load_app_prompt("com.qiyi.video")
        self.assertIsNotNone(app_prompt)
        iqiyi_names = {
            spec.name
            for spec in build_action_specs(
                1000,
                1000,
                app_prompt.action_names,
            )
        }
        generic_names = {
            spec.name
            for spec in build_action_specs(1000, 1000, frozenset())
        }
        self.assertIn("player_pause", iqiyi_names)
        self.assertNotIn("iqiyi_toggle_screen", iqiyi_names)
        self.assertNotIn("iqiyi_resume", iqiyi_names)
        self.assertNotIn("iqiyi_fast_forward", iqiyi_names)
        self.assertNotIn("iqiyi_rewind", iqiyi_names)
        self.assertEqual(generic_names, {"click", "swipe", "type", "reject"})


if __name__ == "__main__":
    unittest.main()
