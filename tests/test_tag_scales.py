"""动态标签权重的缩放配置：data/tag_weights.json 的 mulligan_scale / play_scale。

坏值必须回退到模块常量（mulligan.MULLIGAN_SCALE / policy.TAG_WEIGHT_SCALE）：
手写文件里写错一个 0、负数或字符串，会让留牌/出牌的标签附加分整项消失或反号，
而且没有任何报错 —— 只能靠读配置这一层挡住。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor, cards as cards_mod, mulligan, policy
from hsai.advisor import Advisor
from hsai.infer import Prediction
from hsai.state import replay_log_text
from hsai.tag_weights import Situation

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"
MULLIGAN_LINES = 40


def _plan_stub() -> dict:
    return {"plan": [], "gain": 0.0, "alternatives": [],
            "lethal": {"lethal": False, "damage": 0}, "final": None}


class ScaleConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _with_config(self, path: Path) -> Advisor:
        with mock.patch.object(advisor, "TAG_WEIGHTS_JSON", path):
            return Advisor(self.db)

    def _advisor(self, payload: dict | str) -> Advisor:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tag_weights.json"
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                            encoding="utf-8")
            return self._with_config(path)

    def test_reads_scales_from_config(self) -> None:
        adv = self._advisor({"weights": {"单解": 1.5}, "mulligan_scale": 2.0, "play_scale": 0.5})
        self.assertEqual((adv.tag_mulligan_scale, adv.tag_play_scale), (2.0, 0.5))
        self.assertEqual(adv.tag_weight_overrides, {"单解": 1.5})

    def test_bad_values_fall_back_to_constants(self) -> None:
        for payload in (
            {"mulligan_scale": 0, "play_scale": -2},            # 非正数
            {"mulligan_scale": "3.5", "play_scale": True},       # 字符串 / 布尔
            {"mulligan_scale": float("nan"), "play_scale": float("inf")},  # 非有限
            {"mulligan_scale": None, "play_scale": [1.0]},       # 坏形状
        ):
            with self.subTest(payload=payload):
                adv = self._advisor(payload)
                self.assertEqual(adv.tag_mulligan_scale, mulligan.MULLIGAN_SCALE)
                self.assertEqual(adv.tag_play_scale, policy.TAG_WEIGHT_SCALE)

    def test_missing_keys_fall_back_to_constants(self) -> None:
        adv = self._advisor({"weights": {"单解": 1.5}})
        self.assertEqual(adv.tag_mulligan_scale, mulligan.MULLIGAN_SCALE)
        self.assertEqual(adv.tag_play_scale, policy.TAG_WEIGHT_SCALE)

    def test_bad_json_falls_back_to_constants(self) -> None:
        adv = self._advisor("{not json")
        self.assertEqual((adv.tag_mulligan_scale, adv.tag_play_scale),
                         (mulligan.MULLIGAN_SCALE, policy.TAG_WEIGHT_SCALE))
        self.assertEqual(adv.tag_weight_overrides, {})


class AdviseScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.hand = [cls.db.by_name_or_id(name) for name in ("火球术", "暴风雪", "治疗波")]

    def _advise(self, **kwargs):
        situation = Situation(phase="mulligan", opp_class="HUNTER")   # 对快攻，解场/回复加权
        rows = mulligan.advise(self.hand, cards=self.db, situation=situation, **kwargs)
        return {row["name"]: row for row in rows}

    def test_default_equals_explicit_default(self) -> None:
        """不传 tag_scale 与显式传 MULLIGAN_SCALE 必须逐字节一致。"""
        self.assertEqual(self._advise(), self._advise(tag_scale=mulligan.MULLIGAN_SCALE))

    def test_tag_scale_changes_bonus(self) -> None:
        default = self._advise()
        scaled = self._advise(tag_scale=1.0)
        self.assertGreater(default["暴风雪"]["score"], scaled["暴风雪"]["score"])
        self.assertGreater(default["火球术"]["score"], scaled["火球术"]["score"])
        self.assertGreater(default["治疗波"]["score"], scaled["治疗波"]["score"])

    def test_bonus_capped_regardless_of_scale(self) -> None:
        """tag_scale 放大后仍受 MULLIGAN_BONUS_CAP 钳制：追加分不会超过 15。"""
        situation = Situation(phase="mulligan")
        huge = {"单解": 6.0, "斩杀": 6.0}       # 基础权重和 12，×6 = 72 > CAP
        hand = [self.db.by_name_or_id("火球术")]

        def score(scale: float) -> float:
            rows = mulligan.advise(hand, cards=self.db, situation=situation,
                                   tag_weight_overrides=huge, tag_scale=scale)
            return rows[0]["score"]

        self.assertEqual(score(6.0) - score(0.0), round(mulligan.MULLIGAN_BONUS_CAP))
        self.assertEqual(score(100.0), score(6.0), "超过 CAP 后继续放大不应该再涨")


class SnapshotScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.text = FIXTURE.read_text(encoding="utf-8")

    def _state(self, *, mulligan_phase: bool = False):
        text = "\n".join(self.text.splitlines()[:MULLIGAN_LINES]) if mulligan_phase else self.text
        return replay_log_text(text, self.db)

    def _with_config(self, path: Path) -> Advisor:
        with mock.patch.object(advisor, "TAG_WEIGHTS_JSON", path):
            return Advisor(self.db)

    def _advisor(self, payload: dict) -> Advisor:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tag_weights.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return self._with_config(path)

    def test_snapshot_exposes_scales(self) -> None:
        snap = self._advisor({"mulligan_scale": 2.0, "play_scale": 0.5}).snapshot(
            self._state(mulligan_phase=True))
        table = snap["tag_weights"]
        self.assertEqual((table["mulligan_scale"], table["play_scale"]), (2.0, 0.5))

    def test_snapshot_default_scales_without_config(self) -> None:
        adv = self._with_config(Path("__no_such_tag_weights__.json"))
        snap = adv.snapshot(self._state(mulligan_phase=True))
        table = snap["tag_weights"]
        self.assertEqual(table["mulligan_scale"], mulligan.MULLIGAN_SCALE)
        self.assertEqual(table["play_scale"], policy.TAG_WEIGHT_SCALE)

    def test_mulligan_advise_receives_mulligan_scale(self) -> None:
        adv = self._advisor({"mulligan_scale": 2.0, "play_scale": 0.5})
        with mock.patch.object(mulligan, "advise", return_value=[]) as fake:
            adv.snapshot(self._state(mulligan_phase=True))
        self.assertEqual(fake.call_args.kwargs["tag_scale"], 2.0)

    def test_plan_turn_receives_play_scale(self) -> None:
        adv = self._advisor({"mulligan_scale": 2.0, "play_scale": 0.5})
        with mock.patch.object(policy, "plan_turn", return_value=_plan_stub()) as fake, \
                mock.patch.object(policy, "advice_lines", return_value=[]):
            adv.snapshot(self._state())
        self.assertEqual(fake.call_args.kwargs["tag_scale"], 0.5)

    def test_advice_tag_scale_passthrough(self) -> None:
        adv = self._with_config(Path("__no_such_tag_weights__.json"))
        pred = Prediction(name="未知卡组")
        with mock.patch.object(policy, "plan_turn", return_value=_plan_stub()) as fake, \
                mock.patch.object(policy, "advice_lines", return_value=[]):
            adv._advice(self._state(), pred, tag_scale=0.25)
        self.assertEqual(fake.call_args.kwargs["tag_scale"], 0.25)

    def test_advice_defaults_to_play_scale(self) -> None:
        adv = self._advisor({"play_scale": 0.5})
        pred = Prediction(name="未知卡组")
        with mock.patch.object(policy, "plan_turn", return_value=_plan_stub()) as fake, \
                mock.patch.object(policy, "advice_lines", return_value=[]):
            adv._advice(self._state(), pred)
        self.assertEqual(fake.call_args.kwargs["tag_scale"], 0.5)


if __name__ == "__main__":
    unittest.main()
