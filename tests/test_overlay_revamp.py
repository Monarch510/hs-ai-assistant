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
from tests._ui_support import widget_texts as _texts

prepare()                       # 没有 tkinter 就装替身

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai.ui import Overlay     # noqa: E402
from hsai.ui_theme import ADVICE_VISIBLE_ROWS, DEFAULT_COLLAPSED, DECK_VISIBLE_ROWS

FIXTURES = Path(__file__).parent / "fixtures"


def _bound(widget, seq="<MouseWheel>") -> bool:
    """控件上是否挂了滚轮回调：替身查 _binds，真 tkinter 用 bind() 查询。"""
    if hasattr(widget, "_binds"):
        return seq in widget._binds
    try:
        return bool(widget.bind(seq))
    except Exception:
        return False


def _walk(widget):
    for c in widget.winfo_children():
        yield c
        yield from _walk(c)


def _label_text(widget) -> str:
    """控件上的 Label 文本，真 tkinter 和替身都能用。"""
    opts = getattr(widget, "_opts", None)
    t = opts.get("text") if isinstance(opts, dict) else None
    if not isinstance(t, str):
        try:
            t = widget.cget("text")
        except Exception:
            return ""
    return t if isinstance(t, str) else ""


def _pack_side(widget) -> str:
    """pack 的 side：替身查 _layout，真 tkinter 用 pack_info()。"""
    layout = getattr(widget, "_layout", None)
    if isinstance(layout, dict):
        return str(layout.get("side", ""))
    try:
        return str(widget.pack_info().get("side", ""))
    except Exception:
        return ""


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
        costs = [c["cost"] for c in d["cards"]]
        self.assertEqual(costs, sorted(costs, reverse=True), "明细要从高费到低费排")
        self.assertIn("key_cards", d, "老字段不能丢")
        self.assertIn("source", d)

    def test_deck_context_labels_card_type(self) -> None:
        from hsai.advisor import Advisor

        ids = [c.id for c in list(self.db.by_id.values())[:3]]
        adv = Advisor(self.db)
        adv.our_deck = ids
        adv._deck_manual = True
        st = types.SimpleNamespace(our_hand_ids=[], our_played=[])
        d = adv._deck_context(st)
        for c in d["cards"]:
            self.assertIn("type_cn", c, "每行都要带所属类型，窗口才显示得出来")
            self.assertTrue(c["type_cn"], "类型不能是空串：读不到也要有兜底")
            self.assertEqual(c["type_cn"], adv.cards.get(c["id"]).type_cn)

    def test_deck_context_labels_functional_tags(self) -> None:
        from hsai import tag_rules
        from hsai.advisor import Advisor

        ids = [c.id for c in list(self.db.by_id.values())[:3]]
        adv = Advisor(self.db)
        adv.our_deck = ids
        adv._deck_manual = True
        st = types.SimpleNamespace(our_hand_ids=[], our_played=[])
        d = adv._deck_context(st)
        for c in d["cards"]:
            self.assertIn("func_tags", c, "每行都要带功能标签，窗口才显示得出来")
            card = adv.cards.get(c["id"])
            expected = [t for t in tag_rules.TAG_ORDER
                        if t != "未知" and tag_rules.RULES[t](card)]
            self.assertEqual(c["func_tags"], expected, "按卡面现算、按 TAG_ORDER 排")
            self.assertNotIn("未知", c["func_tags"], "规则拿不准的内部标记不摆给玩家看")

    def test_deck_left_matches_battle_info(self) -> None:
        # 牌库剩余标题张数要服从「对战信息」的真实读数，别两套算法各说各话
        adv = self.ov.advisor
        ids = [c.id for c in list(self.db.by_id.values())[:3]]
        adv.our_deck = ids
        adv._deck_manual = True
        adv._auto_deck = None
        snap = adv.snapshot(self.state_obj)
        self.assertTrue(snap.get("deck"), "得先认出一套牌才有牌库剩余")
        self.assertEqual(snap["deck"]["left"], snap["our"]["deck_count"])

    def test_signature_tracks_deck_composition(self) -> None:
        snap = self._deck_snap(6)
        snap["deck"]["left"] = 6
        s1 = self.ov._signature(snap)
        snap2 = copy.deepcopy(snap)
        snap2["deck"]["cards"][0] = {"id": "Z", "name": "换掉的牌", "cost": 3, "count": 1}
        self.assertNotEqual(s1, self.ov._signature(snap2),
                            "只换组成不换张数也要重画，否则列表停在旧牌")


