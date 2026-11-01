"""决策层的关键规则测试。

这些用例对应真实对局里出现过的错误判断，不是"覆盖率装饰"。
"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.model import Board, SideState, Unit


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


if __name__ == "__main__":
    unittest.main()
