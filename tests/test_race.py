"""抢血 / 解场判定的测试。"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.model import Board, SideState, Unit


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

    def test_aggression_coefficient(self) -> None:
        aggro = [self.db.by_name_or_id("火羽精灵")] * 10
        slow = [self.db.by_name_or_id("克苏恩，帝国之眼")] * 10
        fast, name_fast = policy.aggression_coefficient(aggro, self.db)
        slowc, name_slow = policy.aggression_coefficient(slow, self.db)
        self.assertGreater(fast, slowc)
        self.assertEqual(name_fast, "快攻")
        self.assertEqual(name_slow, "控制")


if __name__ == "__main__":
    unittest.main()
