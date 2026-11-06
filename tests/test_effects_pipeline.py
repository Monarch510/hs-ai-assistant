"""表驱动规则 → parse_effect 的搬运链路。

`data/effects.json` 是行为定义：表里写了什么字段，`parse_effect` 就得搬什么。
以前拷贝块只认 10 个字段，剩下一半静默丢弃 —— 规则进了表、闸门记录「通过」、
解析结果却还是 `known=False`，21 条规则等于白写。

所以这里不逐条手写「这个字段应该被拷贝」，而是**按表里实际出现过的键反查**：
以后谁往表里加了新键却忘了在 parse_effect 里搬，这批用例会直接红。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields as dataclass_fields
from unittest import mock

from hsai import cards, model
from hsai.cards import Card
from hsai.paths import EFFECTS_JSON
from hsai.model import Effect

# 只被自造的最小规则命中，不会撞上内置正则（没有 造成/召唤/抽/获得 这些词）
MARK = "测试标记文本"

# 修饰符：只有和 damage / destroy 一起时才有意义，单独一条规则测不出来
MODIFIERS = ("scope", "aoe")


def _patterns() -> list[dict]:
    raw = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
    return raw.get("patterns") or []


def _table(*rules: dict) -> dict:
    return {"keywords": {}, "patterns": list(rules)}


def _rule(**effect: object) -> dict:
    return {"name": "test", "regex": MARK, "effect": effect}


def _parse(text: str = MARK) -> Effect:
    return model.parse_effect(Card(id="T_PIPE", name="测试牌", type="SPELL", text=text))


class FieldCoverageTest(unittest.TestCase):
    def test_every_key_in_table_reaches_effect(self) -> None:
        """表里出现过的每个 effect 键，都必须真的落到返回的 Effect 上。"""
        defaults = {f.name: f.default for f in dataclass_fields(Effect)}
        keys = sorted({k for pat in _patterns()
                       for k in (pat.get("effect") or {}) if k not in MODIFIERS})
        self.assertTrue(keys, "效果表里没有任何字段，用例失去意义")
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, defaults, f"{key} 不是 Effect 的字段")
                default = defaults[key]
                # 数值字段给 2（顺带验证「非零默认值也能写」），布尔字段给 True
                probe = 2 if isinstance(default, int) and not isinstance(default, bool) else True
                with mock.patch.object(model, "_EFFECT_TABLE", _table(_rule(**{key: probe}))):
                    eff = _parse()
                self.assertEqual(getattr(eff, key), probe, f"{key} 没被 parse_effect 搬过来")

    def test_nonzero_default_can_be_written(self) -> None:
        """summon_count 默认 1，规则写 3 必须能盖掉（旧代码按真值判断，写不进）。"""
        with mock.patch.object(model, "_EFFECT_TABLE", _table(_rule(summon_count=3))):
            self.assertEqual(_parse().summon_count, 3)

    def test_earlier_rule_wins(self) -> None:
        """两条规则都写 summon_count 时，靠前的那条不被后面的覆盖。"""
        first = {"name": "first", "regex": "测试标记", "effect": {"summon_count": 2}}
        second = {"name": "second", "regex": "标记文本", "effect": {"summon_count": 5}}
        with mock.patch.object(model, "_EFFECT_TABLE", _table(first, second)):
            self.assertEqual(_parse().summon_count, 2)

    def test_scope_and_aoe_come_with_damage(self) -> None:
        """scope / aoe 是修饰符：跟着 damage 一起搬。"""
        rule = {"name": "t", "regex": MARK,
                "effect": {"damage": 3, "scope": "enemy_minions", "aoe": True}}
        with mock.patch.object(model, "_EFFECT_TABLE", _table(rule)):
            eff = _parse()
        self.assertEqual((eff.damage, eff.scope, eff.aoe), (3, "enemy_minions", True))


class RealTableTest(unittest.TestCase):
    """真实 data/effects.json 上，原来「写了但不生效」的规则现在都要生效。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def _eff(self, name: str) -> Effect:
        return model.parse_effect(self.db.by_name_or_id(name))

    def test_former_dead_rules_now_apply(self) -> None:
        self.assertEqual(self._eff("图腾物证").summon_count, 4)
        self.assertEqual(self._eff("演武仪式").summon_count, 3)
        self.assertEqual(self._eff("死亡绽放").summon_count, 2)
        self.assertEqual(self._eff("休养").summon_count, 2)
        self.assertTrue(self._eff("安保盲区").discover)
        self.assertGreater(self._eff("破碎之力").mana, 0)
        self.assertGreater(self._eff("真言术：障").buff_hp, 0)

    def test_known_count_never_goes_back(self) -> None:
        """改动前基线 14981 张已建模，只许增不许减。"""
        known = sum(1 for c in self.db.by_id.values() if model.parse_effect(c).known)
        self.assertGreaterEqual(known, 14981)
        for name in ("窒息", "妖术"):
            self.assertTrue(self._eff(name).destroy, name)


if __name__ == "__main__":
    unittest.main()
