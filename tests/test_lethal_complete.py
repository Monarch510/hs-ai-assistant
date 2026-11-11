"""斩杀估算要和 apply_action 同一口径：场上法伤、英雄攻击、武器次数。

这些用例对应 00-现状 §2.3 / §3.5 的缺口：漏算任何一项都会「理论能斩却判不能」。
"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.model import Board, SideState, Unit

DB = cards.load()


def _board(opp_hp: int = 30, mana: int = 10,
           hero_atk: int = 0, hero_attacks_left: int = 0) -> Board:
    return Board([SideState(hero_hp=30, mana=mana, max_mana=mana,
                            hero_atk=hero_atk, hero_attacks_left=hero_attacks_left),
                  SideState(hero_hp=opp_hp)], 9, 0, cards=DB)


class SpellDamageTest(unittest.TestCase):
    def test_field_spell_damage_adds_to_face_spell(self) -> None:
        b = _board()
        b.sides[0].board.append(Unit("", "法伤随从", 0, 2, spell_damage=1))
        b.sides[0].board.append(Unit("", "法伤随从", 0, 2, spell_damage=1))
        b.sides[0].hand = [DB.by_name_or_id("火球术")]
        r = policy.max_face_damage(b, 0, DB)
        self.assertEqual(r["spell"], 8)          # 6 + 1 + 1，修复前是 6

    def test_spell_matches_apply_action(self) -> None:
        """同一个数不能有两套算法：斩杀里的 spell 要等于模拟结算掉的血。"""
        b = _board()
        b.sides[0].board.append(Unit("", "法伤随从", 0, 2, spell_damage=1))
        b.sides[0].hand = [DB.by_name_or_id("火球术")]
        r = policy.max_face_damage(b, 0, DB)
        act = next(a for a in policy.legal_actions(b, 0, DB)
                   if a.kind == "play" and "火球术" in a.desc and "打脸" in a.desc)
        after = policy.apply_action(b, 0, act, DB)
        self.assertEqual(30 - after.sides[1].hero_hp, r["spell"])


class HeroAttackTest(unittest.TestCase):
    def test_hero_attack_counts_toward_lethal(self) -> None:
        b = _board(opp_hp=3, hero_atk=3, hero_attacks_left=1)
        r = policy.max_face_damage(b, 0, DB)
        self.assertEqual(r["attack"], 3)         # 修复前英雄攻击完全不算
        self.assertTrue(r["lethal"])

    def test_windfury_hero_swings_twice_without_double_counting_weapon(self) -> None:
        b = _board(hero_atk=2, hero_attacks_left=2)
        b.sides[0].weapon = Unit("", "风怒武器", 2, 2, windfury=True)
        r = policy.max_face_damage(b, 0, DB)
        self.assertEqual(r["attack"], 4)         # 2 攻 × 2 次，不是 2 + 2 + 2

    def test_hero_that_already_attacked_adds_nothing(self) -> None:
        b = _board(hero_atk=3, hero_attacks_left=0)
        b.sides[0].weapon = Unit("", "武器", 2, 2)
        r = policy.max_face_damage(b, 0, DB)
        self.assertEqual(r["attack"], 0)         # 0 次是「打过了」，不是「未知」
        self.assertFalse(r["lethal"])




if __name__ == "__main__":
    unittest.main()
