"""第六轮审计：加费铺垫与超级风怒授予者的回归用例。

两条都是只读审查里用隔离探针坐实的问题，先写测试看红再改源码。
构造走真卡库 cards.load()，不碰 data/ 产物。
"""

from __future__ import annotations

import unittest

from hsai import cards, policy
from hsai.cards import Card, CardDB, windfury_attacks
from hsai.model import Board, SideState, Unit

REAL: CardDB = cards.load()


def _board(me: SideState, foe: SideState, db: CardDB = REAL) -> Board:
    return Board([me, foe], turn=9, active=0, cards=db)


class PreludeManaTest(unittest.TestCase):
    """加费牌只能拿「净赚法力」的当铺垫，别把本可斩杀的线路挤掉。"""

    def test_growth_does_not_block_direct_lethal(self) -> None:
        # CS2_013 野性成长 cost=2/+1mana（净 -1）；火球 cost=4/6 伤
        growth = REAL.get("CS2_013")
        fireball = Card(id="FB", name="火球术", cost=4, type="SPELL", text="造成6点伤害")
        self.assertIsNotNone(growth)
        me = SideState(hero_hp=30, mana=4, board=[], hand=[growth, fireball])
        foe = SideState(hero_hp=6, board=[])
        b = _board(me, foe)

        seq = policy._lethal_sequence(b, 0, REAL)
        self.assertIsNotNone(seq, "4 费直接火球就能斩，不该被野性成长铺垫挤掉")
        self.assertEqual(seq[0].cost, 4, "第一手应当是火球，不是 2 费的野性成长")
        self.assertTrue(policy.plan_turn(b, REAL)["lethal"]["lethal"])

    def test_coin_prelude_still_works(self) -> None:
        # 幸运币 cost=0/+1mana（净 +1）：仍然要先打，否则火球买不起
        coin = Card(id="COIN", name="幸运币", cost=0, type="SPELL",
                    text="获得一个空的法力水晶")
        fireball = Card(id="FB", name="火球术", cost=4, type="SPELL", text="造成6点伤害")
        me = SideState(hero_hp=30, mana=3, board=[], hand=[coin, fireball])
        foe = SideState(hero_hp=6, board=[])
        seq = policy._lethal_sequence(_board(me, foe), 0, REAL)
        self.assertIsNotNone(seq)
        self.assertEqual(seq[0].cost, 0, "幸运币是净赚法力，铺垫仍应在前")


class WindfuryGrantorTest(unittest.TestCase):
    """把超级风怒赋予别人的牌，自己不该被当成 4 段。"""

    def test_real_grantors_are_not_four_swings(self) -> None:
        # DAL_742 光环「你的风怒随从拥有超级风怒」
        # CATA_564 战吼「使一个友方随从获得超级风怒，且该随从无法攻击英雄」
        for cid in ("DAL_742", "CATA_564"):
            card = REAL.get(cid)
            self.assertIsNotNone(card)
            self.assertEqual(windfury_attacks(card), 1, cid)

    def test_self_mega_windfury_cards_stay_four(self) -> None:
        for cid in ("DRGA_005", "GVG_111t", "WW_382", "DRGA_24w", "LOOTA_821"):
            card = REAL.get(cid)
            self.assertIsNotNone(card)
            self.assertEqual(windfury_attacks(card), 4, cid)

    def test_playing_grantor_does_not_give_it_four_attacks(self) -> None:
        grantor = Card(id="GRANT", name="暴走旋风", cost=3, type="MINION",
                       atk=3, health=3,
                       text="你的<b>风怒</b>随从拥有<b>超级风怒</b>")
        me = SideState(hero_hp=30, mana=10, board=[], hand=[grantor])
        foe = SideState(hero_hp=30, board=[])
        b = _board(me, foe)
        nb = policy.apply_action(b, 0, policy.Action("play", card_idx=0, cost=3), REAL)
        u = nb.sides[0].board[0]
        self.assertFalse(u.mega_windfury)
        self.assertEqual(u.max_attacks, 1)
        self.assertEqual(nb.sides[0].board_attack, 3)


if __name__ == "__main__":
    unittest.main()
