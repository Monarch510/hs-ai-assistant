"""复盘不再常驻：启动不读旧记录、局末只展示本局且限时、超时收细条、新局清空。

用户 (m04120) 的「两处都收口」：等待对局时别一直挂着上一局复盘，
「不赞同复盘」只来自刚结束这一局。这里锁 5 条可自动验的行为，视觉靠全量冒烟兜住。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hsai.state import GameState
from hsai import cards

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身 + 改道状态文件

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402


def _snap(**kw) -> dict:
    s = {"phase": "playing", "turn": 1, "actions": []}
    s.update(kw)
    return s


class ReviewDisplayTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-review-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.review_path = root / "last_review.json"
        self.feedback_path = root / "advice_feedback.jsonl"
        # 磁盘上放着「上一局」的复盘与旧反馈：启动必须无视它们
        self.review_path.write_text(
            json.dumps({"result": "win", "lessons": ["旧教训"],
                        "findings": [], "issues": []}), encoding="utf-8")
        self.feedback_path.write_text(
            json.dumps({"items": [{"lesson": "旧反馈", "rule": "旧规则"}]}) + "\n",
            encoding="utf-8")
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", root / "window.json"),
            mock.patch.object(ui_mod, "LAST_REVIEW_JSON", self.review_path),
            mock.patch.object(ui_mod, "ADVICE_FEEDBACK_JSONL", self.feedback_path),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", root / "advice.jsonl"),
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ui_mod.ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])

        self.db = cards.CardDB([])
        self.st = GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._render = lambda *a, **k: None
        self.ov._sync_bar = lambda *a, **k: None
        self.ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(lambda: self.ov.quit())

    def _same_game(self) -> None:
        """把状态预置成「非新局」，否则首帧 new_game 会清掉注入的展示窗口。"""
        self.ov._last_state = self.st
        self.ov._last_game_ts = str(getattr(self.st, "_create_ts", "") or "")

    def test_startup_ignores_old_review_and_feedback(self) -> None:
        ov = self.ov
        self.assertIsNone(ov._last_review)
        self.assertEqual(ov._feedback_items, [])
        self.assertIsNone(ov._review_deadline)
        self.assertTrue(ov.collapsed)

    def test_game_over_opens_limited_review_once(self) -> None:
        ov = self.ov
        self._same_game()
        ov._game_over_since = time.time() - 9     # 已经过了 8 秒，直接进 idle
        snap = _snap(phase="game_over")
        with mock.patch.object(ov, "_render_last_review") as render, \
                mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)
            ov._update(self.st)                   # 第二帧不该再重画
        self.assertIsNotNone(ov._last_review)
        self.assertIsNotNone(ov._review_deadline)
        self.assertGreater(ov._review_deadline, time.time())
        self.assertEqual(render.call_count, 1, "复盘只在展示窗口内画一次")

    def test_expired_deadline_collapses_to_bar(self) -> None:
        ov = self.ov
        self._same_game()
        ov._last_review = {"lessons": ["本局教训"]}
        ov._review_deadline = time.time() - 1    # 展示窗口已过期
        ov.collapsed = False
        snap = _snap(phase="idle")
        with mock.patch.object(ov, "_render_last_review") as render, \
                mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)
        self.assertEqual(render.call_count, 0, "超时后不再画复盘")
        self.assertTrue(ov.collapsed, "超时后收成细条")

    def test_new_game_clears_review_state(self) -> None:
        ov = self.ov
        ov._feedback_items = [{"lesson": "本局", "rule": "x"}]
        ov._last_review = {"lessons": ["本局"]}
        ov._review_deadline = time.time() + 30
        fresh = GameState(self.db)               # 换对象 = 新局
        snap = _snap(phase="idle")
        with mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(fresh)
        self.assertEqual(ov._feedback_items, [])
        self.assertIsNone(ov._last_review)
        self.assertIsNone(ov._review_deadline)

    def test_render_error_collapses_without_old_review(self) -> None:
        ov = self.ov
        ov.collapsed = False
        ov._last_review = {"lessons": ["旧复盘"]}
        with mock.patch.object(ov, "_render_last_review") as render:
            ov._show_render_error()
        self.assertEqual(render.call_count, 0, "出错不该翻出旧复盘")
        self.assertTrue(ov.collapsed)


if __name__ == "__main__":
    unittest.main()
