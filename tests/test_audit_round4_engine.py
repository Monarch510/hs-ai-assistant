"""第四轮审计：决策引擎回归用例（斩杀估算 ↔ 实战同口径）。

原来这些口径散落在 max_face_damage / apply_action / apply_effect 三处，
漏算或错算时「理论能斩却判不能」「打不穿却显示可以斩杀」都不会报错。
这里每组都用「估算 vs 实打」对账，改一处忘了改另一处就会红。
"""

from __future__ import annotations

import unittest
from unittest import mock

from hsai import cards, policy
from hsai.cards import Card, CardDB
from hsai.model import Board, SideState, Unit, hero_power_effect
from hsai.model_effects import spell_damage_from_card, unit_spell_damage
from hsai.policy_race import _hand_burst, race_plan

REAL: CardDB = cards.load()


def _board(me: SideState, foe: SideState, db: CardDB = REAL) -> Board:
    return Board([me, foe], turn=9, active=0, cards=db)


def _spell(cid: str, name: str, cost: int, dmg: int) -> Card:
    return Card(id=cid, name=name, cost=cost, type="SPELL", text=f"造成{dmg}点伤害")


class FaceDamageParityTest(unittest.TestCase):
    """A1：估算与 apply_action 必须同一套结算，否则斩杀提示会骗人。"""

    def test_three_small_swings_break_a_big_taunt_and_still_hit_face(self) -> None:
        # 3 血嘲讽 + 三只 2 攻：旧算法要求「单段 ≥3」，判 0 点；
        # 实战是 2+2 破墙、第三只打脸 2 点斩。
        me = SideState(hero_hp=30)
        for _ in range(3):
            me.board.append(Unit("", "小怪", 2, 2, attacks_left=1))
        foe = SideState(hero_hp=2)
        foe.board.append(Unit("W", "墙", 0, 3, taunt=True))
        r = policy.max_face_damage(_board(me, foe), 0, REAL)
        self.assertEqual(r["damage"], 2)
        self.assertTrue(r["lethal"])

    def test_divine_taunt_costs_a_break_swing_then_its_full_hp(self) -> None:
        # 8/8 圣盾嘲讽：破盾只需一段，之后还得再打满 8 点，不是「3 点打穿」。
        me = SideState(hero_hp=30)
        for _ in range(3):
            me.board.append(Unit("", "大哥", 8, 8, attacks_left=1))
        foe = SideState(hero_hp=8)
        foe.board.append(Unit("W", "盾墙", 0, 8, taunt=True, divine=True))
        r = policy.max_face_damage(_board(me, foe), 0, REAL)
        self.assertEqual(r["damage"], 8)
        self.assertTrue(r["lethal"])

    def test_deathrattle_spawns_a_new_taunt_so_no_false_lethal(self) -> None:
        # 亡语「召唤一个1/1嘲讽」：打死第一面墙会再立一面，估算必须看见它。
        dr = Card(id="DR_R4", name="亡语墙", cost=1, type="MINION",
                  text="亡语：召唤一个1/1的嘲讽随从")
        db = CardDB([dr])
        me = SideState(hero_hp=30)
        me.board.append(Unit("", "大哥A", 8, 8, attacks_left=1))
        me.board.append(Unit("", "大哥B", 8, 8, attacks_left=1))
        foe = SideState(hero_hp=8)
        foe.board.append(Unit("DR_R4", "亡语墙", 1, 1, taunt=True))
        r = policy.max_face_damage(_board(me, foe, db), 0, db)
        self.assertFalse(r["lethal"])
        self.assertTrue(r["has_taunt"])

    def test_windfury_weapon_with_one_durability_only_swings_once(self) -> None:
        # 风怒刀 3 攻但只剩 1 耐久、英雄自身 0 攻：碎了就打不出第二下。
        for dur, damage, lethal in ((1, 3, False), (2, 6, True)):
            with self.subTest(dur=dur):
                me = SideState(hero_hp=30, hero_atk=0, hero_attacks_left=2,
                               weapon=Unit("", "风怒刀", 3, dur, windfury=True))
                r = policy.max_face_damage(_board(me, SideState(hero_hp=6)), 0, REAL)
                self.assertEqual(r["damage"], damage)
                self.assertEqual(r["lethal"], lethal)

    def test_spell_knapsack_picks_best_subset_not_cost_ascending(self) -> None:
        # 4 费：火球 6 伤 + 寒冰箭 3 伤。升序贪心先买寒冰箭会剩 2 费买不起火球，
        # 0/1 背包要挑出「单打火球」这档 6 伤。
        me = SideState(hero_hp=30, mana=4, max_mana=10,
                       hand=[_spell("FB_R4", "火球", 4, 6),
                             _spell("IB_R4", "寒冰箭", 2, 3)])
        r = policy.max_face_damage(_board(me, SideState(hero_hp=6)), 0, REAL)
        self.assertEqual(r["spell"], 6)
        self.assertTrue(r["lethal"])

    def test_coin_mana_pays_for_the_fireball(self) -> None:
        # 3 费 + 幸运币(0 费 eff.mana=1) + 火球(4 费 6 伤) 对 6 血：实战能斩。
        coin = Card(id="COIN_R4", name="幸运币", cost=0, type="SPELL",
                    text="获得一个空的法力水晶")
        me = SideState(hero_hp=30, mana=3, max_mana=10,
                       hand=[coin, _spell("FB_R4", "火球", 4, 6)])
        r = policy.max_face_damage(_board(me, SideState(hero_hp=6)), 0, REAL)
        self.assertEqual(r["spell"], 6)
        self.assertTrue(r["lethal"])

    def test_board_spell_damage_counts_from_card_text(self) -> None:
        # 日志视图里随从只带 card_id，spell_damage 字段是 0：要回查卡面「法术伤害+1」。
        self.assertEqual(spell_damage_from_card(REAL.get("AT_006")), 1)
        self.assertEqual(unit_spell_damage(Unit("AT_006", "达拉然铁骑士", 4, 3), REAL), 1)
        me = SideState(hero_hp=30, mana=4, max_mana=10,
                       hand=[_spell("FB_R4", "火球", 4, 6)])
        me.board.append(Unit("AT_006", "法伤随从", 4, 3))
        r = policy.max_face_damage(_board(me, SideState(hero_hp=7)), 0, REAL)
        self.assertEqual(r["spell"], 7)
        self.assertTrue(r["lethal"])

    def test_lethal_sequence_runs_without_the_need_check_gate(self) -> None:
        # 旧版 _lethal_sequence 开头先问 max_face_damage，估算说不能就 return None；
        # 现在枚举器自己用 apply_action 实打验证，不能再被估算挡住。
        me = SideState(hero_hp=30, mana=4, max_mana=10, hand=[_spell("FB_R4", "火球", 4, 6)])
        seq = policy._lethal_sequence(_board(me, SideState(hero_hp=6)), 0, REAL)
        self.assertIsNotNone(seq)
        self.assertTrue(any(a.card_id == "FB_R4" for a in seq))  # type: ignore[union-attr]


