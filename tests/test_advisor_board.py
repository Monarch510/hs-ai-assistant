"""`board_from_state` 的口径：武器只建一次、牌库 0 张不是未知。

对应 00-现状 §5.14 / §6：同一函数里换个行号重写同一个字段，
前一段白写；`x or 默认值` 会把疲劳期的 0 张当成「没数据」。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from hsai import cards
from hsai.advisor import board_from_state
from hsai.state import ZONE_PLAY, Entity, GameState

ADVISOR_SRC = Path(__file__).resolve().parent.parent / "hsai" / "advisor.py"


def _state_with_weapon(durability: int = 3) -> GameState:
    st = GameState(cards.load())
    st.turn = 3
    st.turn_owner = 1
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[20] = Entity(20, card_id="HERO_01", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "HEALTH": 30})
    st.entities[30] = Entity(30, card_id="CS2_106", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "DURABILITY": durability})
    return st


class WeaponTest(unittest.TestCase):
    def test_weapon_keeps_durability(self) -> None:
        b = board_from_state(_state_with_weapon(3))
        self.assertIsNotNone(b.sides[0].weapon)
        self.assertEqual(b.sides[0].weapon.hp, 3)       # 修复前被第二次构造写成 1
        self.assertEqual(b.sides[0].weapon.max_hp, 3)

    def test_weapon_is_built_once(self) -> None:
        """静态防回归：同一个字段在同一个函数里只该赋值一次。"""
        src = ADVISOR_SRC.read_text(encoding="utf-8")
        body = src[src.index("def board_from_state"):]
        body = body[:body.index("\ndef ", 1)]
        self.assertEqual(body.count("me.weapon = Unit("), 1)


class DeckCountTest(unittest.TestCase):
    def test_empty_deck_is_not_unknown(self) -> None:
        """抽完 30 张（疲劳期）时牌库就是 0 张，不能再凭空补 30 张。"""
        st = _state_with_weapon()
        st.our_drawn = 30
        b = board_from_state(st)
        self.assertEqual(b.sides[0].deck, [])


if __name__ == "__main__":
    unittest.main()
