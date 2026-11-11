"""advisor 组装局势：快照暴露 12 类动态权重，留牌/出牌按合同拿到 Situation/权重。

B/C 的打分逻辑各有自己的测试，这里只钉组装这一层：传给 mulligan.advise /
policy.plan_turn 的参数、snap["tag_weights"] 的内容，以及配置缺失/损坏时的降级。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor, cards as cards_mod, mulligan, policy
from hsai.advisor import Advisor, board_from_state
from hsai.infer import Candidate, Prediction
from hsai.state import replay_log_text
from hsai.tag_rules import TAG_ORDER
from hsai.tag_weights import DEFAULT_TAG_WEIGHTS, Situation

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"
MULLIGAN_LINES = 40      # 留牌阶段为止的日志前缀；整份会走到对局中

def aggro_prediction() -> Prediction:
    return Prediction(name="快攻战", candidates=[Candidate("x", "快攻战", 0.9, "战士", ["快攻"])])

class TagAdvisorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.text = FIXTURE.read_text(encoding="utf-8")

    def _state(self, *, mulligan_phase: bool = False):
        text = "\n".join(self.text.splitlines()[:MULLIGAN_LINES]) if mulligan_phase else self.text
        return replay_log_text(text, self.db)

    def _advisor(self, tag_cfg: Path | None = None) -> Advisor:
        if tag_cfg is None:
            return Advisor(self.db)
        with mock.patch.object(advisor, "TAG_WEIGHTS_JSON", tag_cfg):
            return Advisor(self.db)

    def test_snapshot_exposes_all_weights_and_reasons(self) -> None:
        snap = Advisor(self.db).snapshot(self._state())
        table = snap["tag_weights"]
        self.assertEqual(list(table["weights"]), list(TAG_ORDER))
        self.assertEqual(len(table["weights"]), len(TAG_ORDER))
        self.assertTrue(table["reasons"])
        self.assertTrue(all(isinstance(line, str) and line for line in table["reasons"]))
        self.assertIn("出牌阶段", table["reasons"][0])

    def test_mulligan_phase_uses_mulligan_reasons(self) -> None:
        snap = Advisor(self.db).snapshot(self._state(mulligan_phase=True))
        self.assertEqual(snap["phase"], "mulligan")
        self.assertIn("留牌阶段", snap["tag_weights"]["reasons"][0])

    def test_mulligan_advise_receives_situation(self) -> None:
        with mock.patch.object(mulligan, "advise", return_value=[]) as fake:
            Advisor(self.db).snapshot(self._state(mulligan_phase=True))
        kwargs = fake.call_args.kwargs
        situation = kwargs["situation"]
        self.assertIsInstance(situation, Situation)
        self.assertEqual(situation.phase, "mulligan")
        self.assertEqual((situation.our_class, situation.opp_class), ("MAGE", "WARRIOR"))
        self.assertEqual(kwargs["tag_weight_overrides"], {})

    def test_plan_turn_receives_tag_weights(self) -> None:
        plan = {"plan": [], "gain": 0.0, "alternatives": [],
                "lethal": {"lethal": False, "damage": 0}, "final": None}
        with mock.patch.object(policy, "plan_turn", return_value=plan) as fake, \
                mock.patch.object(policy, "advice_lines", return_value=[]):
            snap = Advisor(self.db).snapshot(self._state())
        weights = fake.call_args.kwargs["tag_weights"]
        self.assertEqual(list(weights), list(TAG_ORDER))

    def test_situation_numbers_come_from_state(self) -> None:
        st = self._state()
        st.hero(st.our_id).tags["DAMAGE"] = 25
        adv = Advisor(self.db)
        pred = aggro_prediction()
        board = board_from_state(st)
        situation = adv._situation(st, pred, board, phase="playing")
        self.assertEqual((situation.our_hp, situation.our_class), (5, "MAGE"))
        self.assertEqual((situation.opp_hp, situation.opp_class), (30, "WARRIOR"))
        self.assertIn("快攻", situation.opp_style)
        self.assertEqual(situation.our_board_count, len(board.sides[0].board))
        self.assertEqual(situation.our_hand_count, len(board.sides[0].hand))
        self.assertEqual(situation.phase, "playing")
        self.assertFalse(situation.lethal)

    def test_aggro_and_low_hp_raises_defense_lowers_lethal(self) -> None:
        st = self._state()
        st.hero(st.our_id).tags["DAMAGE"] = 25      # 30 - 25 = 5 血
        adv = Advisor(self.db)
        # 隔离本地卡组配置，只留「对快攻 + 我方血线低」这一个信号
        adv.our_tags = []
        adv.our_archetype = ""
        adv.our_deck_name = ""
        with mock.patch.object(Advisor, "_predict", return_value=aggro_prediction()):
            snap = adv.snapshot(st)
        weights = snap["tag_weights"]["weights"]
        self.assertGreater(weights["治疗"], DEFAULT_TAG_WEIGHTS["治疗"])
        self.assertGreater(weights["护甲"], DEFAULT_TAG_WEIGHTS["护甲"])
        self.assertLess(weights["斩杀"], DEFAULT_TAG_WEIGHTS["斩杀"])

    def test_bad_configs_degrade_to_empty(self) -> None:
        # 缺失 / 坏 JSON / 坏形状：三种坏配置都退回空覆盖，且快照仍给全套权重
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "tag_weights.json"
            cases = (
                ("缺失文件", Path("__no_such_tag_weights__.json"), None),
                ("坏 JSON", bad, "{not json"),
                ("坏形状", bad, json.dumps({"weights": ["单解"]})),
            )
            for name, path, payload in cases:
                with self.subTest(name=name):
                    if payload is not None:
                        path.write_text(payload, encoding="utf-8")
                    adv = self._advisor(path)
                    self.assertEqual(adv.tag_weight_overrides, {})
                    snap = adv.snapshot(self._state(mulligan_phase=True))
                    self.assertEqual(len(snap["tag_weights"]["weights"]), len(TAG_ORDER))

    def test_config_overrides_apply_to_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "tag_weights.json"
            cfg.write_text(json.dumps({"weights": {"单解": 3.0}}), encoding="utf-8")
            adv = self._advisor(cfg)
            self.assertEqual(adv.tag_weight_overrides, {"单解": 3.0})
            snap = adv.snapshot(self._state(mulligan_phase=True))
            self.assertGreaterEqual(snap["tag_weights"]["weights"]["单解"], 3.0)

if __name__ == "__main__":
    unittest.main()