class PlanTurnDegradationTest(unittest.TestCase):
    """A1：估算够但枚举器拼不出线路时，别把「可以斩杀」透到 UI。"""

    def test_estimate_without_sequence_is_downgraded(self) -> None:
        me = SideState(hero_hp=30, mana=4, max_mana=10,
                       hand=[_spell("FB_R4", "火球", 4, 6)], hero_class="MAGE")
        b = _board(me, SideState(hero_hp=6))
        self.assertTrue(policy.max_face_damage(b, 0, REAL)["lethal"])
        with mock.patch("hsai.policy_plan._lethal_sequence", return_value=None):
            res = policy.plan_turn(b, REAL, side=0)
        self.assertFalse(res["lethal"]["lethal"])
        self.assertIn("没有能兑现的出牌线路", res["lethal"]["note"])


class HandBurstParityTest(unittest.TestCase):
    """A3/F6：抢血判断的手牌爆发必须与斩杀估算同口径（查费用 + 加法伤）。"""

    def test_zero_mana_burst_is_zero_four_mana_is_six(self) -> None:
        fb = _spell("FB_R4", "火球", 4, 6)
        for mana, expected in ((0, 0.0), (4, 6.0)):
            with self.subTest(mana=mana):
                me = SideState(hero_hp=30, mana=mana, max_mana=10, hand=[fb])
                b = _board(me, SideState(hero_hp=30))
                self.assertEqual(_hand_burst(b, 0, REAL), expected)
                self.assertEqual(race_plan(b, 0, REAL)["burst"], expected)


