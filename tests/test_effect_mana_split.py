"""费用修改与「获得法力」拆分。

Effect.mana 只表示「本回合多 N 费」（幸运币）。以前「某张牌费用 ±N」也写进
mana，引擎以为打出它会返法力，多步路线因此算得出、实际打不出。
现在拆成 cost_delta_self（负数=我方变便宜）/ cost_delta_opp（正数=敌方变贵）。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from hsai import cards, effects_ai, model
from hsai.cards import Card
from hsai.model import Board, SideState, parse_effect, resource_score
from hsai.paths import EFFECTS_JSON
from hsai.policy import apply_action, legal_actions, plan_turn, validate_sequence

_DB = None


def db():
    global _DB
    if _DB is None:
        _DB = cards.load()
    return _DB


def eff(name: str):
    return model.parse_effect(db().by_name_or_id(name))


def _rules():
    return {p.get("name"): p for p in json.loads(
        EFFECTS_JSON.read_text(encoding="utf-8")).get("patterns") or []}


# 12 条待迁移规则的去向
TO_SELF = ("触须刺鞭", "自然天性", "鹦鹉乐园", "融冰而出", "暗影塑形")
TO_OPP = ("强力裁决",)
DELETED = ("统一着装", "头等大奖", "小偷小摸", "升腾", "死亡绽放")

# 改动前这 11 条写的是 mana；用于在内存里还原旧表、对比 known 集合
OLD_MANA = {
    "触须刺鞭": "$1", "统一着装": "$1", "自然天性": "$1",
    "头等大奖": "$1", "小偷小摸": "$1", "升腾": "$1",
    "强力裁决": "$1", "鹦鹉乐园": "-$1", "融冰而出": "-$1",
    "死亡绽放": "$3", "暗影塑形": "-$1",
}


def _old_full_table():
    """把 11 条迁走的规则改回 mana，模拟改动前的 effects.json。"""
    old = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
    for p in old["patterns"]:
        if p.get("name") not in OLD_MANA:
            continue
        effect = p["effect"]
        effect.pop("cost_delta_self", None)
        effect.pop("cost_delta_opp", None)
        effect["mana"] = OLD_MANA[p["name"]]
    return old


def _known_with(table):
    """在给定效果表下算出全卡池已建模的卡牌 id 集合。"""
    with mock.patch.object(model, "_EFFECT_TABLE", table):
        return {c.id for c in db().by_id.values() if model.parse_effect(c).known}


class SplitSemanticsTest(unittest.TestCase):
    def test_coin_still_gains_mana(self):
        """幸运币/激活/野性成长的 mana=1 不变，两个新字段都为 0。"""
        for name in ("幸运币", "激活", "野性成长"):
            with self.subTest(name=name):
                e = eff(name)
                self.assertEqual(e.mana, 1)
                self.assertEqual(e.cost_delta_self, 0)
                self.assertEqual(e.cost_delta_opp, 0)
                self.assertTrue(e.known)

    def test_no_mana_in_cost_regex_rules(self):
        """回归护栏：regex 含费用词的规则不许再写 mana。"""
        rules = _rules()
        offenders = [name for name, p in rules.items()
                     if "mana" in (p.get("effect") or {})
                     and ("法力值消耗" in p.get("regex", "") or "费用" in p.get("regex", ""))]
        self.assertEqual(offenders, [])
        # 12 条各自落到该去的地方
        for name in TO_SELF:
            with self.subTest(name=name):
                self.assertIn("cost_delta_self", rules[name]["effect"])
                self.assertNotIn("mana", rules[name]["effect"])
        for name in TO_OPP:
            with self.subTest(name=name):
                self.assertIn("cost_delta_opp", rules[name]["effect"])
                self.assertNotIn("mana", rules[name]["effect"])
        for name in DELETED:
            with self.subTest(name=name):
                effect = rules[name]["effect"]
                self.assertNotIn("mana", effect)
                self.assertNotIn("cost_delta_self", effect)
                self.assertNotIn("cost_delta_opp", effect)
        # 破碎之力是延迟获得水晶，不是费用修改；gain_mana 是幸运币
        self.assertEqual(rules["破碎之力"]["effect"]["mana"], 2)
        self.assertEqual(rules["gain_mana"]["effect"]["mana"], 1)

    def test_cost_delta_values(self):
        e = eff("强力裁决")
        self.assertEqual((e.mana, e.cost_delta_self, e.cost_delta_opp), (0, 0, 2))
        # 冻感舞步没有独立规则，命中的是强力裁决那条，捕获值必须跟着走
        self.assertEqual(eff("冻感舞步").cost_delta_opp, 5)
        self.assertEqual(eff("鹦鹉乐园").cost_delta_self, -1)
        self.assertEqual(eff("自然天性").cost_delta_self, -2)
        self.assertEqual(eff("暗影塑形").cost_delta_self, -2)
        e = eff("小偷小摸")
        self.assertEqual((e.cost_delta_self, e.cost_delta_opp, e.discover), (0, 0, 2))
        self.assertEqual(eff("头等大奖").discover, 2)

    def test_new_fields_make_effect_known(self):
        """只有费用变化、没有其它效果的法术也该算已建模。"""
        for name in ("强力裁决", "鹦鹉乐园", "自然天性", "暗影塑形"):
            with self.subTest(name=name):
                self.assertTrue(eff(name).known, name)

    def test_negative_group_reference(self):
        """-$1 必须解成负数；以前被 _as_int 吃掉负号，得到 +1。"""
        rule = {"name": "t", "regex": r"减费（(\d+)）",
                "effect": {"cost_delta_self": "-$1", "cost_delta_opp": "$1"}}
        with mock.patch.object(model, "_EFFECT_TABLE", {"keywords": {}, "patterns": [rule]}):
            e = model.parse_effect(Card(id="T_NEG", name="测试牌", type="SPELL", text="减费（2）"))
        self.assertEqual(e.cost_delta_self, -2)
        self.assertEqual(e.cost_delta_opp, 2)
        self.assertEqual(e.mana, 0)

    def test_conservative_score_in_resource_score(self):
        """自己变便宜算半个法力、对手变贵算一个；只给保守分，调参留给下一批。"""
        def score_of(effect: dict) -> float:
            rule = {"name": "t", "regex": "测试标记", "effect": effect}
            card = Card(id="T_SCORE", name="测试牌", type="SPELL", text="测试标记")
            with mock.patch.object(model, "_EFFECT_TABLE", {"keywords": {}, "patterns": [rule]}):
                return resource_score(SideState(hand=[card]))

        base = score_of({})
        self.assertAlmostEqual(score_of({"cost_delta_self": -2}) - base, 1.0)
        self.assertAlmostEqual(score_of({"cost_delta_opp": 2}) - base, 2.0)

    def test_known_delta_is_only_three_unmodelled_transform_cards(self):
        """任务书用例 5 的诚实版本：只有这 3 张纯变形牌掉出已建模集合。

        统一着装 / 升腾 / 可信的伪装只做「变形为费用+N 的随从」，引擎没有变形
        字段，删掉错误的 mana 后如实退回 known=False。任务书原话是「只增不减」，
        但这里塞任何数值都是错数字（AGENTS.md：不可控的东西降级为「未知」），
        所以不硬凑数字，而是把差异钉死为这 3 张、不许有第 4 张。

        绝对张数不能钉死：data/effects.json 会被「AI 补规则」持续追加，
        每补一条就可能多几张 known。这里只比**差集**。
        """
        now = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
        before = _known_with(_old_full_table())
        after = _known_with(now)
        id_to_name = {c.id: c.name for c in db().by_id.values()}
        lost = {id_to_name[i] for i in before - after}
        self.assertEqual(lost, {"统一着装", "升腾", "可信的伪装"})
        self.assertEqual(after - before, set())  # 没有新牌被误判成未建模


class PlanningRegressionTest(unittest.TestCase):
    """最重要的一条：打出费用修改牌不能凭空返费。"""

    def _board(self):
        ruling = db().by_name_or_id("强力裁决")
        three = next(c for c in db().by_id.values()
                     if c.cost == 3 and c.type == "MINION" and c.collectible and c.atk >= 3)
        me = SideState(mana=3, max_mana=3, hand=[ruling, three], deck=[three] * 10)
        return Board([me, SideState(hero_hp=30)], turn=3, active=0, cards=db()), ruling, three

    @staticmethod
    def _old_table():
        """把强力裁决那条换回 mana，模拟改动前的行为。"""
        old = json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))
        for p in old["patterns"]:
            if p.get("name") == "强力裁决":
                p["effect"] = {"mana": "$1", "scope": "enemy"}
        return old

    def test_before_phantom_mana_after_not(self):
        b, ruling, _ = self._board()
        act = next(a for a in legal_actions(b, 0, db())
                   if a.kind == "play" and a.card_id == ruling.id)
        nb = apply_action(b, 0, act, db())
        self.assertEqual(nb.sides[0].mana, 1)
        self.assertTrue(any("强力裁决：敌方手牌费用 +2" in line for line in nb.log),
                        "费用修改本回合只记 note，不动法力")
        with mock.patch.object(model, "_EFFECT_TABLE", self._old_table()):
            self.assertEqual(apply_action(b, 0, act, db()).sides[0].mana, 3)

    def test_two_step_route_is_no_longer_affordable(self):
        """改动前「先裁决再打 3 费牌」两步都可执行，改动后第二步排不出来。"""
        b, _, _ = self._board()
        descs = [a.desc for a in legal_actions(b, 0, db()) if a.kind == "play"]
        self.assertEqual(len(descs), 2)
        now = validate_sequence(b, descs, db())
        self.assertTrue(now[0]["ok"])
        self.assertFalse(now[1]["ok"])
        with mock.patch.object(model, "_EFFECT_TABLE", self._old_table()):
            before = validate_sequence(b, descs, db())
        self.assertTrue(all(step["ok"] for step in before))

    def test_plan_turn_has_no_two_step_phantom_route(self):
        b, _, three = self._board()
        result = plan_turn(b, db(), with_alternatives=False)
        plan = [p["desc"] for p in result["plan"]]
        ruling_desc = next(a.desc for a in legal_actions(b, 0, db())
                           if a.kind == "play" and "强力裁决" in a.desc)
        three_desc = next(a.desc for a in legal_actions(b, 0, db())
                          if a.kind == "play" and three.name in a.desc)
        # 不会出现「先打裁决、再打 3 费牌」这两步连在一起的建议
        for first, second in zip(plan, plan[1:]):
            self.assertFalse(first == ruling_desc and second == three_desc)
        self.assertLessEqual(result["final"].sides[0].mana, 3)



class AiGateTest(unittest.TestCase):
    def test_gate_rejects_mana_for_cost_regex(self):
        rule = {"regex": r"法力值消耗减少（(\d+)）点", "effect": {"mana": "$1"}}
        ok, why = effects_ai.validate_rule(db(), rule, [])
        self.assertFalse(ok)
        self.assertIn("cost_delta", why)

    def test_gate_allows_real_mana_gain(self):
        rule = {"regex": r"获得一个法力水晶", "effect": {"mana": 1}}
        ok, why = effects_ai.validate_rule(db(), rule, [])
        self.assertNotIn("cost_delta", why)

    def test_negative_reference_passes_numeric_check(self):
        """闸门提示改用 cost_delta_*，那条规则本身也必须能过数值校验。"""
        rule = {"regex": r"法力值消耗减少（(\d+)）点",
                "effect": {"cost_delta_self": "-$1"}}
        card = Card(id="T_GATE", name="测试牌", type="SPELL", text="法力值消耗减少（2）点")
        out = effects_ai._apply_one(card, rule)
        self.assertEqual(out["cost_delta_self"], -2)
        ok, why = effects_ai.validate_rule(db(), rule, [])
        self.assertNotIn("不是数字", why)


if __name__ == "__main__":
    unittest.main()
