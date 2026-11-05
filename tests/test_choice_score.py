"""发现/抉择打分的第 6 层：对手形态必须真的影响分数。

修复前那行条件表达式恒为 False（`Effect` 没有 `taunt` 字段），
快攻对局的「解场/回血加分」从来没生效过。
"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.model import Board, Effect, SideState, parse_effect

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