class HeroPowerParityTest(unittest.TestCase):
    """A2/F4/F5：英雄技能的费用与效果都要按真实卡结算。"""

    def test_zero_cost_hero_power_stays_zero(self) -> None:
        hp = Card(id="HP0_R4", name="零费技能", cost=0, type="HERO_POWER", text="造成1点伤害")
        self.assertEqual(hero_power_effect("MAGE", hp)[1], 0)
        hp2 = Card(id="HP2_R4", name="两费技能", cost=2, type="HERO_POWER", text="造成1点伤害")
        self.assertEqual(hero_power_effect("MAGE", hp2)[1], 2)

    def test_armor_and_draw_are_settled_not_dropped(self) -> None:
        hp = Card(id="HPA_R4", name="叠甲抽牌", cost=2, type="HERO_POWER",
                  text="获得4点护甲值。抽一张牌")
        me = SideState(hero_hp=30, mana=10, max_mana=10, hero_class="WARRIOR",
                       hero_power=hp, deck=[Card(id="D1", name="牌", cost=1)])
        b = _board(me, SideState(hero_hp=30))
        nb = policy.apply_action(b, 0, policy.Action("hero_power", cost=2, target="enemy_hero"), REAL)
        self.assertEqual(nb.sides[0].armor, 4)
        self.assertEqual(len(nb.sides[0].hand), 1)


class CthunParityTest(unittest.TestCase):
    """A4：克苏恩强化按「同一只」计数；凝视回血封顶 30 且只算实际伤害。"""

    def test_tripled_uses_per_unit_skills_not_global(self) -> None:
        for used_here, expected_atk in ((3, 9), (1, 3)):
            with self.subTest(used_here=used_here):
                me = SideState(hero_hp=30, cthun_uses_left=3, hero_class="NEUTRAL")
                me._cthun_by_unit = {"CTHUN#0": {f"s{i}" for i in range(used_here)}}
                me.cthun_used = {"x", "y", "z", "w"}   # 全局凑够 4 个也不能替当前这只
                me.board.append(Unit("", "友军", 1, 1))
                b = _board(me, SideState(hero_hp=30))
                act = policy.Action("cthun_skill", target="cthun:CTHUN#0:克苏恩的领域")
                nb = policy.apply_action(b, 0, act, REAL)
                self.assertEqual(nb.sides[0].board[0].atk, expected_atk)

    def test_gaze_heal_capped_at_30(self) -> None:
        for hp, expected in ((20, 30), (5, 25)):
            with self.subTest(hp=hp):
                b = _board(SideState(hero_hp=hp), SideState(hero_hp=30))
                policy.apply_cthun_skill(b, 0, "克苏恩的凝视", tripled=True)
                self.assertEqual(b.sides[0].hero_hp, expected)

    def test_gaze_heal_uses_actual_damage_not_overflow(self) -> None:
        me = SideState(hero_hp=29)
        foe = SideState(hero_hp=30)
        foe.board.append(Unit("", "小怪", 1, 1))
        b = _board(me, foe)
        policy.apply_cthun_skill(b, 0, "克苏恩的凝视", tripled=False)
        # 5 点打在 1 血随从上，只回 1 点（29→30），不是 29→34
        self.assertEqual(b.sides[0].hero_hp, 30)


if __name__ == "__main__":
    unittest.main()
