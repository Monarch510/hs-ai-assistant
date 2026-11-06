"""对手牌库口径：疲劳期 0 张不能被当成「没数据」，40 张卡组兜底不能写死 30。

离线构造：空 CardDB + 内存 GameState，meta 指到临时目录；
不调用 cards.load()（会触发下载），不写 data/。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor as advisor_mod
from hsai import cards, state


class DeckCountTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-deck-count-")
        self.addCleanup(tmp.cleanup)
        db = cards.CardDB([])
        self.adv = advisor_mod.Advisor(db, meta_path=Path(tmp.name))
        self.st = state.GameState(db)
        self.st.our_id, self.st.opp_id = 1, 2
        # 有 opp 键才不会走「卡组 − 手牌 − 场面」粗估，把 opp_drawn 冲掉
        self.st._draw_seen = {"opp": {}}

    def _fresh_sides(self, *, turn: int, deck_size: int, opp_drawn: int):
        self.st.turn = turn
        self.st.deck_size = deck_size
        self.st.opp_drawn = opp_drawn
        return self.st.sides()

    def _captured(self, opp) -> int:
        """返回 _predict 真正交给 predictor.predict 的 deck_count。"""
        with mock.patch.object(self.adv.predictor, "predict",
                               return_value=None) as pred:
            self.adv._predict(self.st, opp)
        return pred.call_args.kwargs["deck_count"]

    def test_fatigue_zero_is_not_unknown(self) -> None:
        """T25、对手牌库真实 0 张：不能退化成 30 − 25 = 5。"""
        _, opp = self._fresh_sides(turn=25, deck_size=30, opp_drawn=30)
        self.assertEqual(opp.deck_count, 0)
        self.assertEqual(self._captured(opp), 0)

    def test_40_card_deck_fallback(self) -> None:
        """deck_count 为 None 时按卡组张数兜底，不是写死的 30。"""
        _, opp = self._fresh_sides(turn=12, deck_size=40, opp_drawn=0)
        opp.deck_count = None
        self.assertEqual(self._captured(opp), 28)       # 40 − 12，不是 30 − 12 = 18
        src = Path(advisor_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("30 - st.turn", src)           # 兜底不许再写死 30

    def test_real_value_wins_over_estimate(self) -> None:
        """有真实读数就用真实值。deck_size=40 让兜底 28 与真实 18 分得开，
        避开任务书例子里 30 − 12 恰好也等于 18 的巧合。"""
        _, opp = self._fresh_sides(turn=12, deck_size=40, opp_drawn=22)
        self.assertEqual(opp.deck_count, 18)
        self.assertEqual(self._captured(opp), 18)

    def test_our_fatigue_deck_placeholder_is_zero(self) -> None:
        """我方疲劳时占位牌数为 0，不能被兜底补成 30 张。"""
        self.st.deck_size = 30
        self.st.our_drawn = 30
        ours, _ = self.st.sides()
        self.assertEqual(ours.deck_count, 0)
        board = advisor_mod.board_from_state(self.st)
        self.assertEqual(len(board.sides[0].deck), 0)


if __name__ == "__main__":
    unittest.main()
