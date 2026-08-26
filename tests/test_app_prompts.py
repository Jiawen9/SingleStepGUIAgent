from __future__ import annotations

import unittest

from contracts import ActionSelection
from engines.validation import build_action_specs, validate_action
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
        self.assertIn('"action":"reject"', prompt)
        self.assertNotIn("player_pause", prompt)
        self.assertNotIn("## 页面专用动作", prompt)

    def test_netease_cloudmusic_package_adds_search_action_only(self):
        package = "com.netease.cloudmusic.iot"
        app_prompt = load_app_prompt(package)
        self.assertIsNotNone(app_prompt)
        self.assertEqual(app_prompt.app_id, "netease_cloudmusic")
        self.assertEqual(app_prompt.action_names, frozenset({"player_search"}))
        prompt = build_system_prompt(package)
        self.assertIn("## 页面专用动作", prompt)
        self.assertIn('{"action":"player_search","query":"搜索词"}', prompt)
        self.assertNotIn("cloudmusic_", prompt)
        names = {
            spec.name
            for spec in build_action_specs(1000, 1000, app_prompt.action_names)
        }
        self.assertEqual(
            names,
            {"click", "swipe", "type", "reject", "player_search"},
        )
        specs = build_action_specs(1000, 1000, app_prompt.action_names)
        validate_action(
            ActionSelection("player_search", {"query": "周杰伦"}),
            specs,
            1920,
            1200,
        )
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection("player_search", {"query": ""}),
                specs,
                1920,
                1200,
            )

    def test_ximalaya_package_adds_declared_actions(self):
        package = "com.ximalayaos.pad"
        app_prompt = load_app_prompt(package)
        self.assertIsNotNone(app_prompt)
        self.assertEqual(app_prompt.app_id, "ximalaya")
        expected_actions = {
            "player_search",
            "player_set_playback_speed",
            "player_set_sleep_timer",
        }
        self.assertEqual(app_prompt.action_names, frozenset(expected_actions))
        prompt = build_system_prompt(package)
        self.assertIn('{"action":"player_search","query":"搜索词"}', prompt)
        self.assertIn('"speed":"目标倍速"', prompt)
        self.assertIn('"minutes":30', prompt)
        self.assertNotIn("cloudmusic_", prompt)
        specs = build_action_specs(1920, 1200, app_prompt.action_names)
        self.assertEqual(
            {spec.name for spec in specs},
            {"click", "swipe", "type", "reject", *expected_actions},
        )
        validate_action(ActionSelection("player_search", {"query": "三体"}), specs, 1920, 1200)
        for speed in ("0.5x", "1.0x", "1.5x", "2.0x", "2.5x", "3.0x"):
            validate_action(
                ActionSelection("player_set_playback_speed", {"speed": speed}),
                specs, 1920, 1200,
            )
        for minutes in (15, 30, 60, 90):
            validate_action(
                ActionSelection("player_set_sleep_timer", {"minutes": minutes}),
                specs, 1920, 1200,
            )
        for action in (
            ActionSelection("player_set_playback_speed", {"speed": "1.25x"}),
            ActionSelection("player_set_sleep_timer", {"minutes": 45}),
        ):
            with self.subTest(action=action.name), self.assertRaises(ValueError):
                validate_action(action, specs, 1920, 1200)

    def test_douyin_package_adds_declared_actions(self):
        package = "com.ss.android.ugc.aweme"
        app_prompt = load_app_prompt(package)
        self.assertIsNotNone(app_prompt)
        self.assertEqual(app_prompt.app_id, "douyin")
        expected_actions = {
            "player_search",
            "player_set_playback_speed",
            "player_pause",
            "player_next_episode",
        }
        self.assertEqual(app_prompt.action_names, frozenset(expected_actions))
        prompt = build_system_prompt(package)
        self.assertIn('{"action":"player_search","query":"搜索词"}', prompt)
        self.assertIn('"speed":"目标倍速"', prompt)
        self.assertIn('{"action":"player_pause"}', prompt)
        self.assertIn('{"action":"player_next_episode"}', prompt)
        self.assertNotIn("ximalaya_", prompt)
        specs = build_action_specs(1920, 1200, app_prompt.action_names)
        self.assertEqual(
            {spec.name for spec in specs},
            {"click", "swipe", "type", "reject", *expected_actions},
        )
        validate_action(ActionSelection("player_search", {"query": "美食"}), specs, 1920, 1200)
        for speed in ("0.5x", "1.0x", "1.25x", "1.5x"):
            validate_action(
                ActionSelection("player_set_playback_speed", {"speed": speed}),
                specs, 1920, 1200,
            )
        validate_action(ActionSelection("player_pause", {}), specs, 1920, 1200)
        validate_action(ActionSelection("player_next_episode", {}), specs, 1920, 1200)
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection("player_set_playback_speed", {"speed": "2.0x"}),
                specs, 1920, 1200,
            )

    def test_tencent_video_package_adds_declared_actions(self):
        package = "com.tencent.qqlive.audiobox"
        app_prompt = load_app_prompt(package)
        self.assertIsNotNone(app_prompt)
        self.assertEqual(app_prompt.app_id, "tencent_video")
        expected_actions = {
            "player_search",
            "player_set_playback_speed",
            "player_pause",
            "player_resume",
            "player_previous_episode",
            "player_next_episode",
        }
        self.assertEqual(app_prompt.action_names, frozenset(expected_actions))
        prompt = build_system_prompt(package)
        self.assertIn('{"action":"player_search","query":"搜索词"}', prompt)
        self.assertIn('"speed":"目标倍速"', prompt)
        self.assertIn('{"action":"player_pause"}', prompt)
        self.assertIn('{"action":"player_resume"}', prompt)
        self.assertIn('{"action":"player_previous_episode"}', prompt)
        self.assertIn('{"action":"player_next_episode"}', prompt)
        self.assertNotIn("douyin_", prompt)
        specs = build_action_specs(1920, 1200, app_prompt.action_names)
        self.assertEqual(
            {spec.name for spec in specs},
            {"click", "swipe", "type", "reject", *expected_actions},
        )
        validate_action(
            ActionSelection("player_search", {"query": "庆余年"}),
            specs, 1920, 1200,
        )
        for speed in ("0.5x", "0.75x", "1.0x", "1.25x", "1.5x"):
            validate_action(
                ActionSelection("player_set_playback_speed", {"speed": speed}),
                specs, 1920, 1200,
            )
        for name in (
            "player_pause", "player_resume",
            "player_previous_episode", "player_next_episode",
        ):
            validate_action(ActionSelection(name, {}), specs, 1920, 1200)
        with self.assertRaises(ValueError):
            validate_action(
                ActionSelection("player_set_playback_speed", {"speed": "2.0x"}),
                specs, 1920, 1200,
            )

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
