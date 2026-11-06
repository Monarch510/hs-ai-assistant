"""结算帧不能把上一帧的建议重算掉。

实测最后一局：最后一帧（game_over）会拿对手已经 0 血的场面重算，把
「可直接斩杀（22 点伤害）」换成「打出 灭世信徒」——用户看到的正是这一帧，
于是以为「能斩杀却没有斩杀提示」。复盘记录也依赖同一份 advice。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor as advisor_mod
from hsai import cards, review
from hsai.advisor import Advisor
from hsai.state import ZONE_PLAY, Entity, GameState

DB = cards.load()


def _state() -> GameState:
    st = GameState(DB)
    st.turn = 9
    st.turn_owner = 1
    st.phase = "playing"
    st.game_started = True
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10, 2: 11}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[11] = Entity(11, tags={"PLAYER_ID": 2, "HERO_ENTITY": 21})
    st.entities[20] = Entity(20, card_id="HERO_10", controller=1, zone=ZONE_PLAY,
                             tags={"HEALTH": 30})
    st.entities[21] = Entity(21, card_id="HERO_10", controller=2, zone=ZONE_PLAY,
                             tags={"HEALTH": 2})
    # 场上一只 3 攻随从，对面 2 血：这回合能斩杀
    st.entities[30] = Entity(30, card_id="CS2_120", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "HEALTH": 3, "ATTACKABLE": 1})
    return st


class AdviceOnGameOverTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="_test-advice-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patcher = mock.patch.object(advisor_mod, "LAST_REVIEW_JSON",
                                    Path(tmp) / "last_review.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        rec_patcher = mock.patch.object(review, "append_game", lambda *a, **k: None)
        rec_patcher.start()
        self.addCleanup(rec_patcher.stop)
        self.adv = Advisor(DB)
        self.adv._record_result = lambda *a, **k: None  # type: ignore[method-assign]

    def test_game_over_keeps_last_advice(self) -> None:
        st = _state()
        playing = self.adv.snapshot(st)
        self.assertTrue((playing.get("advice") or {}).get("lethal"), playing.get("advice"))

        # 伤害先结算、PLAYSTATE 还没到的那一帧：对面已经 0 血，但 phase 还是 playing
        st.entities[21].tags["DAMAGE"] = 2
        mid = self.adv.snapshot(st)
        self.assertTrue((mid.get("advice") or {}).get("lethal"),
                        "对面已死的那一帧把斩杀建议重算掉了")

        # 击杀结算：phase 变成 game_over
        st.phase = "game_over"
        st.game_over = True
        st.winner = 1
        over = self.adv.snapshot(st)

        self.assertTrue((over.get("advice") or {}).get("lethal"),
                        "结算帧把斩杀建议重算掉了")
        self.assertEqual(
            [x.get("title") for x in over["advice"].get("lines") or []],
            [x.get("title") for x in playing["advice"].get("lines") or []],
        )
        self.assertFalse(any("有斩杀没斩" in x for x in over.get("review") or []),
                         over.get("review"))


if __name__ == "__main__":
    unittest.main()
