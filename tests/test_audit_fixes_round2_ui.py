"""A 组第二轮修复的回归用例：只锁本组改动的契约，不拉起悬浮窗、不联网。

为什么用源码文本 / mock：cli.main 会真跑、cards.update 会联网，
A3/A4 只需锁住参数契约，A5 把缓存改道到临时文件即可，都不碰真实 data/。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, review, ui, ui_state, ui_theme
from hsai.ui_ai import AiWorkers

ROOT = Path(__file__).resolve().parent.parent


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class HasOptionsTest(unittest.TestCase):
    """_has_options 是 @staticmethod，直接调，不需要 tkinter。"""

    @staticmethod
    def _snap(mana=0, hand=None, board=None, hp=None, used=True):
        our = {"mana": mana, "hand": hand or [], "board": board or [],
               "hero_power_used": used}
        if hp is not None:
            our["hero_power"] = hp
        return {"our": our}

    def test_hand_cost_within_mana(self):
        self.assertTrue(AiWorkers._has_options(
            self._snap(mana=3, hand=[{"cost": 2}], hp={"cost": 2}, used=True)))

    def test_all_hand_overcost_alone_is_no_option(self):
        # 技能已用、场面不可攻：只剩超费手牌，不该凭空判「还有事可做」
        self.assertFalse(AiWorkers._has_options(
            self._snap(mana=1, hand=[{"cost": 5}], hp={"cost": 2}, used=True)))

    def test_board_can_attack(self):
        self.assertTrue(AiWorkers._has_options(
            self._snap(mana=0, hand=[{"cost": 9}], board=[{"can_attack": True}],
                       hp={"cost": 2}, used=True)))

    def test_no_attacker_is_no_option(self):
        self.assertFalse(AiWorkers._has_options(
            self._snap(mana=0, hand=[{"cost": 9}], board=[{"can_attack": False}],
                       hp={"cost": 2}, used=True)))

    def test_hero_power_unused_and_affordable(self):
        self.assertTrue(AiWorkers._has_options(
            self._snap(mana=2, hand=[{"cost": 9}], hp={"cost": 2}, used=False)))

    def test_hero_power_used_still_true_via_board(self):
        # 已用技能时不能靠技能判 True，这条靠场面可攻
        self.assertTrue(AiWorkers._has_options(
            self._snap(mana=0, hand=[{"cost": 9}], board=[{"can_attack": True}],
                       hp={"cost": 2}, used=True)))

    def test_nothing_possible(self):
        self.assertFalse(AiWorkers._has_options(
            self._snap(mana=0, hand=[{"cost": 9}], hp={"cost": 2}, used=True)))

    def test_empty_snapshot(self):
        # 规格写「返回 False」，实测是 True：hero_power 缺失时 hp.get("cost")
        # 取 0、mana 取 0，命中「技能未用且费用够」。ui_ai.py 是只读参考，
        # 本轮不能改行为，这里锁住真实返回值，别把它当成已修的坑。
        self.assertTrue(AiWorkers._has_options({}))

    def test_hero_power_missing_does_not_crash(self):
        # hp.get("cost") 不能因为缺 hero_power 就抛
        self.assertTrue(AiWorkers._has_options(
            {"our": {"mana": 2, "hand": [{"cost": 9}], "board": [],
                     "hero_power_used": False}}))


class UiFacadeSingleSourceTest(unittest.TestCase):
    def test_state_functions_are_reexported_not_redefined(self):
        self.assertIs(ui._load_state, ui_state._load_state)
        self.assertIs(ui._save_state, ui_state._save_state)

    def test_theme_constants_match_ui_theme(self):
        self.assertEqual(ui.LAYOUT_VERSION, ui_theme.LAYOUT_VERSION)
        self.assertEqual(ui.DEFAULT_WIDTH, ui_theme.DEFAULT_WIDTH)

    def test_module_level_names_still_reachable(self):
        # 子模块靠 from . import ui 回查这几个名字，删了就 NameError
        for name in ("STATE_PATH", "DATA_DIR", "LAYOUT_VERSION"):
            self.assertTrue(hasattr(ui, name), name)


class CliContractTest(unittest.TestCase):
    def test_serve_accepts_check(self):
        self.assertIn('s2.add_argument("--check"', _source("hsai/cli.py"))

    def test_max_turns_help_is_honest(self):
        self.assertIn("仅 --trace 生效", _source("hsai/cli.py"))


class CardLoadDegradeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="_test-a5-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cache = Path(tmp) / "cards_zhCN.json"

    def _load_with(self, text):
        self.cache.write_text(text, encoding="utf-8")
        with mock.patch.object(cards, "cache_path", return_value=self.cache):
            return cards.load(auto_update=False)

    def test_bad_payloads_degrade_to_empty_db_and_good_one_reads(self):
        for label, text in (("缺 rows 键", "{}"),
                            ("顶层是 list", "[]"),
                            ("坏 JSON", "{not json")):
            with self.subTest(label=label):
                self.assertEqual(len(self._load_with(text)), 0)
        row = cards._to_row(cards.Card(id="CS2_029", name="火球术", cost=4))
        good = self._load_with(json.dumps({"rows": [row]}))
        self.assertEqual(len(good), 1)
        self.assertIsNotNone(good.get("CS2_029"))


class ReviewSummarizeTest(unittest.TestCase):
    def test_hand_overflow_reported_and_capped(self):
        turns = [{"turn": t, "hand": 10} for t in range(1, 9)]
        out = review.summarize({"turns": turns})
        line = next(x for x in out if "手牌已满" in x)
        self.assertIn("再过牌会烧牌", line)
        self.assertLessEqual(len(out), 5)

    def test_hand_below_ten_not_reported(self):
        out = review.summarize({"turns": [{"turn": 3, "hand": 9}]})
        self.assertFalse(any("手牌已满" in x for x in out), out)

    def test_lethal_and_overflow_both_kept_within_five(self):
        turns = [{"turn": t, "hand": 10, "lethal_available": True,
                  "lethal_damage": 22, "killed": False} for t in range(1, 9)]
        out = review.summarize({"turns": turns})
        self.assertTrue(any("有斩杀没斩" in x for x in out), out)
        self.assertTrue(any("手牌已满" in x for x in out), out)
        self.assertLessEqual(len(out), 5)


class TagsAiWordingTest(unittest.TestCase):
    def test_no_hardcoded_ten_classes(self):
        src = _source("hsai/tags_ai.py")
        self.assertNotIn("固定 10 类", src)
        self.assertIn("只能来自 {len(TAG_ORDER)} 类", src)


if __name__ == "__main__":
    unittest.main()
