"""渲染冒烟测试：每种面板状态都必须能画出来，不能抛异常。

背景：曾经有过两次「界面卡住不动」的线上问题，都是渲染函数里的局部错误
（引用了不存在的变量、空列表时变量未定义），被 _tick 的 try/except 吞掉，
表现成「对局结束了但面板不更新」。这些错误在真实游戏里才触发，
所以这里直接构造各种快照，把渲染路径全跑一遍。

没有 tkinter 的环境（精简版 Python、CI 容器）用 tests/_ui_support 里的替身跑，
验的是渲染路径不抛异常，不是像素。
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，别整组跳过

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402  必须在 prepare() 之后

FIXTURES = Path(__file__).parent / "fixtures"


def _texts(widget):
    """递归收齐控件树里的 Label 文本。

    **两种环境都要能用**：装不上 tkinter 时 `_ui_support.install()` 会换成替身，
    文本只记在 `_opts` 里；本机有真 tkinter（Windows 自带 8.6）时控件是真实对象，
    `_opts` 根本不存在，得用 `cget("text")`。之前只认 `_opts`，于是同一个用例
    在 CI 上绿、在开发机上红。
    """
    out = []
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
        self._render(snap)

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
        """重启后等界面那张「上一局」卡片要带上上次点 👎 的复盘。"""
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "game_over"
        snap["advice"] = {"lines": [], "lethal": False, "defense": []}
        snap["unmodelled"] = []
        # 没有上一局记录时 _render_last_review 会直接返回，反馈卡片根本画不出来
        self.ov._last_review = {
            "result": "win", "turns": 8, "our_class": "mage", "opp_class": "mage",
            "findings": [], "issues": [],
        }
        self.ov._feedback_items = [
            {"lesson": "别乱铺场", "rule": "对面 3 伤 AOE 时留随从"}]
        self._render(snap)
        self.ov._render_last_review()          # 等待对局时展示的就是这一屏
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


if __name__ == "__main__":
    unittest.main()
