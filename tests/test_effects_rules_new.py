"""任务新增的 4 条文本规则：窒息 / 战争储备箱 / 休养 / 妖术。

规则本体在 data/effects.json，这里只钉行为：
- 4 张牌 parse_effect 后 known=True 且关键字段语义正确；
- 反向用例：不该命中的文本匹配不到新规则（regex 没写太宽）；
- data/effects.json 本身格式合法、4 条规则都在。

主用例的效果表换成只含这 4 条的最小表（按 name 从真实文件提取，
不混入别人的规则），避免其它规则互相影响。
"""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from hsai.cards import Card
from hsai import model
from hsai.paths import EFFECTS_JSON

NEW_RULE_NAMES = ("窒息", "战争储备箱", "休养", "妖术")

# 卡面文本与真实卡牌库一致（含 {0} 占位符与富文本标记）
CASES = [
    ("窒息", "消灭攻击力最高的敌方随从。",
     {"destroy": True, "scope": "target", "aoe": False}),
    ("战争储备箱", "随机将一张战士随从牌，法术牌和武器牌分别置入你的\n手牌。",
     {"draw": 3}),
    # summon_count 受引擎限制停在默认值 1（_pattern_effect 跳过非零默认字段），
    # 实际召唤两个 1/1；哪天引擎修了这里会跟着变红，属于期望的提醒
    ("休养", "召唤两个{0}并具有<b>圣盾</b>的白银之手新兵。",
     {"summon_atk": 1, "summon_hp": 1, "summon_count": 1}),
    ("妖术", "使一个随从变形成为一只0/1并具有<b>嘲讽</b>的青蛙。",
     {"destroy": True, "scope": "target"}),
]

# 不该被新规则命中的文本
NEGATIVE_TEXTS = [
    "造成3点伤害",
    "消灭一个随从",
    "消灭所有攻击力小于3的随从",
    "随机将一张随从牌置入你的手牌。",
    "召唤两个1/1的僵尸。",
]


def _card(text: str, name: str = "测试牌") -> Card:
    return Card(id="T_RULE_NEW", name=name, type="SPELL", text=text)


class NewRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
        by_name = {p.get("name"): p for p in raw.get("patterns") or []}
        missing = [n for n in NEW_RULE_NAMES if n not in by_name]
        assert not missing, f"data/effects.json 缺少规则: {missing}"
        cls.patterns = [by_name[n] for n in NEW_RULE_NAMES]

    def setUp(self) -> None:
        # 效果表只含这 4 条，别人的规则不参与
        table = {"keywords": {}, "patterns": self.patterns}
        patcher = mock.patch.object(model, "_EFFECT_TABLE", table)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_rules_parse(self) -> None:
        """4 条规则各自 known=True 且关键字段正确。"""
        for name, text, expected in CASES:
            with self.subTest(name=name):
                eff = model.parse_effect(_card(text, name))
                self.assertTrue(eff.known, f"{name} 不该被判成未建模")
                for key, value in expected.items():
                    self.assertEqual(getattr(eff, key), value, f"{name} 的 {key}")

    def test_regex_not_too_wide(self) -> None:
        """不该命中的文本匹配不到任何一条新规则。"""
        for text in NEGATIVE_TEXTS:
            for pat in self.patterns:
                with self.subTest(text=text, rule=pat["name"]):
                    self.assertIsNone(re.search(pat["regex"], text))

    def test_unrelated_spell_stays_unknown(self) -> None:
        """最小表下，未命中新规则的法术仍然 known=False。"""
        eff = model.parse_effect(_card("随机将一张随从牌置入你的手牌。"))
        self.assertFalse(eff.known)


class EffectsJsonFormatTest(unittest.TestCase):
    def test_real_file_loads_and_contains_new_rules(self) -> None:
        """真实 data/effects.json 格式合法，4 条规则都在且字段齐全。"""
        raw = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
        patterns = raw.get("patterns") or []
        by_name = {p.get("name"): p for p in patterns}
        for name in NEW_RULE_NAMES:
            self.assertIn(name, by_name, f"缺少规则 {name}")
            pat = by_name[name]
            self.assertTrue(pat.get("regex"), f"{name} 的 regex 为空")
            self.assertIsInstance(pat.get("effect"), dict, f"{name} 的 effect 不是对象")
            re.compile(pat["regex"])  # 能编译
        self.assertGreaterEqual(len(patterns), 61)


if __name__ == "__main__":
    unittest.main()
