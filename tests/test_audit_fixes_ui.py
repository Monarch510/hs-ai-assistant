"""A 组修复的复现测试：多局复盘生命周期 / _log_advice / _load_state / hud_row。

跟 tests/test_end_turn.py 一样用 tests/_ui_support.py 的 tkinter 替身桩，
不碰真实 data/：ADVICE_LOG_JSONL / STATE_PATH 全部改道到临时目录。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，并把 window.json 改道到临时目录

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
import hsai.ui_ai as ui_ai_mod  # noqa: E402
import hsai.ui_state as ui_state_mod  # noqa: E402
from hsai import ai as ai_mod   # noqa: E402
from hsai import ui_text        # noqa: E402
from hsai.ui import Overlay     # noqa: E402


def _snap(**kw) -> dict:
    snap = {
        "phase": "playing", "turn": 3, "turn_known": True, "is_our_turn": True,
        "actions": [],
        "advice": {"lines": [{"title": "先打脸", "reason": "能斩"}],
                   "lethal": False, "defense": []},
        "our": {"mana": 5, "hand": [{"name": "火球术", "cost": 4}], "board": []},
        "opp": {"board": []},
        "end_turn_seq": 0, "turn_ending": False,
    }
    snap.update(kw)
    return snap


def _thread_targets(fake_threading) -> list:
    return [c[1].get("target") for c in fake_threading.Thread.call_args_list]


class _OverlayTestCase(unittest.TestCase):
    """构造一个不开事件循环的 Overlay，渲染类方法替成空操作隔离被测逻辑。"""

    def setUp(self) -> None:
        log_patch = mock.patch.object(ui_mod.logutil, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-ui-")
        self.addCleanup(tmp.cleanup)
        self._advice_log = Path(tmp.name) / "advice_log.jsonl"
        log_path = mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", self._advice_log)
        log_path.start()
        self.addCleanup(log_path.stop)

        ready = mock.patch.object(ai_mod, "is_ready", return_value=False)
        ready.start()
        self.addCleanup(ready.stop)

        self.db = cards.CardDB([])
        self.st = state.GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._render = lambda *a, **k: None
        self.ov._sync_bar = lambda *a, **k: None
        self.ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass


class ReviewLifecycleTest(_OverlayTestCase):
    """缺陷 1 + G1：第二局 mulligan 必须把 _ai_reviewed 清零，复盘线程能再调度。"""

    def test_ai_reviewed_reset_and_worker_rescheduled_between_games(self) -> None:
        ov = self.ov
        # 第一局 game_over：复盘线程起一次，标志置 True
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ui_ai_mod, "threading") as fake_threading:
            ov._maybe_ai(_snap(phase="game_over", turn=9))
        self.assertTrue(ov._ai_reviewed, "第一局复盘后标志应为 True")
        self.assertIn(ov._ai_review_worker, _thread_targets(fake_threading),
                      "第一局应调度复盘线程")

        # 第二局回到 mulligan：_update 的重置块要把标志清零
        with mock.patch.object(ov.advisor, "snapshot",
                               return_value=_snap(phase="mulligan", turn=1)):
            ov._update(self.st)
        self.assertFalse(ov._ai_reviewed, "第二局 mulligan 应把 _ai_reviewed 清零")

        # 第二局 game_over：复盘线程必须能再次调度，否则 lessons/tunings 静默断更
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ui_ai_mod, "threading") as fake_threading2:
            ov._maybe_ai(_snap(phase="game_over", turn=9))
        self.assertIn(ov._ai_review_worker, _thread_targets(fake_threading2),
                      "第二局复盘线程应能再次调度")
        self.assertTrue(ov._ai_reviewed)


class LogAdviceTest(_OverlayTestCase):
    """缺陷 2：_log_advice 不能整桶覆盖掉 _update 先写进来的 actions。"""

    def test_keeps_actions_and_advice(self) -> None:
        ov = self.ov
        turn = 3
        ov._advice_by_turn[turn] = {"actions": ["火球术打脸"]}
        ov._log_advice(_snap(turn=turn))

        bucket = ov._advice_by_turn[turn]
        self.assertEqual(bucket.get("actions"), ["火球术打脸"],
                         "actions 不能被整桶覆盖吞掉")
        self.assertEqual(bucket.get("advice"), ["先打脸"])
        self.assertEqual(bucket.get("mana"), 5)

        rows = self._advice_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1, "建议确实落盘（已改道到临时目录）")
        self.assertEqual(json.loads(rows[0])["turn"], turn)


class LoadStateGuardTest(unittest.TestCase):
    """缺陷 3：两份 _load_state 对顶层非 dict 的 window.json 都要兜底成 {}。"""

    def test_non_dict_top_level_returns_empty(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-state-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        for content in ("null", "[]", '"x"', "123", "true"):
            path.write_text(content, encoding="utf-8")
            with mock.patch.object(ui_mod, "STATE_PATH", path):
                self.assertEqual(ui_mod._load_state(), {}, f"ui: {content}")
                self.assertEqual(ui_state_mod._load_state(), {}, f"ui_state: {content}")

    def test_valid_dict_still_returned(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-state-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        data = {"layout": 7, "x": 1}
        path.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(ui_mod, "STATE_PATH", path):
            self.assertEqual(ui_mod._load_state(), data)
            self.assertEqual(ui_state_mod._load_state(), data)


class HudRowDeckUnknownTest(unittest.TestCase):
    """G5：deck_count=None 显示「未知」，且别弄坏粗估标记。"""

    def test_deck_none_shows_unknown(self) -> None:
        side = {"health": 30, "hand_count": 3, "deck_count": None, "board": []}
        row = ui_text.hud_row(side, "对手")
        self.assertIn("牌库 未知", row)
        self.assertNotIn("None", row)

    def test_estimated_marker_preserved(self) -> None:
        side = {"health": 30, "hand_count": 3, "deck_count": 22,
                "deck_estimated": True, "board": []}
        row = ui_text.hud_row(side, "对手")
        self.assertIn("牌库 约22", row, "不能弄坏 tests/test_rush_face.py:83-84 的断言")


if __name__ == "__main__":
    unittest.main()
