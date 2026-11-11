"""决策层的关键规则测试。

这些用例对应真实对局里出现过的错误判断，不是"覆盖率装饰"。
"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.model import Board, Effect, SideState, Unit, parse_effect


def _board(our_hp: int = 30, opp_hp: int = 30, mana: int = 10) -> Board:
    db = cards.load()
    b = Board([SideState(hero_hp=our_hp, mana=mana, max_mana=mana), SideState(hero_hp=opp_hp)],
              5, 0, cards=db)
    return b


class FaceDamageRulesTest(unittest.TestCase):
    def test_minion_that_already_attacked_is_not_counted(self) -> None:
        """已经打过的随从不能再算进可达伤害。

        曾经的真实 bug：T18 四个随从都打过了，max_face_damage 仍按 24 点算，
        于是面板说"能斩杀"，可实际上一下都打不出去。
        """
        b = _board()
        b.sides[0].board.append(Unit("", "打过的随从", 7, 7, attacks_left=0))
        b.sides[0].board.append(Unit("", "没打过的随从", 5, 5, attacks_left=1))
        r = policy.max_face_damage(b, 0, b.cards)
        self.assertEqual(r["attack"], 5)
        self.assertFalse(r["lethal"])

    def test_frozen_minion_is_not_counted(self) -> None:
        b = _board()
        b.sides[0].board.append(Unit("", "被冻结", 6, 6, attacks_left=1, frozen=True))
        r = policy.max_face_damage(b, 0, b.cards)
        self.assertEqual(r["attack"], 0)

    def test_lethal_needs_enough_damage(self) -> None:
        b = _board(opp_hp=4)
        b.sides[0].board.append(Unit("", "打手", 5, 5, attacks_left=1))
        r = policy.max_face_damage(b, 0, b.cards)
        self.assertTrue(r["lethal"])
        self.assertEqual(r["damage"], 5)


class LegalActionRulesTest(unittest.TestCase):
    def test_damage_spell_can_go_face(self) -> None:
        """直伤法术必须能打脸，否则手握火球却永远不推荐斩杀。"""
        db = cards.load()
        b = Board([SideState(hero_hp=30, mana=10, max_mana=10, hero_class="MAGE"),
                   SideState(hero_hp=5)], 5, 0, cards=db)
        b.sides[0].hand = [db.by_name_or_id("火球术")]
        descs = [a.desc for a in policy.legal_actions(b, 0, db) if a.kind == "play"]
        self.assertTrue(any("火球术" in d and "打脸" in d for d in descs))

    def test_taunt_blocks_face_damage(self) -> None:
        """撞嘲讽的攻击不会溢出到脸：8/8 撞 5 血嘲讽，对面 3 血打不死。"""
        db = cards.load()
        b = Board([SideState(hero_hp=30, mana=10, max_mana=10), SideState(hero_hp=3)], 5, 0, cards=db)
        b.sides[0].board.append(Unit("", "大怪", 8, 8, attacks_left=1))
        b.sides[1].board.append(Unit("", "嘲讽", 2, 5, taunt=True))
        r = policy.max_face_damage(b, 0, db)
        self.assertFalse(r["lethal"])
        # 唯一一次攻击被嘲讽吃掉，溢出的 3 点不会转到脸上
        self.assertEqual(r["damage"], 0)
        self.assertEqual(r["attack"], 8)

    def test_no_fake_lethal_action(self) -> None:
        """没有任何可执行动作时，不能伪造一条"全走脸，凑齐斩杀"。"""
        db = cards.load()
        b = Board([SideState(hero_hp=30, mana=0, max_mana=0, hero_class="MAGE"),
                   SideState(hero_hp=3)], 5, 0, cards=db)
        res = policy.plan_turn(b, db, side=0)
        titles = [p["desc"] for p in res["plan"]]
        self.assertNotIn("全走脸，凑齐斩杀", titles)
        self.assertFalse(res["lethal"]["lethal"])


# 并入自 tests/test_race.py：抢血 / 解场判定
class RacePlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def board(self, my_hp, foe_hp, my_board, foe_board, hand=()):
        b = Board([SideState(hero_hp=my_hp, mana=5, max_mana=5),
                   SideState(hero_hp=foe_hp)], 8, 0, cards=self.db)
        b.sides[0].board = [Unit("", f"我{i}", a, h, attacks_left=1)
                            for i, (a, h) in enumerate(my_board)]
        b.sides[1].board = [Unit("", f"他{i}", a, h)
                            for i, (a, h) in enumerate(foe_board)]
        b.sides[0].hand = list(hand)
        return b

    def test_fast_kill_means_race(self) -> None:
        b = self.board(25, 20, [(5, 5)], [(3, 3)])
        plan = policy.race_plan(b, 0, self.db, 3.0)
        self.assertEqual(plan["mode"], "race")
        self.assertLess(plan["my_turns"], plan["his_turns"])

    def test_about_to_die_means_trade(self) -> None:
        b = self.board(8, 20, [(2, 2)], [(8, 8)])
        plan = policy.race_plan(b, 0, self.db, 3.0)
        self.assertEqual(plan["mode"], "trade")

    def test_no_damage_falls_back_to_trade(self) -> None:
        b = self.board(30, 30, [], [(1, 1)])
        plan = policy.race_plan(b, 0, self.db, 1.5)
        self.assertEqual(plan["mode"], "trade")

# 并入自 tests/test_choice_score.py：发现/抉择：对手形态影响分数
DB = cards.load()


def _score(name: str, *, opp_fast: bool, effect: Effect | None = None) -> float:
    card = DB.by_name_or_id(name)
    b = Board([SideState(hero_hp=30, mana=10, max_mana=10), SideState(hero_hp=30)],
              5, 0, cards=DB)
    return policy.score_choice(card, effect or parse_effect(card), b, 0, 10,
                               opp_fast=opp_fast)[0]


class OpponentShapeTest(unittest.TestCase):
    def test_removal_scores_higher_against_aggro(self) -> None:
        fast = _score("火球术", opp_fast=True)
        slow = _score("火球术", opp_fast=False)
        self.assertAlmostEqual(fast - slow, 0.8)

    def test_heal_scores_higher_against_aggro(self) -> None:
        """回血/加甲这一行本来是对的，加用例防回归。"""
        fast = _score("治疗之触", opp_fast=True)
        slow = _score("治疗之触", opp_fast=False)
        self.assertAlmostEqual(fast - slow, 0.8)

    def test_unknown_card_is_not_rewarded(self) -> None:
        unknown = Effect(known=False, damage=6, scope="target")
        fast = _score("火球术", opp_fast=True, effect=unknown)
        slow = _score("火球术", opp_fast=False, effect=unknown)
        self.assertAlmostEqual(fast, slow)

if __name__ == "__main__":
    unittest.main()
