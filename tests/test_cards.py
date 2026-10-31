"""卡牌库 / 文本效果解析。"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai.model import parse_effect


class CardDBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def test_loaded(self) -> None:
        self.assertGreater(len(self.db), 1000, "卡牌库应有上千张牌（需要联网更新过一次）")

    def test_lookup_by_id_and_name(self) -> None:
        c = self.db.get("CS2_029")
        self.assertIsNotNone(c)
        self.assertEqual(c.name, "火球术")
        self.assertEqual(self.db.by_name_or_id("火球术").card_class, "MAGE")
        # case-insensitive
        self.assertIsNotNone(self.db.get("cs2_029"))

    def test_class_cn(self) -> None:
        self.assertEqual(self.db.get("CS2_029").class_cn, "法师")

    def test_unknown(self) -> None:
        self.assertIsNone(self.db.get("NOT_A_CARD"))
        self.assertEqual(self.db.display_name("NOT_A_CARD"), "NOT_A_CARD")


class EffectParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def test_damage(self) -> None:
        eff = parse_effect(self.db.get("CORE_CS2_029"))
        self.assertEqual(eff.damage, 6)
        self.assertFalse(eff.aoe)

    def test_aoe(self) -> None:
        eff = parse_effect(self.db.get("CORE_CS2_032"))
        self.assertEqual(eff.damage, 5)
        self.assertTrue(eff.aoe)
        self.assertEqual(eff.scope, "enemy_minions")

    def test_freeze(self) -> None:
        eff = parse_effect(self.db.get("CORE_CS2_024"))
        self.assertEqual(eff.damage, 3)
        self.assertTrue(eff.freeze)

    def test_draw(self) -> None:
        eff = parse_effect(self.db.get("CORE_CS2_023"))  # 奥术智慧：抽两张
        self.assertEqual(eff.draw, 2)

    def test_minion_without_text_is_known(self) -> None:
        eff = parse_effect(self.db.get("CORE_CS2_182"))  # 冰风雪人，无效果
        self.assertTrue(eff.known)
        self.assertTrue(eff.is_empty())


if __name__ == "__main__":
    unittest.main()
