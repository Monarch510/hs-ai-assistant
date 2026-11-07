"""动态标签权重接入留牌（DYNAMIC-WEIGHTS.md §3）的回归与行为测试。

第一条钉住「不传 situation 时逐字节不变」：期望值是接入前的真实输出冻结在这里，
谁改旧打分逻辑（费用/对位/重复惩罚）都会立刻红。其余断言用相对大小，不写死浮点：
权重系数是启发式，以后会调，但「对快攻更看重清场/回复、对控制更看重资源/过牌」
这类关系不能变。
"""

from __future__ import annotations

import unittest
from unittest import mock

from hsai import cards as cards_mod
from hsai import mulligan
from hsai.infer import Candidate, Prediction
from hsai.tag_weights import DEFAULT_TAG_WEIGHTS, Situation

# 接入动态权重前跑出来的输出，逐字节冻结（fixture = 固定手牌 + 固定参数）
REGRESSION_HAND_A = ["寒冰箭", "火球术", "森金持盾卫士", "寒冰箭", "暴风雪"]
EXPECTED_A = [
    {"id": "CORE_CS2_024", "name": "寒冰箭", "cost": 2, "action": "keep", "score": 91,
     "reason": "对快攻要有前期场面；单解能处理对方前期核心"},
    {"id": "CORE_CS2_024", "name": "寒冰箭", "cost": 2, "action": "keep", "score": 80,
     "reason": "对快攻要有前期场面；单解能处理对方前期核心；同名牌留一张就够"},
    {"id": "CORE_CS2_179", "name": "森金持盾卫士", "cost": 4, "action": "keep", "score": 62,
     "reason": "嘲讽能挡住打脸"},
    {"id": "CORE_CS2_029", "name": "火球术", "cost": 4, "action": "keep", "score": 59,
     "reason": "单解能处理对方前期核心"},
    {"id": "CORE_CS2_028", "name": "暴风雪", "cost": 6, "action": "toss", "score": 18,
     "reason": "对快攻，高费牌起手太拖；清场虽好，但这个费用起手会卡死"},
]
EXPECTED_B = [
    {"id": "CORE_EX1_606", "name": "盾牌格挡", "cost": 2, "action": "keep", "score": 97,
     "reason": "对位 猎人（形态未知，按快攻处理）；对快攻要有前期场面；回复能撑过爆发"},
    {"id": "FB_Champs_NEW1_012", "name": "法力浮龙", "cost": 1, "action": "keep", "score": 92,
     "reason": "对位 猎人（形态未知，按快攻处理）；对快攻要有前期场面"},
    {"id": "CORE_CS2_033", "name": "水元素", "cost": 4, "action": "toss", "score": 50,
     "reason": "对位 猎人（形态未知，按快攻处理）；你自己的卡组偏快，4 费以上起手偏重"},
]


class TagMulliganTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _hand(self, *names: str):
        hand = [self.db.by_name_or_id(n) for n in names]
        self.assertTrue(all(hand), f"卡牌库缺牌：{[n for n, c in zip(names, hand) if c is None]}")
        return hand

    @staticmethod
    def _pred(tags: list[str]) -> Prediction:
        return Prediction(name="快攻战", candidates=[Candidate("x", "快攻战", 0.9, "战士", tags)])

    def _scores(self, names, **kwargs) -> dict[str, int]:
        rows = mulligan.advise(self._hand(*names), cards=self.db, **kwargs)
        return {r["name"]: r["score"] for r in rows}

    # ---------------------------------------------------------------- 回归
    def test_without_situation_is_byte_identical(self) -> None:
        """不传 situation：输出与接入动态权重前逐字节一致（含理由行、分数、顺序）。"""
        rows = mulligan.advise(
            self._hand(*REGRESSION_HAND_A), cards=self.db, prediction=self._pred(["快攻"])
        )
        self.assertEqual(rows, EXPECTED_A)

        rows = mulligan.advise(
            self._hand("法力浮龙", "水元素", "盾牌格挡"),
            cards=self.db, opp_class="HUNTER", our_tags=["快攻"], is_first=False,
        )
        self.assertEqual(rows, EXPECTED_B)

        # 只给 overrides 不给 situation：动态层不生效，仍是旧行为
        plain = mulligan.advise(self._hand("寒冰箭"), cards=self.db, prediction=self._pred(["快攻"]))
        overridden = mulligan.advise(
            self._hand("寒冰箭"), cards=self.db, prediction=self._pred(["快攻"]),
            tag_weight_overrides={"单解": 9.0},
        )
        self.assertEqual(overridden, plain)

    def test_situation_accepts_dict(self) -> None:
        """Situation 或同键 dict 两种写法等价：调用方从 JSON 拼局势不用先转 dataclass。"""
        obj = Situation(opp_style=frozenset({"快攻"}), our_hp=10, phase="playing")
        as_obj = mulligan.advise(self._hand("暴风雪"), cards=self.db, situation=obj)
        as_dict = mulligan.advise(
            self._hand("暴风雪"), cards=self.db,
            situation={"opp_style": {"快攻"}, "our_hp": 10, "phase": "playing"},
        )
        self.assertEqual(as_dict, as_obj)

    def test_situation_dict_ignores_extra_keys(self) -> None:
        """dict 里带快照额外字段也要能转：按 dataclass 字段反射，不会因为多一个键就崩。"""
        clean = {"opp_style": {"快攻"}, "our_hp": 10, "phase": "playing"}
        noisy = {**clean, "snapshot_only_field": 123, "our_deck_name": "x"}
        self.assertEqual(
            mulligan.advise(self._hand("暴风雪"), cards=self.db, situation=noisy),
            mulligan.advise(self._hand("暴风雪"), cards=self.db, situation=clean),
        )

    def test_dynamic_bonus_capped_at_15(self) -> None:
        """斩杀 + 多标签牌的附加分会超 15，钳制后不能推翻费用基础分。"""
        # 寒冰箭吃 单解/减益/斩杀 三类，lethal 时权重和最大、最容易爆分
        s = Situation(opp_style=frozenset({"快攻"}), opp_hp=1, lethal=True,
                      our_hp=1, our_hand_count=1, our_mana=10, turn=15,
                      opp_board_attack=20, opp_board_count=7, phase="playing")
        base = self._scores(("寒冰箭",))
        up = self._scores(("寒冰箭",), situation=s)
        delta = up["寒冰箭"] - base["寒冰箭"]
        self.assertGreater(delta, 0, "极端局势仍应有正向加权")
        self.assertLessEqual(delta, mulligan.MULLIGAN_BONUS_CAP)

    # ---------------------------------------------------------------- 对位加权
    def test_vs_aggro_low_hp_boosts_aoe_and_heal(self) -> None:
        """对快攻且我方血线低：群解（暴风雪）和治疗（治疗波）分数高于无局势时。"""
        names = ("暴风雪", "治疗波", "森金持盾卫士")
        base = self._scores(names)
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10, opp_board_attack=6,
                      opp_board_count=3, phase="playing")
        up = self._scores(names, situation=s)
        self.assertGreater(up["暴风雪"], base["暴风雪"])
        self.assertGreater(up["治疗波"], base["治疗波"])
        # 普通站场随从也加分，但解场/回复的加权应更明显
        self.assertGreater(up["暴风雪"] - base["暴风雪"],
                           up["森金持盾卫士"] - base["森金持盾卫士"])
        self.assertGreater(up["治疗波"] - base["治疗波"],
                           up["森金持盾卫士"] - base["森金持盾卫士"])

    def test_vs_control_low_hand_boosts_resource_and_draw(self) -> None:
        """对控制且手牌少：资源（不稳定的传送门）和抽牌（奥术智慧）分数高于无局势时。"""
        names = ("奥术智慧", "不稳定的传送门")
        base = self._scores(names)
        s = Situation(opp_style=frozenset({"控制"}), our_hand_count=1, our_mana=2,
                      turn=8, phase="playing")
        up = self._scores(names, situation=s)
        for name in names:
            self.assertGreater(up[name], base[name], name)

    def test_tag_weight_reason_line(self) -> None:
        """理由行出现动态权重的人话说明，且仍截断到 3 句。"""
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10, phase="playing")
        row = mulligan.advise(self._hand("暴风雪"), cards=self.db, situation=s)[0]
        self.assertIn("快攻", row["reason"])
        self.assertIn("加权", row["reason"])
        self.assertLessEqual(len(row["reason"].split("；")), 3)

    def test_overrides_apply_on_top_of_situation(self) -> None:
        """tag_weight_overrides 只局部覆盖权重表，作用在局势修正之前。"""
        names = ("寒冰箭", "暴风雪")
        s = Situation(opp_style=frozenset({"快攻"}), phase="mulligan")
        base = self._scores(names, situation=s)
        up = self._scores(names, situation=s, tag_weight_overrides={"单解": 3.0})
        self.assertGreater(up["寒冰箭"], base["寒冰箭"])
        self.assertEqual(up["暴风雪"], base["暴风雪"], "没有单解标签的牌不该被影响")

    # ---------------------------------------------------------------- 打分公式
    def test_score_formula_with_stubbed_weights(self) -> None:
        """用 mock 把 tag_weights 换成固定表，单独验 score += SCALE * sum(权重[t])。

        A 的权重表以后会调系数，这条不依赖它的具体数值：只钉住「全 11 类求和再乘缩放」
        这个接入口径，且不改动 A 的任何文件。
        """
        stub_w = {t: 0.0 for t in DEFAULT_TAG_WEIGHTS}  # 固定 11 键
        stub_w["群解"] = 2.0  # 暴风雪只吃「群解」，期望附加 = 3 * 2 = 6
        s = Situation(opp_style=frozenset({"快攻"}), phase="mulligan")
        with mock.patch.object(mulligan, "tag_weights", return_value=stub_w), \
             mock.patch.object(mulligan, "explain", return_value=["对快攻，解场牌加权"]):
            base = mulligan.advise(self._hand("暴风雪"), cards=self.db)[0]["score"]
            up = mulligan.advise(self._hand("暴风雪"), cards=self.db, situation=s)[0]["score"]
        self.assertEqual(up - base, round(mulligan.MULLIGAN_SCALE * 2.0))

    # ---------------------------------------------------------------- 基础分
    def test_duplicate_and_cost_base_unchanged(self) -> None:
        """动态叠加不改变重复惩罚和费用基础分：两者仍是可解释的固定项。"""
        s = Situation(opp_style=frozenset({"快攻"}), our_hp=10, phase="playing")

        rows = mulligan.advise(self._hand("寒冰箭", "寒冰箭"), cards=self.db, situation=s)
        self.assertEqual(abs(rows[0]["score"] - rows[1]["score"]),
                         abs(mulligan.DEFAULT_WEIGHTS["duplicate_penalty"]))

        # 同标签、不同费用的两张白板随从：分数差 = 费用基础分差
        scores = self._scores(("鱼人袭击者", "冰风雪人"), situation=s)
        expected = mulligan.DEFAULT_WEIGHTS["cost_1"] - mulligan.DEFAULT_WEIGHTS["cost_4"]
        self.assertEqual(scores["鱼人袭击者"] - scores["冰风雪人"], expected)


if __name__ == "__main__":
    unittest.main()