class RenderOrderTest(_Case):
    def test_info_above_advice_and_deck(self) -> None:
        self.ov._render(self._deck_snap())
        titles = self._section_titles()
        self.assertIn("本回合建议", titles)
        self.assertIn("牌库剩余", titles)
        # 用户要求：先看对战信息，再看建议
        self.assertLess(titles.index("对战信息"), titles.index("本回合建议"))
        self.assertLess(titles.index("本回合建议"), titles.index("牌库剩余"))
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

        for _ in range(12 - DECK_VISIBLE_ROWS):
            self.ov._on_deck_wheel(types.SimpleNamespace(delta=-120))
        self.assertLessEqual(self.ov._deck_scroll, 12 - DECK_VISIBLE_ROWS)
        self.assertTrue(any("第12张牌" in t for t in _texts(self.ov.root)))

        self.ov._on_deck_wheel(types.SimpleNamespace(delta=120))
        self.assertTrue(any("第11张牌" in t for t in _texts(self.ov.root)))

    def test_every_row_is_wheel_bound(self) -> None:
        # 复现「很难滚动」：滚轮事件不冒泡，铺满宽度的行 Label 没绑定时，
        # 鼠标停在牌名上父 Frame 收不到，只有缝隙/指示条能滚
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        rows = list(_walk(self.ov._deck_list))
        self.assertTrue(rows, "得先有可见行")
        for w in rows:
            self.assertTrue(_bound(w), "每一行都得挂滚轮，不能只绑容器 Frame")
        self.assertTrue(_bound(self.ov._deck_list), "容器本身也要挂")
        self.assertTrue(_bound(self.ov._deck_bar), "右侧指示条也要挂")

    def test_scroll_position_survives_rerender(self) -> None:
        # _update 每秒重画一帧：牌没变就别把人弹回顶部
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        for _ in range(4):
            self.ov._on_deck_wheel(types.SimpleNamespace(delta=-120))
        self.assertEqual(self.ov._deck_scroll, 4)
        self.ov._render(snap)
        self.assertEqual(self.ov._deck_scroll, 4, "同一副牌重画后滚动位置要留住")
        # 换了一副牌才回到顶部
        snap2 = self._deck_snap(12)
        snap2["deck"]["cards"] = list(reversed(snap2["deck"]["cards"]))
        self.ov._render(snap2)
        self.assertEqual(self.ov._deck_scroll, 0)

    def test_linux_wheel_buttons(self) -> None:
        from hsai.ui_render import _wheel_dir

        self.assertEqual(_wheel_dir(types.SimpleNamespace(num=4, delta=0)), -1)
        self.assertEqual(_wheel_dir(types.SimpleNamespace(num=5, delta=0)), 1)
        self.assertEqual(_wheel_dir(types.SimpleNamespace(delta=-120)), 1)
        self.assertEqual(_wheel_dir(types.SimpleNamespace(delta=120)), -1)
        self.assertEqual(_wheel_dir(types.SimpleNamespace(delta=0)), 0)

    def test_no_deck_no_section(self) -> None:
        snap = self._deck_snap()
        snap["deck"] = {}
        self.ov._render(snap)
        self.assertNotIn("牌库剩余", self._section_titles(),
                         "认不出卡组就整块不画，别编一个牌库出来")

    def test_deck_row_shows_name_count_and_draw_rate(self) -> None:
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        texts = _texts(self.ov.root)
        names = [t for t in texts if "第1张牌" in t]
        self.assertTrue(names, "明细要写名字、数量")
        self.assertIn("×1", names[0])
        self.assertNotIn("抽", names[0], "抽取率不再紧跟在数量后面")
        # 百分比单独一个 Label、只写数值、贴在行末（right）
        rates = [w for w in _walk(self.ov._deck_list)
                 if _label_text(w).strip().endswith("%")
                 and "%" in _label_text(w) and "抽" not in _label_text(w)]
        self.assertTrue(rates, "行末要有单独的百分比 Label")
        for w in rates:
            text = _label_text(w).strip()
            self.assertRegex(text, r"^\d+%$", "只展示百分比，别带「抽」字或别的说明")
            self.assertEqual(_pack_side(w), "right", "百分比要放到行末尾")

    def test_deck_row_shows_type_when_present(self) -> None:
        snap = self._deck_snap(12)
        snap["deck"]["cards"][0]["type_cn"] = "随从"
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        names = [t for t in _texts(self.ov.root) if "第1张牌" in t]
        self.assertTrue(names, "明细要写名字、数量")
        self.assertIn("[随从]", names[0], "所属类型要跟在牌名后面")
        self.assertIn("×1", names[0], "类型不能挤掉数量")

    def test_deck_row_shows_functional_tags(self) -> None:
        snap = self._deck_snap(12)
        snap["deck"]["cards"][0]["func_tags"] = ["治疗", "站场"]
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        names = [t for t in _texts(self.ov.root) if "第1张牌" in t]
        self.assertTrue(names)
        self.assertIn("[治疗·站场]", names[0], "功能标签要跟在牌名后面、顺序不靠哈希")
        self.assertIn("×1", names[0], "功能标签不能挤掉数量")

    def test_deck_row_without_functional_tags_stays_clean(self) -> None:
        # 老快照没 func_tags：不能冒出空方括号或「未知」噪声
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        names = [t for t in _texts(self.ov.root) if "第1张牌" in t]
        self.assertIn("×1", names[0])
        self.assertNotIn("未知", names[0])
        self.assertNotIn("[]", names[0])

    def test_deck_row_without_type_stays_clean(self) -> None:
        # 老快照没 type_cn：不能冒出「未知」「[]」这类噪声
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        names = [t for t in _texts(self.ov.root) if "第1张牌" in t]
        self.assertIn("×1", names[0])
        self.assertNotIn("[", names[0], "没读到类型就整段留空")

    def test_draw_rate_is_zero_when_deck_count_unknown(self) -> None:
        snap = self._deck_snap(6)
        snap["deck"]["left"] = None
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        self.assertTrue(any("未知" in t for t in _texts(self.ov.root)),
                        "牌库读数拿不到时抽取率显示未知，不编一个数")

    def test_deck_gap_is_stated_not_faked(self) -> None:
        # 真实牌库 17 张，但只认出 12 张（残套模板）：差多少要明说，不能编牌名
        snap = self._deck_snap(12)
        snap["deck"]["left"] = 17
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        self.assertTrue(any("还有 5 张未识别" in t for t in _texts(self.ov.root)),
                        "标题与明细对不上时要补一行说明，别让残明细冒充完整牌库")
        self.assertTrue(any("牌库剩余 · 17 张" in t for t in _texts(self.ov.root)),
                        "标题仍显示对战信息里的真实张数")

    def test_no_gap_line_when_every_card_is_known(self) -> None:
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        self.assertFalse(any("未识别" in t for t in _texts(self.ov.root)),
                         "明细齐了就别画蛇添足")


