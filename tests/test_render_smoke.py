"""渲染冒烟测试：每种面板状态都必须能画出来，不能抛异常。

背景：曾经有过两次「界面卡住不动」的线上问题，都是渲染函数里的局部错误
（引用了不存在的变量、空列表时变量未定义），被 _tick 的 try/except 吞掉，
表现成「对局结束了但面板不更新」。这些错误在真实游戏里才触发，
所以这里直接构造各种快照，把渲染路径全跑一遍。

没有 tkinter 的环境（精简版 Python、CI 容器）用 tests/_ui_support 里的替身跑，
验的是渲染路径不抛异常，不是像素。
"""

from __future__ import annotations
import types

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare
from tests._ui_support import widget_texts as _texts

prepare()                       # 没有 tkinter 就装替身，别整组跳过
from hsai import ai as ai_mod  # noqa: E402
from hsai import policy as policy_mod  # noqa: E402

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402  必须在 prepare() 之后

FIXTURES = Path(__file__).parent / "fixtures"


class RenderSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()
        text = (FIXTURES / "sample_game.log").read_text(encoding="utf-8")
        cls.state = state.replay_log_text(text, cls.db)

    def setUp(self) -> None:
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None          # 别让它自己定时跑
        self.snap = self.ov.advisor.snapshot(self.state)
        # 带建议行的快照会让 _render 记一条 advice_log，改道到临时目录别碰 data/
        tmp = tempfile.TemporaryDirectory(prefix="hsai-advice-log-")
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(ui_mod, "ADVICE_LOG_JSONL",
                                    Path(tmp.name) / "advice_log.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass

    def _render(self, snap: dict) -> None:
        self.ov._render(snap)                 # 抛异常就是失败
        self.ov.root.update()

    def test_renders_normal(self) -> None:
        self._render(self.snap)

    def test_header_turn_owner_is_short(self) -> None:
        """标题栏回合归属用两字简写：窄条上「我方回合 / 对方回合」太占地方。"""
        for ours, expected in ((True, "我方"), (False, "对方")):
            snap = copy.deepcopy(self.snap)
            snap.update(phase="playing", turn=7, turn_known=True, is_our_turn=ours)
            self.ov._render(snap)             # 不调 root.update()，免得排队的 _tick 重画
            text = str(self.ov.header_label.cget("text"))
            self.assertIn(f"T7 · {expected} ·", text)
            self.assertNotIn("回合", text)

    def test_renders_lethal(self) -> None:
        """斩杀分支：标题会变成「斩杀」，曾经在这里引用过不存在的 snap。"""
        snap = copy.deepcopy(self.snap)
        snap["advice"] = dict(snap.get("advice") or {})
        snap["advice"]["lethal"] = True
        snap["advice"]["lines"] = [
            {"rank": 1, "title": "大酋长拉法姆 攻击英雄", "score": 99, "reason": "打脸 3 点"},
        ]
        self._render(snap)

    def test_renders_empty_advice(self) -> None:
        """没有建议行时，防御块不能引用未定义的 inner。"""
        snap = copy.deepcopy(self.snap)
        snap["advice"] = {"lines": [], "lethal": False, "defense": ["提防 绝命乱斗（62%）"]}
        # 和 defense 用例同理：拦住 root.update() 触发的重画，否则注入的 defense 被冲掉
        self.ov._update = lambda *a, **k: None
        self._render(snap)
        self.assertTrue(any("提防" in t for t in _texts(self.ov.root)),
                        "只有防御块时建议卡也要画出来")

    def test_defense_lines_are_rendered(self) -> None:
        """有建议行时，defense 的每一行都必须画到面板上。"""
        snap = copy.deepcopy(self.snap)
        snap["advice"] = {
            "lines": [{"rank": 1, "title": "寒冰箭 打对面随从", "score": 88, "reason": "先解场"}],
            "lethal": False,
            "defense": ["提防 绝命乱斗（62%）"],
        }
        # root.update() 会跑 __init__ 里排好的 _tick，它拿真实对局状态重画一帧，
        # 把上面注入的 defense 覆盖掉 —— 这是测试脚手架的问题，不是渲染没画。
        self.ov._update = lambda *a, **k: None
        self._render(snap)
        self.assertTrue(any("提防" in t for t in _texts(self.ov.root)),
                        "defense 内容没渲染出来")

    def test_renders_game_over_with_review(self) -> None:
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "game_over"
        snap["review"] = ["有斩杀没斩：第 8 回合（5 点）"]
        self._render(snap)

    def test_renders_mulligan(self) -> None:
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "mulligan"
        snap["mulligan"] = {
            "advice": [
                {"action": "留", "name": "寒冰箭", "score": 80, "reason": "低费解场"},
                {"action": "换", "name": "冰风雪人", "score": 30, "reason": "太贵"},
            ]
        }
        self._render(snap)

    def test_renders_unknown_archetype(self) -> None:
        snap = copy.deepcopy(self.snap)
        snap["prediction"] = {
            "name": "未知卡组", "confidence": 0.0, "unknown": True,
            "candidates": [], "likely_hand": [], "aoe_risk": 0.0, "removal_risk": 0.0,
        }
        self._render(snap)

    def test_feedback_items_show_in_review(self) -> None:
        """局末展示的复盘要带上本局点 👎 的反馈（不再从文件读旧记录常驻）。"""
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "game_over"
        snap["advice"] = {"lines": [], "lethal": False, "defense": []}
        snap["unmodelled"] = []
        self._render(snap)
        # 注入必须放在 _render 之后：真 Tk 的 root.update() 会触发 __init__ 里
        # 排好的 after(50, _tick)，它拿的是原绑定的 _tick（setUp 的 lambda 替不掉），
        # 进而 _update 的 new_game 分支把本局反馈清空。定向单跑 50ms 没到才侥幸过。
        self.ov._last_review = {
            "result": "win", "turns": 8, "our_class": "mage", "opp_class": "mage",
            "findings": [], "issues": [],
        }
        self.ov._feedback_items = [
            {"lesson": "别乱铺场", "rule": "对面 3 伤 AOE 时留随从"}]
        self.ov._render_last_review()
        self.assertIn("不赞同复盘：别乱铺场", _texts(self.ov.root))

    def _render_with_gap(self, gap: float) -> list[str]:
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "playing"
        snap["is_our_turn"] = True
        snap["turn_known"] = True
        # _render_advice 在 lines 为空时提前 return；分差挂在这张建议卡片里，
        # 所以必须给一条真建议，否则测的只是「什么都没画」
        snap["advice"] = {
            "lines": [{"rank": 1, "title": "火球术", "score": 99,
                       "reason": "能解掉1个随从"}],
            "lethal": False, "defense": [],
        }
        snap["unmodelled"] = []
        snap["choice"] = {"type": "GENERAL",
                          "options": [{"name": "火球术"}, {"name": "寒冰箭"}]}
        self.ov._ai_route = None
        # _render() 内部会调 _maybe_ai() 重算抉择并覆盖 _ai_pick/_pick_gap（ui.py:743-744）；
        # 而 root.update() 还会触发 Overlay 启动时排好的 _tick（替换 _tick 属性拦不住
        # 已经排进 after 队列的那个绑定），它拿真实 demo 状态又渲染一遍并清掉注入值。
        self.ov._maybe_ai = lambda *a, **k: None
        self.ov._update = lambda *a, **k: None
        self._render(snap)                     # 第一帧按签名作废旧建议
        self.ov._ai_pick = ("火球术", "能解掉1个随从")
        self.ov._pick_gap = gap
        self._render(snap)                     # 签名没变，这一帧才挂着建议
        return _texts(self.ov.root)

    def test_pick_gap_is_shown_and_big_gap_is_not(self) -> None:
        texts = self._render_with_gap(0.4)
        self.assertIn("与第二名差 0.4 分（咬得紧）", texts)
        texts = self._render_with_gap(9.9)
        self.assertFalse(any("与第二名差" in t for t in texts))


# 并入自 tests/test_ui_failures.py：后台链路静默失败要在状态栏留痕
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
