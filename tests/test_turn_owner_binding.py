"""回合归属：名字不能绑到错的人身上。

实测 2026-11-06 23:07 的对局（对手先手、第 1 回合没出牌）：
1. 留牌阶段对手的占位名「UNKNOWN HUMAN PLAYER」被当成玩家 2，占住了编号；
2. 真正的对手名字没法按「新名字只能是另一个人」推断，只能挂起；
3. 这个名字跨回合留到第 2 回合，我方第一张牌一打出就被 block_start 的
   controller=1 绑给了玩家 1 —— 两个名字都指向我方。
4. 之后对手回合的 CURRENT_PLAYER 全解析成「我方回合」，整局判反。
"""

from __future__ import annotations

import unittest

from hsai import cards, powerlog
from hsai.state import GameState


def _ev(kind: str, **data: object) -> powerlog.Event:
    return powerlog.Event(kind, dict(data), "")


def _current_player(name: str, value: int) -> powerlog.Event:
    return _ev("tag_change", tag="CURRENT_PLAYER", value=value,
               entity=None, ref={"name": name})


class TurnOwnerBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def setUp(self) -> None:
        self.st = GameState(self.db)
        self.st.our_id, self.st.opp_id = 1, 2
        self.st.turn = 1
        self.st._name_player = {"我#111": 1}
        self.st.apply(_ev("tag_change", tag="FIRST_PLAYER", value=1,
                          entity=None, ref={"name": "对手#999"}))

    def test_stale_name_is_not_bound_to_our_first_play(self) -> None:
        """上一回合没出牌留下的名字，不能被下一回合我方的第一个动作「认领」。"""
        self.st._last_cp_name = "对手#999"        # 上一回合挂起的对手名字
        self.st.turn = 2
        self.st.apply(_current_player("我#111", 1))   # 新回合：轮到我们
        self.st.apply(_ev("block_start", type="PLAY", entity=None,
                          ref={"controller": 1}))
        self.assertNotIn("对手#999", self.st._name_player)
        # 错绑会把它当成先手（1）；名字绑对时先手是对方
        self.assertEqual(self.st.first_player, 2)

    def test_same_turn_binding_still_works(self) -> None:
        """轮到谁、谁先出牌：这条正常的绑定路径不能被改坏。"""
        self.st._name_player = {}
        self.st.first_player = None
        self.st.apply(_current_player("对手#999", 1))
        self.assertEqual(self.st._last_cp_name, "对手#999")
        self.st.apply(_ev("block_start", type="PLAY", entity=None,
                          ref={"controller": 2}))
        self.assertEqual(self.st._name_player.get("对手#999"), 2)
        self.assertEqual(self.st.first_player, 2)

    def test_placeholder_player_name_is_not_bound(self) -> None:
        """「UNKNOWN HUMAN PLAYER」不是玩家，不能占掉玩家编号。"""
        self.st.apply(_ev("choices", player_name="UNKNOWN", refs=[
            {"controller": 2, "zone": 2},
        ]))
        self.assertNotIn("UNKNOWN", self.st._name_player)

    def test_real_player_name_is_bound_from_choices(self) -> None:
        self.st.apply(_ev("choices", player_name="我#111", refs=[
            {"controller": 1, "zone": 2},
        ]))
        self.assertEqual(self.st._name_player.get("我#111"), 1)

    def test_first_player_is_inferred_from_first_player_name(self) -> None:
        """FIRST_PLAYER 给的是对手的名字 → 先手是对方，不用等谁先出牌。"""
        self.assertEqual(self.st.first_player, 2)

    def test_first_player_is_us_when_name_matches(self) -> None:
        st = GameState(self.db)
        st.our_id, st.opp_id = 1, 2
        st._name_player = {"我#111": 1}
        st.apply(_ev("tag_change", tag="FIRST_PLAYER", value=1,
                     entity=None, ref={"name": "我#111"}))
        self.assertEqual(st.first_player, 1)

    def test_opponent_turn_is_not_mistaken_for_ours(self) -> None:
        self.st._name_player = {"我#111": 1, "对手#999": 2}
        self.st.first_player = 2
        self.st.turn = 3
        self.st.apply(_current_player("我#111", 0))
        self.st.apply(_current_player("对手#999", 1))
        self.assertEqual(self.st.effective_turn_owner(), 2)
        self.assertFalse(self.st.snapshot()["is_our_turn"])


if __name__ == "__main__":
    unittest.main()