class AdviceScrollTest(_Case):
    def test_long_advice_is_paged_not_all_drawn(self) -> None:
        snap = self._deck_snap()
        snap["advice"] = {
            "lines": [{"rank": i, "title": f"步骤{i}", "score": 90 - i,
                       "reason": f"理由{i}"} for i in range(1, 4)],
            "lethal": False,
            "defense": [f"提防牌{i}" for i in range(1, 9)],
        }
        self.ov._render(snap)
        self.assertGreater(len(self.ov._advice_rows), ADVICE_VISIBLE_ROWS)
        self.assertIn("步骤1", _texts(self.ov.root))
        self.assertNotIn("提防牌8", " ".join(_texts(self.ov.root)),
                         "超出的行不该一次全画出来")
        # 一格一行：多滚几格才到底（原来一格跳半屏）
        for _ in range(len(self.ov._advice_rows)):
            self.ov._on_advice_wheel(types.SimpleNamespace(delta=-120))
        self.assertIn("提防牌8", " ".join(_texts(self.ov.root)),
                      "下滑后能看到后面的行")
        self.assertTrue(all(_bound(w) for w in _walk(self.ov._advice_list)),
                        "建议卡的每一行也要挂滚轮（同一个冒泡坑）")
        self.ov._on_advice_wheel(types.SimpleNamespace(delta=120))
        self.assertLess(self.ov._advice_scroll, len(self.ov._advice_rows))


if __name__ == "__main__":
    unittest.main()
