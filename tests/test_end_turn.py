"""「结束回合」的感知：真实日志里 GameEntity 的 NEXT_STEP=MAIN_END。

留牌流程也会走一次 MAIN_END，GameState / PowerTaskList 还会各打一遍，
所以状态层必须同时挡住这两类误报。界面要在回合归属切换之前
（比 TURN / CURRENT_PLAYER 早到）就撤掉上一回合的建议。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, powerlog as pl, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，别整组跳过

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai import ai as ai_mod   # noqa: E402
from hsai.ui import Overlay     # noqa: E402


def _line(value: str, source: str = "GameState.DebugPrintPower") -> str:
    return (f"D 12:16:40.7652036 {source}() -     "
            f"TAG_CHANGE Entity=GameEntity tag=NEXT_STEP value={value} ")


def _feed(st: state.GameState, value: str,
          source: str = "GameState.DebugPrintPower") -> None:
    ev = pl.parse_line(_line(value, source))
    assert ev is not None, value
    st.apply(ev)


def _playing(turn: int = 5) -> state.GameState:
    st = state.GameState(cards.CardDB([]))
    st.game_started = True
    st.mulligan_done = True
    st.phase = "playing"
    st.turn = turn
    return st


def _texts(widget) -> list[str]:
    """递归收齐控件树里的文本；替身和真 tkinter 都要能用。"""
    out: list[str] = []
    for c in widget.winfo_children():
        opts = getattr(c, "_opts", None)
        t = opts.get("text") if isinstance(opts, dict) else None
        if not isinstance(t, str):
            try:
                t = c.cget("text")
            except Exception:
                t = None
        if isinstance(t, str):
            out.append(t)
        out.extend(_texts(c))
    return out


class EndTurnStateTest(unittest.TestCase):
    def test_mulligan_main_end_is_not_a_turn_end(self) -> None:
        st = _playing()
        st.phase = "mulligan"
        _feed(st, "MAIN_END")
        self.assertEqual(st.end_turn_seq, 0)

    def test_two_sources_count_once(self) -> None:
        st = _playing()
        _feed(st, "MAIN_END")
        _feed(st, "MAIN_END", "PowerTaskList.DebugPrintPower")
        self.assertEqual(st.end_turn_seq, 1)

    def test_next_turn_counts_again(self) -> None:
        st = _playing()
        _feed(st, "MAIN_END")
        st.turn = 6
        _feed(st, "MAIN_END")
        self.assertEqual(st.end_turn_seq, 2)

    def test_game_over_is_not_a_turn_end(self) -> None:
        st = _playing()
        st.phase = "game_over"
        _feed(st, "MAIN_END")
        self.assertEqual(st.end_turn_seq, 0)

    def test_reset_clears(self) -> None:
        st = _playing()
        _feed(st, "MAIN_END")
        st.reset()
        self.assertEqual(st.end_turn_seq, 0)
        self.assertIsNone(st.next_step)

    def test_next_step_follows_log(self) -> None:
        st = _playing()
        _feed(st, "MAIN_END")
        self.assertEqual(st.next_step, "MAIN_END")
        _feed(st, "MAIN_READY")
        self.assertEqual(st.next_step, "MAIN_READY")

    def test_new_turn_clears_ending_state(self) -> None:
        """日志缺下一条 NEXT_STEP 时，回合号变化也要解除结束阶段。"""
        st = _playing()
        _feed(st, "MAIN_END")
        ev = pl.parse_line(
            "D 12:16:41.0000000 GameState.DebugPrintPower() - "
            "TAG_CHANGE Entity=1 tag=TURN value=6 ")
        assert ev is not None
        st.apply(ev)
        self.assertEqual(st.turn, 6)
        self.assertIsNone(st.next_step)


class EndTurnUiTest(unittest.TestCase):
    def setUp(self) -> None:
        log_patch = mock.patch.object(ui_mod.logutil, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        # _render 会记一条 advice_log，改道到临时目录别碰 data/
        tmp = tempfile.TemporaryDirectory(prefix="hsai-end-turn-")
        self.addCleanup(tmp.cleanup)
        log_path = mock.patch.object(ui_mod, "ADVICE_LOG_JSONL",
                                     Path(tmp.name) / "advice_log.jsonl")
        log_path.start()
        self.addCleanup(log_path.stop)

        ai_ready = mock.patch.object(ai_mod, "is_ready", return_value=False)
        ai_ready.start()
        self.addCleanup(ai_ready.stop)

        self.db = cards.CardDB([])
        self.st = _playing()
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass

    def _snap(self, **kw) -> dict:
        snap = {
            "phase": "playing", "turn": 5, "turn_known": True,
            "is_our_turn": True,
            "advice": {"lines": [{"title": "先打脸"}], "lethal": False, "defense": []},
            "end_turn_seq": 0, "turn_ending": False,
        }
        snap.update(kw)
        return snap

    def test_snapshot_exposes_signal(self) -> None:
        _feed(self.st, "MAIN_END")
        snap = self.ov.advisor.snapshot(self.st)
        self.assertEqual(snap["end_turn_seq"], 1)
        self.assertTrue(snap["turn_ending"])

    def test_ending_hides_advice_and_shows_hint(self) -> None:
        self.ov._render(self._snap())
        self.assertIn("先打脸", _texts(self.ov.root))
        self.ov._render(self._snap(end_turn_seq=1, turn_ending=True))
        texts = _texts(self.ov.root)
        self.assertIn("回合已结束", texts)
        self.assertNotIn("先打脸", texts)

    def test_hint_disappears_on_next_turn(self) -> None:
        self.ov._render(self._snap(end_turn_seq=1, turn_ending=True))
        self.ov._render(self._snap(end_turn_seq=1, turn_ending=False, turn=6))
        texts = _texts(self.ov.root)
        self.assertNotIn("回合已结束", texts)
        self.assertIn("先打脸", texts)

    def test_ending_does_not_start_ai(self) -> None:
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ai_mod, "load_config") as cfg:
            self.ov._maybe_ai(self._snap(turn_ending=True), self.st)
        cfg.assert_not_called()
