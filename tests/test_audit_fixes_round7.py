"""OCR 审查中危项回归：替身窗口判定、复盘回合数、key 模式三窗透明。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards

from tests import _ui_support
from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
import hsai.ui_ai as ui_ai_mod  # noqa: E402
import hsai.ai as ai_mod        # noqa: E402
from hsai.ui import BG, Overlay  # noqa: E402


class StubToplevelTest(unittest.TestCase):
    """替身里 Toplevel 是独立顶层窗，不能顺着 master 走到 Tk 根。"""

    def test_body_of_satellite_resolves_to_its_own_toplevel(self) -> None:
        root = _ui_support.Tk()
        top = _ui_support.Toplevel(root)
        body = _ui_support.Frame(top)
        self.assertIs(body.winfo_toplevel(), top)
        self.assertIsNot(body.winfo_toplevel(), root)
        # 左窗本体仍归 root
        self.assertIs(_ui_support.Frame(root).winfo_toplevel(), root)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r7-")
        self.addCleanup(tmp.cleanup)
        self.db = cards.load()
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", Path(tmp.name) / "window.json"),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", Path(tmp.name) / "advice.jsonl"),
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ui_mod.ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._maybe_ai = lambda *a, **k: None
        self.ov._update = lambda *a, **k: None
        self.addCleanup(lambda: self.ov.quit())


class ReviewTurnsTest(_Case):
    def test_summary_uses_turns_from_snapshot_not_cleared_advisor(self) -> None:
        # _finish_game 已清空 _game_turns，快照里带着清空前的回合数
        self.ov.advisor._game_turns = []
        snap = {
            "phase": "game_over", "turn": 9, "our_turns": 7,
            "our": {"class_cn": "法师"}, "opp": {"class_cn": "战士"},
            "deck": {"name": "测试卡组"}, "prediction": {"name": "测试预判"},
            "review": [], "issues": [], "result": "win",
        }
        with mock.patch.object(ai_mod, "review", return_value=([], [])) as review:
            self.ov._ai_review_worker(snap)
        summary = review.call_args.args[1]
        self.assertIn("记录了 7 个我方回合", summary)

    def test_summary_falls_back_to_advisor_when_snapshot_lacks_turns(self) -> None:
        self.ov.advisor._game_turns = [1, 2, 3]
        snap = {"phase": "game_over", "turn": 3, "review": [], "issues": []}
        with mock.patch.object(ai_mod, "review", return_value=([], [])) as review:
            self.ov._ai_review_worker(snap)
        self.assertIn("记录了 3 个我方回合", review.call_args.args[1])


class KeyModeTransparencyTest(_Case):
    def test_key_mode_applies_to_both_windows(self) -> None:
        with mock.patch.object(self.ov.root, "attributes") as root_attr:
            patches = [mock.patch.object(p["win"], "attributes") for p in self.ov._panes.values()]
            mocks = [p.start() for p in patches]
            self.addCleanup(lambda: [p.stop() for p in patches])
            self.ov.bg_mode = "key"
            self.ov._apply_bg_mode()
        root_attr.assert_any_call("-transparentcolor", BG)
        self.assertTrue(mocks, "得有卫星窗才验得到透明联动")
        for m in mocks:
            m.assert_any_call("-transparentcolor", BG)

    def test_solid_mode_clears_transparency_on_both_windows(self) -> None:
        with mock.patch.object(self.ov.root, "attributes") as root_attr:
            patches = [mock.patch.object(p["win"], "attributes") for p in self.ov._panes.values()]
            mocks = [p.start() for p in patches]
            self.addCleanup(lambda: [p.stop() for p in patches])
            self.ov.bg_mode = "solid"
            self.ov._apply_bg_mode()
        root_attr.assert_any_call("-transparentcolor", "")
        self.assertTrue(mocks, "得有卫星窗才验得到透明联动")
        for m in mocks:
            m.assert_any_call("-transparentcolor", "")


if __name__ == "__main__":
    unittest.main()
