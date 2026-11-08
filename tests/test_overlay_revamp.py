"""悬浮窗改版：信息分层、默认折叠、牌库剩余滚动。

用户拍板的 12 条里能自动验的部分：新的默认折叠集合要尊重「用户手动展开过」
的 window.json、建议卡置顶、牌库剩余只画可见行且能滚、advisor 要给出完整
剩余牌列表、_signature 要能发现「张数没变但换了一张牌」。
视觉细节（金色竖条、字号）靠全量冒烟不抛异常兜住，不测像素。
"""

from __future__ import annotations

import copy
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402
from hsai.ui_theme import DEFAULT_COLLAPSED, DECK_VISIBLE_ROWS

FIXTURES = Path(__file__).parent / "fixtures"


def _texts(widget):
    """递归收齐控件树里的 Label 文本，真 tkinter 和替身都能用。"""
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


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()
        cls.state_obj = state.replay_log_text(
            (FIXTURES / "sample_game.log").read_text(encoding="utf-8"), cls.db)

    def setUp(self) -> None:
        # 每例都从「没有 window.json」开始，默认折叠才不会被上例的写盘污染
        tmp = tempfile.TemporaryDirectory(prefix="hsai-revamp-")
        self.addCleanup(tmp.cleanup)
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
        self.snap = self.ov.advisor.snapshot(self.state_obj)

    def _deck_snap(self, n: int = 12) -> dict:
        snap = copy.deepcopy(self.snap)
        snap["phase"] = "playing"
        snap["is_our_turn"] = True
        snap["advice"] = {
            "lines": [{"rank": 1, "title": "先解场再打脸", "score": 88,
                       "reason": "对面场攻太高"}],
            "lethal": False, "defense": [],
        }
        snap["deck"] = {
            "name": "测试卡组", "class_cn": "法师", "left": n,
            "cards": [{"id": f"C{i}", "name": f"第{i}张牌", "cost": i % 8,
                       "count": 1} for i in range(1, n + 1)],
            "key_cards": [], "source": "auto",
        }
        snap["prediction"] = {
            "name": "快攻猎", "confidence": 0.8, "source": "core",
            "candidates": [], "likely_hand": [], "aoe_risk": 0.1, "removal_risk": 0.1,
        }
        return snap

    def _section_titles(self) -> list[str]:
        out = []
        for t in _texts(self.ov.root):
            if t.startswith(("▾ ", "▸ ")):
                out.append(t[2:].split(" · ")[0])
        return out


class DefaultCollapseTest(_Case):
    def test_new_install_uses_default_collapsed_set(self) -> None:
        for key in DEFAULT_COLLAPSED:
            self.assertIn(key, self.ov._collapsed)
        for key in ("本回合建议", "待你选择", "对战信息", "留牌建议"):
            self.assertNotIn(key, self.ov._collapsed, "要动手的块默认得摊开")

    def test_explicit_empty_list_is_respected(self) -> None:
        # 用户手动把默认折叠的块全展开了：键存在（哪怕是空表）就不能再套默认
        ui_mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ui_mod.STATE_PATH.write_text(
            json.dumps({"collapsed_sections": []}, ensure_ascii=False), encoding="utf-8")
        ov2 = Overlay(self.db, demo=True)
        ov2._tick = lambda: None
        self.addCleanup(lambda: ov2.quit())
        self.assertEqual(ov2._collapsed, set())

    def test_bad_value_still_degrades_to_empty_set(self) -> None:
        # 键存在但是坏值（第三轮修过的降级路径）不能被误判成「从未表态」
        ui_mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ui_mod.STATE_PATH.write_text(
            json.dumps({"collapsed_sections": "oops"}, ensure_ascii=False), encoding="utf-8")
        ov2 = Overlay(self.db, demo=True)
        ov2._tick = lambda: None
        self.addCleanup(lambda: ov2.quit())
        self.assertEqual(ov2._collapsed, set())


class DeckContextTest(_Case):
    def test_deck_context_exposes_full_remaining_list(self) -> None:
        from hsai.advisor import Advisor

        ids = [c.id for c in list(self.db.by_id.values())[:3]]
        adv = Advisor(self.db)
        adv.our_deck = ids
        adv._deck_manual = True
        st = types.SimpleNamespace(our_hand_ids=[], our_played=[])
        d = adv._deck_context(st)
        self.assertEqual(d["left"], 3)
        self.assertEqual(len(d["cards"]), 3)
        self.assertTrue(all({"id", "name", "cost", "count"} <= set(c) for c in d["cards"]))
        self.assertIn("key_cards", d, "老字段不能丢")
        self.assertIn("source", d)

    def test_signature_tracks_deck_composition(self) -> None:
        snap = self._deck_snap(6)
        snap["deck"]["left"] = 6
        s1 = self.ov._signature(snap)
        snap2 = copy.deepcopy(snap)
        snap2["deck"]["cards"][0] = {"id": "Z", "name": "换掉的牌", "cost": 3, "count": 1}
        self.assertNotEqual(s1, self.ov._signature(snap2),
                            "只换组成不换张数也要重画，否则列表停在旧牌")


class RenderOrderTest(_Case):
    def test_advice_pinned_above_info_and_deck(self) -> None:
        self.ov._render(self._deck_snap())
        titles = self._section_titles()
        self.assertIn("本回合建议", titles)
        self.assertIn("牌库剩余", titles)
        self.assertLess(titles.index("本回合建议"), titles.index("对战信息"))
        self.assertLess(titles.index("对战信息"), titles.index("牌库剩余"))
        self.assertLess(titles.index("牌库剩余"), titles.index("对手预判"))

    def test_mulligan_advice_comes_first_in_mulligan_phase(self) -> None:
        snap = self._deck_snap()
        snap["phase"] = "mulligan"
        snap["mulligan"] = {"advice": [{"action": "留", "name": "寒冰箭", "score": 80}]}
        self.ov._render(snap)
        titles = self._section_titles()
        self.assertEqual(titles[0], "留牌建议")

    def test_prediction_title_carries_name_and_confidence(self) -> None:
        self.ov._render(self._deck_snap())
        self.assertTrue(any("对手预判 · 快攻猎 80%" in t for t in _texts(self.ov.root)))


class DeckScrollTest(_Case):
    def test_only_visible_rows_are_drawn_and_wheel_pages(self) -> None:
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")   # 展开这个默认折叠的块
        self.ov._render(snap)
        texts = _texts(self.ov.root)
        self.assertTrue(any("牌库剩余" in t for t in texts))
        self.assertTrue(any("第1张牌" in t for t in texts))
        self.assertFalse(any("第12张牌" in t for t in texts),
                         "不该把全部牌一次画出来，窗口会越撑越高")

        for _ in range(6):
            self.ov._on_deck_wheel(types.SimpleNamespace(delta=-120))
        self.assertLessEqual(self.ov._deck_scroll, 12 - DECK_VISIBLE_ROWS)
        self.assertTrue(any("第12张牌" in t for t in _texts(self.ov.root)))

        self.ov._on_deck_wheel(types.SimpleNamespace(delta=120))
        self.assertTrue(any("第6张牌" in t for t in _texts(self.ov.root)))

    def test_no_deck_no_section(self) -> None:
        snap = self._deck_snap()
        snap["deck"] = {}
        self.ov._render(snap)
        self.assertNotIn("牌库剩余", self._section_titles(),
                         "认不出卡组就整块不画，别编一个牌库出来")


if __name__ == "__main__":
    unittest.main()
