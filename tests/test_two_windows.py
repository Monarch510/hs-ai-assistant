"""两窗布局：左=其他信息（含对手预判）、右=牌库剩余，两扇各自独立拖动。

只验能自动验的：两窗都建出来、标题不撞单实例互斥用的主窗名、拖一扇不动别的、
收起时一起隐去、两处摆位分别落盘并在同布局版本下各自读回。像素/真机手感不测。
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402
from hsai.ui_theme import LAYOUT_VERSION  # noqa: E402


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-2win-")
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "window.json"
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", self.state_path),
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


class PaneStructureTest(_Case):
    def test_only_deck_pane_exists(self) -> None:
        self.assertEqual(set(self.ov._panes), {"deck"})
        win = self.ov._panes["deck"]["win"]
        self.assertIsNot(win, self.ov.root)
        # 单实例互斥按「炉石 AI 助手」这个标题抓主窗，卫星窗不能撞名
        title = win.title()
        self.assertNotEqual(title, "炉石 AI 助手")
        self.assertEqual(title, "炉石 AI · 牌库剩余")

    def test_pane_has_its_own_body(self) -> None:
        self.assertIsNot(self.ov._panes["deck"]["body"], self.ov.body)


class DragTest(_Case):
    def _drag(self, key: str, to_x: int, to_y: int) -> None:
        win = self.ov._panes[key]["win"]
        # widget 用该窗的 body，_drag_key 靠 winfo_toplevel 认出是哪一扇
        widget = self.ov._panes[key]["body"]
        dx = to_x - win.winfo_x()
        dy = to_y - win.winfo_y()
        self.ov._drag_start(types.SimpleNamespace(
            widget=widget, x_root=win.winfo_x() + dx, y_root=win.winfo_y() + dy))
        self.ov._drag_move(types.SimpleNamespace(x_root=to_x, y_root=to_y))
        self.ov._drag_end(types.SimpleNamespace())

    def test_dragging_deck_moves_only_deck(self) -> None:
        left_before = list(self.ov.pos)
        deck_before = list(self.ov._panes["deck"]["pos"])
        self._drag("deck", 400, 200)
        self.assertNotEqual(list(self.ov._panes["deck"]["pos"]), deck_before)
        # 拖的是牌库这扇：它自己动了，左窗纹丝不动
        self.assertEqual(list(self.ov.pos), left_before)


class VisibilityTest(_Case):
    def test_collapse_hides_satellite(self) -> None:
        self.ov.collapsed = False
        self.ov._sync_panes_visibility()
        self.ov.collapsed = True
        self.ov._sync_panes_visibility()
        for pane in self.ov._panes.values():
            self.assertFalse(pane["win"].winfo_ismapped())


class PersistTest(_Case):
    def test_both_positions_roundtrip(self) -> None:
        self.ov.pos = (11, 22)
        self.ov._panes["deck"]["pos"] = [333, 444]
        self.ov._persist()
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(data["layout"], LAYOUT_VERSION)
        self.assertEqual((data["x"], data["y"]), (11, 22))
        self.assertEqual((data["right_x"], data["right_y"]), (333, 444))
        # 对手预判已并入左窗，不再写 mid_x/mid_y
        self.assertNotIn("mid_x", data)
        self.assertNotIn("mid_y", data)

    def test_old_layout_ignores_saved_positions(self) -> None:
        # 旧布局版本：右窗位置回到默认，不被 window.json 里的旧值带跑
        self.state_path.write_text(json.dumps({
            "layout": LAYOUT_VERSION - 1, "x": 1, "y": 2, "right_x": 5, "right_y": 6,
        }), encoding="utf-8")
        ov = Overlay(self.db, demo=True)
        try:
            self.assertEqual(list(ov._panes["deck"]["pos"]), [580, 60])
        finally:
            ov.quit()


if __name__ == "__main__":
    unittest.main()
