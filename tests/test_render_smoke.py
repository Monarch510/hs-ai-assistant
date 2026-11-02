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
import unittest
from pathlib import Path

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，别整组跳过

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


if __name__ == "__main__":
    unittest.main()
