"""UI 显示动态标签权重：「本回合偏重」的选类/挑理由纯函数 + 渲染冒烟。

纯函数部分不依赖 tkinter、也不读 data/；渲染冒烟复用 tests/_ui_support 的替身，
验的是「不抛异常、该画的时候画了、缺字段时不画」，不是像素。
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
from hsai.ui import Overlay, _weight_summary  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# docs/card-tagging/DYNAMIC-WEIGHTS.md §7 的实际快照样例
SAMPLE = {
    "weights": {
        "单解": 0.99, "群解": 1.03, "站场": 0.95, "增益": 0.56, "减益": 0.75,
        "治疗": 0.56, "护甲": 0.46, "资源": 0.80, "抽牌": 0.80, "斩杀": 0.94,
        "未知": 0.0,
    },
    "reasons": [
        "出牌阶段：叠加场面连续信号",
        "对面场攻/随从多：群解、单解、减益加权",
        "对面场面占优：铺场/抢血降权，更该解场",
        "手牌充足：补牌降权",
        "有余费没花完：资源/抽牌加权",
        "回合数偏大（后期/疲劳）：资源、过牌、斩杀加权",
    ],
}


class WeightSummaryTest(unittest.TestCase):
    def test_top3_sorted_desc(self) -> None:
        tops, reasons = _weight_summary(SAMPLE)
        self.assertEqual(tops, ["群解 1.03", "单解 0.99", "站场 0.95"])
        self.assertEqual(len(reasons), 2)

    def test_unknown_negative_and_zero_filtered(self) -> None:
        tops, _ = _weight_summary({
            "weights": {"未知": 0.0, "治疗": -0.3, "站场": 1.2, "抽牌": 0.0},
            "reasons": [],
        })
        self.assertEqual(tops, ["站场 1.2"], "未知/降权/0 权重都是噪声，不该进「偏重」")

    def test_tie_break_follows_tag_order(self) -> None:
        tops, _ = _weight_summary({"weights": {"斩杀": 1.5, "群解": 1.5, "单解": 1.5}})
        self.assertEqual(tops, ["单解 1.5", "群解 1.5", "斩杀 1.5"],
                         "同分顺序要固定，否则重画时列表会跳")

    def test_reasons_skip_meta_lines(self) -> None:
        _, reasons = _weight_summary(SAMPLE)
        self.assertNotIn("出牌阶段：叠加场面连续信号", reasons)
        self.assertEqual(reasons[0], "对面场攻/随从多：群解、单解、减益加权")
        self.assertEqual(len(reasons), 2, "理由最多 2 行")

    def test_mulligan_meta_reason_skipped(self) -> None:
        _, reasons = _weight_summary({
            "weights": {"站场": 1.0},
            "reasons": ["留牌阶段：只看职业/对位，忽略场面信号",
                        "对面偏控制/后期：资源、过牌、斩杀加权，解场牌降权"],
        })
        self.assertEqual(reasons, ["对面偏控制/后期：资源、过牌、斩杀加权，解场牌降权"])

    def test_empty_and_malformed_do_not_crash(self) -> None:
        for payload in (
            None,
            {},
            {"weights": {}},
            {"weights": None},
            {"weights": {"未知": 0.0}, "reasons": ["出牌阶段：叠加场面连续信号"]},
            {"reasons": ["我方血线偏低：治疗/护甲加权"]},
        ):
            self.assertEqual(_weight_summary(payload), ([], []), payload)

    def test_bad_weight_value_ignored(self) -> None:
        tops, _ = _weight_summary({"weights": {"单解": "很重", "群解": 1.1}})
        self.assertEqual(tops, ["群解 1.1"])

    def test_optional_scale_fields_are_ignored(self) -> None:
        # 另一个 worker 正在补 mulligan_scale/play_scale，未落地时不能影响展示
        payload = dict(SAMPLE, mulligan_scale=1.0, play_scale=1.0)
        self.assertEqual(_weight_summary(payload), _weight_summary(SAMPLE))


def _texts(widget) -> list[str]:
    """递归收齐 Label 文本；同 tests/test_render_smoke.py，替身和真 Tk 都要能用。"""
    out = []
    for child in widget.winfo_children():
        opts = getattr(child, "_opts", None)
        text = opts.get("text") if isinstance(opts, dict) else None
        if not isinstance(text, str):
            try:
                text = child.cget("text")
            except Exception:
                text = None
        if isinstance(text, str):
            out.append(text)
        out.extend(_texts(child))
    return out


class TagWeightRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()
        text = (FIXTURES / "sample_game.log").read_text(encoding="utf-8")
        cls.state = state.replay_log_text(text, cls.db)

    def setUp(self) -> None:
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        # root.update() 会跑 __init__ 里排好的 _tick，拿真实状态重画并覆盖注入值
        self.ov._update = lambda *a, **k: None
        self.ov._maybe_ai = lambda *a, **k: None
        self.snap = self.ov.advisor.snapshot(self.state)
        tmp = tempfile.TemporaryDirectory(prefix="hsai-tag-ui-")
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

    def test_snapshot_has_tag_weights(self) -> None:
        self.assertIn("tag_weights", self.snap, "advisor 应该已经产出 tag_weights")

    def test_renders_weight_card(self) -> None:
        self.ov._render(copy.deepcopy(self.snap))      # 抛异常就是失败
        texts = _texts(self.ov.root)
        self.assertTrue(any("本回合偏重" in t for t in texts), "偏重卡片没画出来")
        self.assertTrue(any("群解" in t or "单解" in t for t in texts),
                        "偏重的标签没画出来")

    def test_no_card_without_tag_weights(self) -> None:
        snap = copy.deepcopy(self.snap)
        snap.pop("tag_weights", None)
        self.ov._render(snap)
        texts = _texts(self.ov.root)
        self.assertFalse(any("本回合偏重" in t for t in texts),
                         "缺字段时不该多出卡片，行为要与改动前一致")

    def test_no_card_with_empty_weights(self) -> None:
        snap = copy.deepcopy(self.snap)
        snap["tag_weights"] = {"weights": {}, "reasons": []}
        self.ov._render(snap)
        texts = _texts(self.ov.root)
        self.assertFalse(any("本回合偏重" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
