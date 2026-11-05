"""状态层的局间隔离与口径：翻面标记、block_start 绑定、双方抽牌、回合归属。

这几条都属于「第一次启动永远是对的、第二局才开始错」的问题 ——
单局手测发现不了，只能靠断言把 reset() 的覆盖面钉住。
"""

from __future__ import annotations

import pathlib
import unittest

from hsai import cards, powerlog, state


class StateResetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = cards.load()
        self.st = state.GameState(self.db)
        self.st.our_id = 1

    # ---------------------------------------------------------------- 工具
    def _hand(self, eid: int, ctrl: int, card_id: str = "CORE_CS2_108") -> None:
        self.st.entities[eid] = state.Entity(
            id=eid, card_id=card_id, controller=ctrl, zone=state.ZONE_HAND
        )

    def _player(self, eid: int, pid: int) -> None:
        self.st.player_ids[pid] = eid
        self.st.entities[eid] = state.Entity(id=eid, controller=pid)

    def _draw(self, eid: int, value: int) -> None:
        self.st.apply(
            powerlog.Event(
                "tag_change",
                {"tag": "NUM_CARDS_DRAWN_THIS_TURN", "entity": eid, "value": value},
                "",
            )
        )

    # ---------------------------------------------------------------- 用例
    def test_reset_clears_id_by_hand_done(self) -> None:
        """翻面标记必须跨局清零 —— 否则第二局起这条兜底永远提前 return。"""
        self._hand(20, 2)                 # 只有玩家 2 的手牌可见
        self.st._fix_our_id_by_name()
        self.assertEqual(self.st.our_id, 2)
        self.assertTrue(self.st._id_by_hand_done)

        self.st.reset()
        self.assertFalse(self.st._id_by_hand_done)
        self.assertEqual(self.st.our_id, 1)

        # 同样的局面再来一次，还得能翻
        self._hand(20, 2)
        self.st._fix_our_id_by_name()
        self.assertEqual(self.st.our_id, 2)

    def test_block_start_binds_name(self) -> None:
        """block 的 controller 是「名字 → 玩家」的一条绑定路径。

        这条分支以前藏在第二个 `elif kind == "block_start"` 里，永远不可达，
        所以名字绑不上而且没有任何报错。
        """
        self.st._last_cp_name = "甲#1234"
        self.st.apply(
            powerlog.Event(
                "block_start",
                {"type": "PLAY", "entity": None, "ref": {"controller": 1}},
                "",
            )
        )
        self.assertEqual(self.st._name_player.get("甲#1234"), 1)

    def test_opponent_draws_are_not_our_draws(self) -> None:
        self._player(10, 1)
        self._player(11, 2)
        self._draw(10, 1)
        self._draw(11, 2)
        self.assertEqual(self.st.our_drawn, 1)
        self.assertEqual(self.st.opp_drawn, 2)
        # 对手牌库不能是「30 − 我方抽牌数」
        self.assertEqual(self.st.snapshot()["opp"]["deck_count"], 28)

    def test_draw_counters_do_not_eat_each_other(self) -> None:
        """双方共用一份「上次数值」会互相把对方的增量吃掉。"""
        self._player(10, 1)
        self._player(11, 2)
        self._draw(10, 1)
        self._draw(11, 2)
        self._draw(10, 2)                 # 我方这回合又抽了一张
        self.assertEqual(self.st.our_drawn, 2)
        self.assertEqual(self.st.opp_drawn, 2)

    def test_draw_without_controller_is_skipped(self) -> None:
        self.st.entities[30] = state.Entity(id=30)      # controller 未知
        self._draw(30, 3)
        self.assertEqual((self.st.our_drawn, self.st.opp_drawn), (0, 0))

    def test_duplicate_draw_pipeline_counts_once(self) -> None:
        """GameState / PowerTaskList 会把同一次抽牌各打一遍，中间的 0 是回落不是新段。

        修复前按「上次数值」算增量，1 → 0 → 1 会把同一张牌记两次，
        一局下来 our_drawn 能虚到 63（实际约 17）。
        """
        self._player(10, 1)
        self.st.turn = 1
        for value in (1, 1, 0, 1, 2, 2, 1):    # 两条管线交错 + 段间回落
            self._draw(10, value)
        self.assertEqual(self.st.our_drawn, 2)

        self.st.turn = 2                       # 换回合才重开一段
        for value in (0, 1, 1):
            self._draw(10, value)
        self.assertEqual(self.st.our_drawn, 3)

    def test_opp_deck_is_estimated_without_draw_tag(self) -> None:
        """对手玩家实体一条抽牌标签都没有时，牌库不能恒显示 30。

        退化成「30 − 手牌 − 场面」的粗估，并标注不可靠；opp_drawn 同步为
        估算的已抽张数，别让它恒为 0 被下游当成「对手一张没抽」。
        """
        self._player(10, 1)
        self._player(11, 2)
        # 对手手牌在日志里看不到 card_id，别给它卡面 —— 否则按可见手牌
        # 校正身份时会把「我方」翻到玩家 2，测的就不是对手那一侧了。
        for eid in (20, 21):
            self.st.entities[eid] = state.Entity(
                id=eid, controller=2, zone=state.ZONE_HAND)
        self.st.entities[30] = state.Entity(id=30, controller=2, zone=state.ZONE_PLAY)

        snap = self.st.snapshot()
        self.assertTrue(snap["opp"]["deck_estimated"])
        self.assertEqual(snap["opp"]["deck_count"], 27)     # 30 − 2 手牌 − 1 场面
        self.assertEqual(self.st.opp_drawn, 3)
        self.assertFalse(snap["our"]["deck_estimated"])

        # 真标签一到就丢掉粗估，不能把假数当起点继续累加
        self._draw(11, 2)
        self.assertEqual(self.st.opp_drawn, 2)
        self.assertFalse(self.st.snapshot()["opp"]["deck_estimated"])

    def test_our_drawn_never_exceeds_deck_size(self) -> None:
        """物理约束：疲劳为 0 时，抽到的张数不可能超过卡组张数。

        实测过 our_drawn=38 而卡组只有 30 张、牌库 0、疲劳 0 —— 重复计数
        把牌算穿了。同一 value 反复打印必须幂等，且计数要有硬上限。
        """
        self._player(10, 1)
        self.st.turn = 1
        for value in range(1, 41):
            for _ in range(3):                 # 两条管线 + 回落副本
                self._draw(10, value)
        self.assertEqual(self.st.deck_size, 30)
        self.assertLessEqual(self.st.our_drawn, self.st.deck_size)
        self.assertEqual(self.st.our_drawn, 30)
        self.assertEqual(self.st.snapshot()["our"]["deck_count"], 0)

    def test_snapshot_is_our_turn_uses_effective_owner(self) -> None:
        """日志里 turn_owner 经常是 None，直接比它会把「我方回合」判成对方回合。"""
        self.st.our_id, self.st.opp_id = 1, 2
        self.st.turn_owner = None
        self.st.first_player = 1
        self.st.turn = 3
        self.assertTrue(self.st.snapshot()["is_our_turn"])
        self.assertIn("我方回合", self.st.summary())

    def test_block_start_branch_is_unique(self) -> None:
        """同一个 `elif kind == "..."` 只能有一个：重复的那个静默不可达。"""
        src = pathlib.Path(state.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('elif kind == "block_start"'), 1)


if __name__ == "__main__":
    unittest.main()
