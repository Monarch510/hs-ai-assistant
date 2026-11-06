"""复盘「有斩杀没斩」不能把已经赢下的最后一回合算进去。

背景：击杀和 PLAYSTATE=WON 常常落在同一批日志里。等下一帧 snapshot 看到时
phase 已经是 game_over，`_track_turn` 以前第一行就 return，`killed` 永远补不上，
于是每一局赢下来的最后一回合都会被复盘写成「有斩杀没斩」。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hsai import advisor as advisor_mod
from hsai import cards, review
from hsai.advisor import Advisor


def _side(hp: int, class_en: str = "MAGE") -> SimpleNamespace:
    return SimpleNamespace(health=hp, armor=0, mana=0, hand=[], hand_count=0,
                           class_en=class_en, board=[])


class _State:
    """只提供 _track_turn / _finish_game 会读的字段，避免造整局日志。"""

    def __init__(self, *, winner: int = 1, our_hp: int = 2, opp_hp: int = 0,
                 owner: int = 1, turn: int = 9) -> None:
        self.winner = winner
        self.our_id = 1
        self.turn = turn
        self.phase = "game_over"
        self.game_over = True
        self.unmodelled: list[str] = []
        self._our_hp = our_hp
        self._opp_hp = opp_hp
        self._owner = owner

    def effective_turn_owner(self) -> int:
        return self._owner

    def sides(self):
        return _side(self._our_hp), _side(self._opp_hp)


class MissedLethalOnWinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def setUp(self) -> None:
        # 放工作区里（沙箱对系统临时目录的清理会报 WinError 5）；
        # 用固定前缀的临时目录，清理失败也不影响结果
        tmp = tempfile.mkdtemp(prefix="_test-review-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patcher = mock.patch.object(advisor_mod, "LAST_REVIEW_JSON",
                                    Path(tmp) / "last_review.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.records: list[dict] = []
        rec_patcher = mock.patch.object(review, "append_game",
                                        lambda rec, path=None: self.records.append(rec.as_dict()))
        rec_patcher.start()
        self.addCleanup(rec_patcher.stop)
        self.adv = Advisor(self.db)
        self.adv._record_result = lambda *a, **k: None  # type: ignore[method-assign]

    def _finish(self, st: _State) -> list[str]:
        # 击杀发生在第 9 回合；等 snapshot 看到时已经是 game_over（原来的 bug 场景）
        self.adv._turn_rec = review.TurnRecord(turn=9, lethal_available=True, lethal_damage=22)
        self.adv._track_turn(st, {"lethal": True, "lethal_damage": 22, "lines": []}, None)
        self.adv._finish_game(st, won=st.winner == 1)
        return list(self.adv.last_findings)

    def test_win_on_our_turn_with_kill_is_not_missed_lethal(self) -> None:
        findings = self._finish(_State(winner=1, our_hp=2, opp_hp=0, owner=1))
        self.assertFalse(any("有斩杀没斩" in x for x in findings), findings)
        self.assertTrue(self.records and self.records[0]["turns"][0]["killed"])

    def test_concede_without_kill_still_reports_missed_lethal(self) -> None:
        """对面没死就投了：这一回合确实没把斩杀打出去，复盘要保留。"""
        findings = self._finish(_State(winner=1, our_hp=5, opp_hp=5, owner=1))
        self.assertTrue(any("有斩杀没斩" in x for x in findings), findings)

    def test_loss_on_opponent_turn_still_reports_missed_lethal(self) -> None:
        findings = self._finish(_State(winner=2, our_hp=0, opp_hp=8, owner=2))
        self.assertTrue(any("有斩杀没斩" in x for x in findings), findings)


if __name__ == "__main__":
    unittest.main()
