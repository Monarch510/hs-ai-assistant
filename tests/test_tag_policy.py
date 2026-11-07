"""出牌计划接入动态标签权重的用例（DYNAMIC-WEIGHTS §3）。

这里只测 policy 怎么用权重表，不测权重表本身（那是 tag_weights 的测试）。
固定 fixture 不读 data/ 里的用户数据：卡牌按名从卡库取，盘面手工构造。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import policy
from hsai.model import Board, SideState, Unit
from hsai.tag_rules import RULES, TAG_ORDER

# 不传 tag_weights 时的完整输出（"final" 是盘面对象，单独排除）。
# 这是「接标签权重之前行为」的回归锚：以后改出牌排序，这里必须同步改并写清原因。
GOLDEN = {
    "mode": "trade",
    "race": {
        "mode": "trade",
        "my_turns": 99.0,
        "his_turns": 99.0,
        "my_damage": 0.0,
        "his_damage": 0.0,
        "burst": 0.0,
        "reason": "伤害信息不足",
    },
    "plan": [
        {"desc": "打出 淡水鳄（2费 2/3）", "reason": "站上 淡水鳄", "gain": 7.31},
    ],
    "gain": 7.31,
    "alternatives": [
        {
            "desc": "英雄技能：英雄技能",
            "reason": "调整站位与手牌",
            "gain": 7.31,
            "then": ["打出 淡水鳄（2费 2/3）"],
        },
    ],
    "lethal": {
        "attack": 0,
        "taunt_block": 0,
        "spell": 0,
        "hero_power": 0,
        "damage": 0,
        "need": 30,
        "lethal": False,
        "has_taunt": False,
        "face_spells": [],
    },
}


class TagPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _board(self, *, hand=(), my_units=(), opp_units=(), my_hp=30, opp_hp=30,
               mana=10, hero_class="NEUTRAL", turn=9) -> Board:
        b = Board([SideState(hero_hp=my_hp, mana=mana, max_mana=mana, hero_class=hero_class),
                   SideState(hero_hp=opp_hp)], turn=turn, active=0, cards=self.db)
        b.sides[0].hand = [self.db.by_name_or_id(name) for name in hand]
        for atk, hp in my_units:
            b.sides[0].board.append(Unit("", "我方随从", atk, hp, attacks_left=1))
        for atk, hp in opp_units:
            b.sides[1].board.append(Unit("", "对方随从", atk, hp, attacks_left=1))
        return b

    @staticmethod
    def _strip(result: dict) -> dict:
        """去掉 final（盘面对象），其余逐字段比较。"""
        return {k: v for k, v in result.items() if k != "final"}

    def _tags(self, name: str) -> list[str]:
        card = self.db.by_name_or_id(name)
        return [t for t in TAG_ORDER if RULES[t](card)]

    # ------------------------------------------------------------ 现状回归
    def test_default_plan_matches_golden(self) -> None:
        result = policy.plan_turn(self._board(hand=["淡水鳄"]), self.db, side=0)
        self.assertEqual(self._strip(result), GOLDEN)

    def test_tag_scale_zero_equals_default(self) -> None:
        """传了权重表但缩放为 0 时，输出必须和完全不传一样。"""
        b = self._board(hand=["淡水鳄"])
        base = policy.plan_turn(b, self.db, side=0)
        off = policy.plan_turn(b, self.db, side=0, tag_weights={t: 3.0 for t in TAG_ORDER},
                               tag_scale=0.0)
        self.assertEqual(self._strip(off), self._strip(base))

    # ------------------------------------------------------------ 叠加口径
    def test_tags_stack_exactly_once_per_card(self) -> None:
        """一张牌只按它的标签权重和叠加一次 —— 曾经 policy.py 里留过两套
        叠加逻辑（上一轮 subagent 残留），同一张牌会被加两倍分。"""
        b = self._board(hand=["火球术"])
        base = policy.plan_turn(b, self.db, side=0, steps=1)
        self.assertIn("火球术", base["plan"][0]["desc"])

        weights = {"单解": 1.0, "斩杀": 2.0}
        expected = sum(weights.get(t, 0.0) for t in self._tags("火球术"))
        boosted = policy.plan_turn(b, self.db, side=0, steps=1, tag_weights=weights)
        gain = boosted["plan"][0]["gain"] - base["plan"][0]["gain"]
        self.assertAlmostEqual(gain, policy.TAG_WEIGHT_SCALE * expected, places=2)

        doubled = policy.plan_turn(b, self.db, side=0, steps=1, tag_weights=weights,
                                   tag_scale=2.0)
        gain2 = doubled["plan"][0]["gain"] - base["plan"][0]["gain"]
        self.assertAlmostEqual(gain2, 2 * policy.TAG_WEIGHT_SCALE * expected, places=2)

    def test_scale_keeps_tags_below_board_priority(self) -> None:
        """叠加分（1-3 分）要低于「推进场面」优先级 3.0，不能推翻既有排序。"""
        self.assertEqual(policy.TAG_WEIGHT_SCALE, 1.0)

    def test_high_weight_tag_changes_choice(self) -> None:
        """权重够大时，带高权重标签的牌排到前面。"""
        b = self._board(hand=["淡水鳄", "火球术"])
        plain = policy.plan_turn(b, self.db, side=0)
        self.assertIn("淡水鳄", plain["plan"][0]["desc"])

        nuke = policy.plan_turn(b, self.db, side=0,
                                tag_weights={"单解": 50.0, "斩杀": 50.0})
        self.assertIn("火球术", nuke["plan"][0]["desc"])

        board_only = policy.plan_turn(b, self.db, side=0, tag_weights={"站场": 50.0})
        self.assertIn("淡水鳄", board_only["plan"][0]["desc"])

    # ------------------------------------------------------------ 不该动的地方
    def test_lethal_route_ignores_tag_weights(self) -> None:
        b = self._board(my_units=[(5, 5)], opp_hp=4)
        base = policy.plan_turn(b, self.db, side=0)
        weighted = policy.plan_turn(b, self.db, side=0, tag_weights={"斩杀": 99.0})
        self.assertEqual(self._strip(weighted), self._strip(base))
        self.assertTrue(base["lethal"]["lethal"])
        self.assertEqual(base["gain"], 999.0)

    def test_attack_and_hero_power_get_no_tag_bonus(self) -> None:
        """手里没有牌时盘面只剩攻击/英雄技能，权重再离谱也不该改分数。"""
        b = self._board(my_units=[(3, 3)], hero_class="MAGE")
        base = policy.plan_turn(b, self.db, side=0)
        weighted = policy.plan_turn(b, self.db, side=0,
                                    tag_weights={t: 50.0 for t in TAG_ORDER})
        self.assertEqual(self._strip(weighted), self._strip(base))
        descs = [p["desc"] for p in base["plan"]]
        self.assertTrue(any("攻击" in d for d in descs))
        self.assertTrue(any("英雄技能" in d for d in descs))

    def test_alternatives_inherit_tag_weights(self) -> None:
        """备选路线的加分来自递归的子路线，权重必须透传进去。"""
        b = self._board(hand=["淡水鳄"])
        base = policy.plan_turn(b, self.db, side=0)
        weighted = policy.plan_turn(b, self.db, side=0, tag_weights={"站场": 5.0})
        base_alts = {a["desc"]: a for a in base["alternatives"]}
        weighted_alts = {a["desc"]: a for a in weighted["alternatives"]}
        key = next(d for d in base_alts if "英雄技能" in d)
        self.assertIn(key, weighted_alts)
        delta = weighted_alts[key]["gain"] - base_alts[key]["gain"]
        self.assertAlmostEqual(delta, policy.TAG_WEIGHT_SCALE * 5.0, places=2)

    def test_alternative_first_action_gets_tag_bonus(self) -> None:
        """备选路线里的「第一个动作」也是打出的手牌，必须同样叠加标签分。

        曾经漏了：只有递归子路线（后续动作）透传了权重，备选首动作的 gain
        仍只是 evaluate 差值 —— 抽牌=30 时「打出 奥术智慧」这条备选纹丝不动。
        """
        b = self._board(hand=["淡水鳄", "奥术智慧", "治疗波"], opp_hp=28)
        base = policy.plan_turn(b, self.db, side=0)
        self.assertIn("淡水鳄", base["plan"][0]["desc"])

        weights = {t: (30.0 if t == "抽牌" else 0.0) for t in TAG_ORDER}
        weighted = policy.plan_turn(b, self.db, side=0, tag_weights=weights)

        base_alts = {a["desc"]: a for a in base["alternatives"]}
        weighted_alts = {a["desc"]: a for a in weighted["alternatives"]}
        draw_base = next(a for d, a in base_alts.items() if "奥术智慧" in d)
        draw_weighted = next(a for d, a in weighted_alts.items() if "奥术智慧" in d)
        self.assertAlmostEqual(
            draw_weighted["gain"] - draw_base["gain"],
            policy.TAG_WEIGHT_SCALE * 30.0, places=2,
        )
        # 只有这张牌自己的标签加权：治疗波（治疗=0）的备选分不该动
        heal_base = next(a for d, a in base_alts.items() if "治疗波" in d)
        heal_weighted = next(a for d, a in weighted_alts.items() if "治疗波" in d)
        self.assertEqual(heal_weighted["gain"], heal_base["gain"])
        # 主 plan 不受影响：淡水鳄是站场（权重 0），仍靠 raw + PRIORITY_BONUS 胜出
        self.assertIn("淡水鳄", weighted["plan"][0]["desc"])


if __name__ == "__main__":
    unittest.main()
