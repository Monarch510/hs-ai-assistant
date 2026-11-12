"""AI 补规则的五道闸门测试（不联网）。"""

from __future__ import annotations

import unittest
from dataclasses import fields as dataclass_fields

from hsai import cards, effects_ai
from hsai.cards import Card, CardDB
from hsai.model import Effect


class ValidateRuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def test_accepts_narrow_rule(self) -> None:
        rule = {
            "regex": r"召唤五个(\d+)/(\d+)的",
            "effect": {"summon_count": 5, "summon_atk": "$1", "summon_hp": "$2"},
            "note": "亡者大军这类写法",
        }
        ok, why = effects_ai.validate_rule(
            self.db, rule, ["TUTR_RLK_060"]
        )
        # 这张牌本来就没建模，规则应当能放行（除非新覆盖超过上限）
        self.assertTrue(ok, why)

    def test_rejects_rule_that_misses_target(self) -> None:
        rule = {"regex": r"绝对不存在的文本", "effect": {"damage": 1}}
        ok, why = effects_ai.validate_rule(self.db, rule, ["TUTR_RLK_060"])
        self.assertFalse(ok)
        self.assertIn("没被", why)

    def test_rejects_broad_rule(self) -> None:
        rule = {"regex": r"造成(\d+)点伤害", "effect": {"damage": "$1"}}
        ok, why = effects_ai.validate_rule(
            self.db, rule, ["TUTR_RLK_060", "CS2_029"]
        )
        self.assertFalse(ok)
        self.assertTrue("宽泛" in why or "改变" in why, why)

    def test_empty_rule_rejected(self) -> None:
        ok, why = effects_ai.validate_rule(self.db, {}, ["CS2_029"])
        self.assertFalse(ok)

    def test_rejects_unknown_effect_field(self) -> None:
        """表里写了 Effect 没有的字段：parse_effect 会 setattr 挂上去骗过校验，
        但引擎根本不会结算，必须当场拒掉（实测 AI 补规则写出过 adjacent_targets）。"""
        rule = {
            "regex": r"召唤五个(\d+)/(\d+)的",
            "effect": {"summon_count": 5, "adjacent_targets": "true"},
            "note": "占位符写错的例子",
        }
        ok, why = effects_ai.validate_rule(self.db, rule, ["TUTR_RLK_060"])
        self.assertFalse(ok)
        self.assertIn("adjacent_targets", why)

    def test_rejects_rule_changing_only_new_effect_fields(self) -> None:
        """签名必须覆盖 Effect 全字段：只改新字段的规则也曾绕过闸门②。"""
        self.assertEqual(len(effects_ai.FIELDS), len(dataclass_fields(Effect)))
        db = CardDB([
            Card(id="A", name="甲", type="SPELL", text="造成2点伤害，抽一张牌"),
            Card(id="B", name="乙", type="SPELL", text="造成3点伤害"),
        ])
        for field, value in (("cost_delta_opp", 2), ("cost_delta_self", -1),
                             ("set_hp", 2), ("min_atk", 2), ("steal", True)):
            with self.subTest(field=field):
                rule = {"regex": r"造成(\d+)点伤害", "effect": {field: value}}
                ok, why = effects_ai.validate_rule(db, rule, ["A"])
                self.assertFalse(ok, why)
                self.assertIn("会改变", why)


if __name__ == "__main__":
    unittest.main()
