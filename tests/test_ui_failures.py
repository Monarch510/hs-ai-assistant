"""后台链路的静默失败要在状态栏留痕，不能「看着正常、其实停更」。

构造照抄 test_render_smoke：装 tkinter 替身、把窗口状态改道临时目录、不真起消息循环。
卡牌用空库 CardDB([])，离线可跑，不碰 data/ 下的缓存；_log 改道到内存，别写真实日志。
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，别整组跳过

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai import ai as ai_mod   # noqa: E402
from hsai import policy as policy_mod   # noqa: E402
from hsai.ui import Overlay     # noqa: E402


class UiFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        # 先改道再构造 Overlay：构造过程也不许往 data/hsai.log 写
        patcher = mock.patch.object(ui_mod.logutil, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.db = cards.CardDB([])
        self.st = state.GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass

    def _inject_choice_failure(self) -> dict:
        """让 _render → _maybe_ai 的抉择链路必然抛异常，返回这一帧快照。"""
        snap = self.ov.advisor.snapshot(self.st)
        snap.update(phase="playing", turn=5, turn_known=True, is_our_turn=True,
                    choice={"type": "GENERAL",
                            "options": [{"name": "A"}, {"name": "B"}]})
        # _render 内部把 _last_state 传给 _maybe_ai；有它才会走进抉择分支
        self.ov._last_state = self.st
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(policy_mod, "pick_choice",
                                  side_effect=RuntimeError("boom")):
            self.ov._render(snap)
        return snap

    def test_choice_failure_is_counted(self) -> None:
        self._inject_choice_failure()
        self.assertGreaterEqual(self.ov._soft_failures, 1)
        self.assertGreaterEqual(self.ov._soft_failure_kinds.get("choice", 0), 1)

    def test_status_bar_shows_failure(self) -> None:
        self._inject_choice_failure()
        self.ov._update(self.st)        # _update 收的是 GameState，不是快照 dict
        self.assertIn("异常", self.ov.bar_label.cget("text"))

    def test_no_failure_no_hint(self) -> None:
        self.ov._update(self.st)
        self.assertNotIn("异常", self.ov.bar_label.cget("text"))

    def test_f8_reload_clears_counter(self) -> None:
        self.ov._soft_failures = 5
        self.ov._soft_failure_kinds = {"choice": 5}
        self.ov._render_broken = True       # F8 的「重载」分支
        self.ov._f8_down = False
        fake = types.SimpleNamespace(windll=types.SimpleNamespace(
            user32=types.SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000)))
        with mock.patch.object(ui_mod, "ctypes", fake):
            self.ov._check_hotkey()
        self.assertEqual(self.ov._soft_failures, 0)
        self.assertEqual(self.ov._soft_failure_kinds, {})


if __name__ == "__main__":
    unittest.main()
