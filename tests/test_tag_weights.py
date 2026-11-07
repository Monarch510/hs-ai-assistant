"""动态标签权重：钉住合同 §2 的行为（DYNAMIC-WEIGHTS.md）。

断言一律用权重的相对大小或与基础表比较，不写死任何浮点结果 ——
系数是启发式，以后会调，但「对快攻该更偏清场」这类关系不能变。
只用标准库、不读 data/、不联网。
"""

from __future__ import annotations

import math
import unittest

from hsai.tag_rules import TAG_ORDER
from hsai.tag_weights import (
    DEFAULT_TAG_WEIGHTS,
    Situation,
    explain,
    tag_weights,
)


class TagWeightsTest(unittest.TestCase):
    def test_keys_order_and_base_table(self):
        """键数固定跟 TAG_ORDER 一致、顺序相同；中性局势就是基础表。"""
        base_before = dict(DEFAULT_TAG_WEIGHTS)
        self.assertEqual(list(DEFAULT_TAG_WEIGHTS), list(TAG_ORDER))
        self.assertEqual(len(DEFAULT_TAG_WEIGHTS), len(TAG_ORDER))

        for s in (Situation(), Situation(phase="mulligan")):
            w = tag_weights(s)
            self.assertEqual(list(w), list(TAG_ORDER))
            self.assertEqual(w, DEFAULT_TAG_WEIGHTS)
            self.assertTrue(all(isinstance(v, float) for v in w.values()))
        self.assertEqual(DEFAULT_TAG_WEIGHTS, base_before)

    def test_vs_aggro_low_hp(self):
        """对快攻 + 我方血线低：群解/单解/治疗/护甲相对高，高费补牌相对低。"""
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10,
                      opp_board_attack=6, opp_board_count=3)
        w = tag_weights(s)
        b = DEFAULT_TAG_WEIGHTS
        for tag in ("群解", "单解", "治疗", "护甲"):
            self.assertGreater(w[tag], b[tag], tag)
        self.assertGreater(w["群解"], w["资源"])
        self.assertGreater(w["群解"], w["抽牌"])
        self.assertGreater(w["治疗"], w["斩杀"])
        self.assertGreater(w["护甲"], w["抽牌"])
        self.assertLess(w["资源"], b["资源"])
        self.assertLess(w["抽牌"], b["抽牌"])

    def test_low_opp_hp_with_lethal(self):
        """对面 5 血且能斩杀：斩杀权重压倒性置顶，解场降权。"""
        s = Situation(opp_hp=5, lethal=True)
        w = tag_weights(s)
        self.assertEqual(w["斩杀"], max(w.values()))
        for tag in TAG_ORDER:
            if tag != "斩杀":
                self.assertGreater(w["斩杀"], w[tag], tag)
        self.assertLess(w["群解"], DEFAULT_TAG_WEIGHTS["群解"])
        self.assertLess(w["单解"], DEFAULT_TAG_WEIGHTS["单解"])

    def test_vs_control_few_cards(self):
        """对控制 + 手牌少/有余费/后期：资源与抽牌最高，解场相对低。"""
        s = Situation(opp_style=frozenset({"控制"}), our_hand_count=1,
                      our_mana=2, turn=8)
        w = tag_weights(s)
        b = DEFAULT_TAG_WEIGHTS
        for tag in ("资源", "抽牌", "斩杀"):
            self.assertGreater(w[tag], b[tag], tag)
        self.assertGreater(w["资源"], w["群解"])
        self.assertGreater(w["抽牌"], w["单解"])
        self.assertGreater(w["资源"], w["站场"])
        self.assertAlmostEqual(w["资源"], w["抽牌"])
        self.assertLess(w["群解"], b["群解"])
        self.assertLess(w["单解"], b["单解"])

    def test_mulligan_ignores_board(self):
        """留牌阶段忽略场面信号：一堆极端场面数字完全不影响结果。"""
        board = dict(
            our_hp=8, opp_hp=6, our_board_attack=9, opp_board_attack=9,
            our_board_count=5, opp_board_count=5, our_hand_count=1,
            our_mana=5, turn=12, aoe_risk=0.9, removal_risk=0.9, lethal=True,
        )
        m = tag_weights(Situation(phase="mulligan", **board))
        clean = tag_weights(Situation(phase="mulligan"))
        self.assertEqual(m, clean)
        # 同样的场面信号在出牌阶段必须起作用
        p = tag_weights(Situation(phase="playing", **board))
        self.assertNotEqual(m, p)
        self.assertNotEqual(p["群解"], clean["群解"])

    def test_class_style_fallback(self):
        """没给对位形态时复用 mulligan.CLASS_STYLE 的职业兜底。"""
        s = Situation(opp_class="HUNTER", phase="mulligan")  # 猎人 = 快攻
        w = tag_weights(s)
        self.assertGreater(w["群解"], DEFAULT_TAG_WEIGHTS["群解"])
        self.assertGreater(w["治疗"], DEFAULT_TAG_WEIGHTS["治疗"])
        self.assertTrue(any("职业取向" in line for line in explain(s)))

    def test_overrides(self):
        """两种写法覆盖单个键、其他键用默认、未知键忽略、不改全局基础表。"""
        base_before = dict(DEFAULT_TAG_WEIGHTS)
        n = Situation(phase="mulligan")
        v = base_before["群解"] + 3.0
        w1 = tag_weights(n, {"单解": v})
        w2 = tag_weights(n, {"tag_单解": v})
        self.assertEqual(w1, w2)
        self.assertEqual(w1["单解"], v)
        self.assertEqual(w1["群解"], base_before["群解"])
        for tag in TAG_ORDER:
            if tag != "单解":
                self.assertEqual(w1[tag], base_before[tag], tag)

        w3 = tag_weights(n, {"单解": v, "不存在的键": 99.0})
        self.assertEqual(w3, w1)

        # 有局势修正时，覆盖仍作用在基础分上，其他键的动态结果不变
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10)
        w0 = tag_weights(s)
        w4 = tag_weights(s, {"单解": v})
        self.assertGreater(w4["单解"], w0["单解"])
        self.assertEqual(w4["群解"], w0["群解"])
        self.assertEqual(DEFAULT_TAG_WEIGHTS, base_before)

    def test_clamp(self):
        """连续信号钳制在 [-1,+1]：超出范围的极端值等价于边界值，不会爆分。"""
        self.assertEqual(tag_weights(Situation(our_hp=10)),
                         tag_weights(Situation(our_hp=-500)))
        self.assertEqual(
            tag_weights(Situation(our_board_count=4, aoe_risk=1.0)),
            tag_weights(Situation(our_board_count=4, aoe_risk=100.0)),
        )
        extreme = tag_weights(Situation(
            opp_style=frozenset({"快攻"}), our_hp=-999, opp_hp=-999,
            our_board_attack=999, opp_board_attack=999,
            our_board_count=99, opp_board_count=99, our_hand_count=-5,
            our_mana=999, turn=999, aoe_risk=99, removal_risk=99, lethal=True,
        ))
        self.assertEqual(list(extreme), list(TAG_ORDER))
        self.assertTrue(all(math.isfinite(x) for x in extreme.values()))
        # 再极端也只是多个 ±1 信号的线性叠加，不会比基础表高出一个量级以上
        ceiling = max(DEFAULT_TAG_WEIGHTS.values()) * 20
        self.assertLess(max(extreme.values()), ceiling)
        self.assertGreater(min(extreme.values()), -ceiling)

    def test_pure_and_reproducible(self):
        """纯函数：同输入同输出，不留下可变状态。"""
        s = Situation(our_hp=12, opp_style=frozenset({"控制"}),
                      turn=5, our_mana=1, our_hand_count=2)
        self.assertEqual(tag_weights(s), tag_weights(s))
        self.assertEqual(explain(s), explain(s))
        self.assertEqual(list(tag_weights(s)), list(TAG_ORDER))

    def test_explain_is_human(self):
        """explain 返回非空人话：说清局势原因，不裸输出数字，无信号时也有兜底句。"""
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10,
                      lethal=True, phase="playing")
        lines = explain(s)
        self.assertIsInstance(lines, list)
        self.assertTrue(lines)
        self.assertTrue(all(isinstance(x, str) and x for x in lines))
        self.assertFalse(any(x.strip().replace(".", "", 1).isdigit() for x in lines))
        joined = "｜".join(lines)
        for word in ("快攻", "血线", "斩杀", "清场"):
            self.assertIn(word, joined)

        self.assertTrue(any("留牌" in x for x in explain(Situation(phase="mulligan"))))
        self.assertTrue(any("基础权重" in x for x in explain(Situation())))
        # 解释与权重同源：能斩杀时解释里必须提到斩杀加权
        self.assertIn("斩杀", "｜".join(explain(Situation(lethal=True))))


if __name__ == "__main__":
    unittest.main()
