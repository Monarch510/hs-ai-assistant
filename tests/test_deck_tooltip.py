"""牌库剩余行：行内只留费用/名称/抽取率，详情靠悬停 tooltip。"""
from __future__ import annotations

import copy
import unittest

from hsai.cards import Card
from hsai.ui_render import _deck_tip_lines

from tests.test_overlay_revamp import (
    _Case, _bound, _label_text, _pack_side, _walk,
)


class DeckRowLayoutTest(_Case):
    def test_row_shows_cost_name_rate_only(self) -> None:
        snap = self._deck_snap(12)
        snap["deck"]["cards"][0].update({"type_cn": "随从", "func_tags": ["治疗"]})
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        labels = [w for w in _walk(self.ov._deck_list) if isinstance(_label_text(w), str)]
        texts = [_label_text(w) for w in labels]
        name = next(t for t in texts if "第1张牌" in t)
        # 名称 Label 只剩名字和剩余张数，类型/功能标签/费用/百分比都不在里面
        self.assertEqual(name.strip(), "第1张牌 ×1")
        self.assertNotIn("[", name)
        self.assertNotIn("%", name)
        # 费用是单独一个 Label，贴在行首（left）
        cost = next(w for w in labels if _label_text(w).strip() == "1")
        self.assertEqual(_pack_side(cost), "left")
        # 抽取率仍是单独 Label，贴在行末（right）
        rate = next(w for w in labels if _label_text(w).strip() == "8%")
        self.assertEqual(_pack_side(rate), "right")

    def test_row_widgets_bind_enter_and_leave(self) -> None:
        snap = self._deck_snap(6)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        rows = [w for w in self.ov._deck_list.winfo_children()]
        self.assertTrue(rows)
        for row in rows:
            # 真 tkinter 的 bind() 不带 seq 时返回当前回调，可用来判定是否已绑
            for w in [row, *list(_walk(row))]:
                self.assertTrue(_bound(w, "<Enter>"), "每行要绑 Enter 才能弹 tooltip")
                self.assertTrue(_bound(w, "<Leave>"), "每行要绑 Leave 才能收 tooltip")


class DeckTipLinesTest(unittest.TestCase):
    def test_tip_has_full_card_text_and_attrs(self) -> None:
        card = Card(id="CS2_029", name="火球术", cost=4, card_class="MAGE",
                    type="SPELL", rarity="FREE", text="<b>造成 $6 点伤害</b>")
        lines = _deck_tip_lines(card, 2, "12%", func_tags=["斩杀", "单解"])
        joined = "\n".join(lines)
        self.assertIn("火球术", joined)
        self.assertIn("4 费", joined)
        self.assertIn("法术", joined)
        self.assertIn("基本", joined)              # 稀有度中文名
        self.assertIn("造成 6 点伤害", joined)     # 卡面文字去掉富文本标记
        self.assertIn("斩杀·单解", joined)
        self.assertIn("剩余 ×2", joined)
        self.assertIn("12%", joined)

    def test_tip_minion_shows_attack_health(self) -> None:
        card = Card(id="X", name="测试随从", cost=3, type="MINION",
                    atk=3, health=2, rarity="RARE")
        joined = "\n".join(_deck_tip_lines(card, 1, "5%"))
        self.assertIn("3/2", joined)
        self.assertIn("稀有", joined)

    def test_tip_weapon_shows_durability(self) -> None:
        card = Card(id="W", name="测试武器", cost=2, type="WEAPON",
                    atk=3, durability=2, rarity="EPIC")
        joined = "\n".join(_deck_tip_lines(card, 1, "5%"))
        self.assertIn("耐久 2", joined)
        self.assertIn("史诗", joined)


if __name__ == "__main__":
    unittest.main()
