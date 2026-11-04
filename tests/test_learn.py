"""「学习只吃真值」的口径测试。

背景：`observations.jsonl` 里只存过预测值（`opp_archetype`），而先验学习
拿它当真值 —— 预测偏哪套，先验就往哪套偏，越用越偏。这条回路没有纠错力。

这里锁住四件事：
  1. 局末回顾能给出**独立于先验**的真值，并说清可信不可信
  2. 先验校准只统计可信真值，老格式（只有预测值）一律跳过
  3. 观察行把「预测」和「真值」分开存，并带回合与轨迹
  4. 衍生物不进 revealed，否则学出来的原型会变成 40 张的卡组
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hsai import advisor, cards as cards_mod, infer, learn, meta, state
from hsai.infer import Prediction, RetroTruth

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"


class RetrospectTest(unittest.TestCase):
    """局末回顾：用全部已见牌判「它到底是什么」。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        pool = [c.id for c in cls.db.standard_pool("MAGE")]
        cls.a_ids, cls.b_ids = pool[:3], pool[3:6]
        cls.meta = meta.MetaDB([
            meta.Archetype(id="a", name="法师·甲", card_class="MAGE", prior=0.5,
                           cards={c: 2 for c in cls.a_ids}),
            meta.Archetype(id="b", name="法师·乙", card_class="MAGE", prior=0.5,
                           cards={c: 2 for c in cls.b_ids}),
        ])
        cls.p = infer.DeckPredictor(cls.meta, cls.db)

    def test_full_match_is_trusted(self) -> None:
        t = self.p.retrospect("MAGE", self.a_ids)
        self.assertEqual(t.archetype_id, "a")
        self.assertAlmostEqual(t.coverage, 1.0)
        self.assertTrue(t.trusted)

    def test_prior_does_not_decide_the_truth(self) -> None:
        """先验 99:1，但牌全指向 b —— 真值必须是 b。

        这正是旧实现做不到的：它把 `log(prior)` 加进后验，先验越强越难被推翻。
        """
        a, b = self.meta.get("a"), self.meta.get("b")
        a.prior, b.prior = 0.99, 0.01
        try:
            t = self.p.retrospect("MAGE", self.b_ids)
        finally:
            a.prior, b.prior = 0.5, 0.5
        self.assertEqual(t.archetype_id, "b")

    def test_split_evidence_is_not_trusted(self) -> None:
        t = self.p.retrospect("MAGE", self.a_ids[:2] + self.b_ids[:2])
        self.assertFalse(t.trusted, "两套牌各占一半，标签本身就是猜的")

    def test_unrecognised_cards_are_reported_not_penalised(self) -> None:
        """谁都不认识的牌（幸运币、偷来的牌）不该拉低所有原型的覆盖率。"""
        t = self.p.retrospect("MAGE", self.a_ids + ["GAME_005"])
        self.assertAlmostEqual(t.coverage, 1.0)
        self.assertIn("GAME_005", t.unexplained)

    def test_copies_are_capped_by_template(self) -> None:
        """模板里只有 2 张，见到 3 张时第 3 张不能算「已解释」。"""
        t = self.p.retrospect("MAGE", [self.a_ids[0]] * 3)
        self.assertEqual(t.explained, 2)
        self.assertLessEqual(t.coverage, 1.0)

    def test_oversized_template_cannot_win(self) -> None:
        """声明 50 张的模板靠「牌多」就能解释一切，不该参与比较。"""
        pool = [c.id for c in self.db.standard_pool("MAGE")]
        big = meta.Archetype(id="bloat", name="法师·巨型", card_class="MAGE",
                             prior=1.0, cards={c: 2 for c in pool[:21]})
        self.assertGreater(big.total(), meta.DECK_MAX_CARDS)
        t = infer.DeckPredictor(meta.MetaDB([big]), self.db).retrospect("MAGE", pool[:5])
        self.assertEqual(t.archetype_id, "")


class CalibratePriorTest(unittest.TestCase):
    """先验校准只认可信真值。"""

    def setUp(self) -> None:
        self.db = cards_mod.load()
        self.meta = meta.MetaDB([
            meta.Archetype(id="a", name="战士·甲", card_class="WARRIOR", prior=0.5),
            meta.Archetype(id="b", name="战士·乙", card_class="WARRIOR", prior=0.5),
        ])
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rows: list[dict]) -> Path:
        path = self.tmp / "obs.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        return path

    def test_old_rows_without_truth_are_ignored(self) -> None:
        """6 局真值指向 a，6 局老格式（只有预测值）指向 b。

        老行要是被当真值统计，两边会打平；被跳过则 a 明显更高。
        """
        rows = [{"opp_class": "WARRIOR", "truth": {"arch": "a", "trusted": True}}] * 6
        rows += [{"opp_class": "WARRIOR", "opp_archetype": "b"}] * 6
        learn.calibrate_priors(self.meta, self.db, self._write(rows))
        a, b = self.meta.get("a"), self.meta.get("b")
        self.assertGreater(a.prior, b.prior * 1.5,
                           "老格式里的预测值不该被当成真值统计")

    def test_untrusted_truth_is_ignored(self) -> None:
        rows = [{"opp_class": "WARRIOR", "truth": {"arch": "a", "trusted": False}}] * 8
        self.assertEqual(learn.calibrate_priors(self.meta, self.db, self._write(rows)), 0)


class ObservationRowTest(unittest.TestCase):
    """观察行的 schema：预测与真值必须分开。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.text = FIXTURE.read_text(encoding="utf-8")

    def test_prediction_and_truth_are_separate_fields(self) -> None:
        st = state.replay_log_text(self.text, self.db)
        pred = Prediction(name="甲", archetype_id="a", confidence=0.62)
        truth = RetroTruth(archetype_id="b", name="乙", coverage=0.9,
                           explained=9, considered=10)
        row = advisor.observation_row(st, pred, truth, [{"t": 3, "arch": "a", "conf": 0.62}])

        self.assertEqual(row["pred_archetype"], "a")
        self.assertEqual(row["truth"]["arch"], "b")
        self.assertTrue(row["truth"]["trusted"])
        self.assertNotIn("opp_archetype", row, "旧字段把预测值当真值，必须消失")
        self.assertEqual(row["schema"], 2)

    def test_revealed_carries_turn_and_trace_is_kept(self) -> None:
        st = state.replay_log_text(self.text, self.db)
        row = advisor.observation_row(st, Prediction(), RetroTruth(), [{"t": 5}])
        self.assertTrue(row["revealed"], "fixture 里对手确实打出过牌")
        for cid, turn in row["revealed"]:
            self.assertIsInstance(cid, str)
            self.assertIsInstance(turn, int, "带回合才能拟合早期权重")
        self.assertEqual(row["trace"], [{"t": 5}])

    def test_no_truth_yields_null_not_a_guess(self) -> None:
        st = state.replay_log_text(self.text, self.db)
        row = advisor.observation_row(st, Prediction(), RetroTruth(), [])
        self.assertIsNone(row["truth"], "认不出来就写 null，不要编一个标签")


class TokenInRevealedTest(unittest.TestCase):
    """衍生物不是「对手打出的牌」。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.text = FIXTURE.read_text(encoding="utf-8")

    def _play(self, st, card_id: str, eid: int) -> None:
        # 调用约定：e.zone 已经是新区域，prev 传旧区域（见 _apply_tags）
        e = state.Entity(id=eid, card_id=card_id, controller=st.opp_id,
                         zone=state.ZONE_PLAY)
        st.entities[e.id] = e
        st._on_zone_change(e, state.ZONE_HAND, state.ZONE_PLAY)

    def test_summoned_token_is_not_recorded(self) -> None:
        st = state.replay_log_text(self.text, self.db)
        self.assertTrue(self.db.is_token("ICC_026t"), "骷髅是召唤出来的衍生物")
        self._play(st, "ICC_026t", 9001)
        self.assertNotIn("ICC_026t", st.revealed_opp_order)

    def test_real_card_is_still_recorded(self) -> None:
        st = state.replay_log_text(self.text, self.db)
        # 挑一张「有中文名、不是衍生物、fixture 里还没出现过」的真牌
        cid = next(c.id for c in self.db.standard_pool("WARRIOR")
                   if c.name and not self.db.is_token(c.id)
                   and c.id not in st.revealed_opp_order)
        self._play(st, cid, 9002)
        self.assertIn(cid, st.revealed_opp_order)


class RevealedFormatTest(unittest.TestCase):
    """聚类要同时吃得下新旧两种 revealed 写法。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.deck = [c.id for c in cls.db.standard_pool("WARRIOR")][:6]

    def _build(self, make):
        rows = [{"opp_class": "WARRIOR", "revealed": make(i)} for i in range(3)]
        return meta.build_from_observations(rows, self.db, min_games=2)

    def test_reads_both_revealed_shapes(self) -> None:
        old = self._build(lambda i: list(self.deck)).archetypes
        new = self._build(lambda i: [[c, i + 1] for c in self.deck]).archetypes
        self.assertGreater(len(old), 0, "三局同样的牌应该能聚出一个原型")
        self.assertEqual(len(old), len(new))
        self.assertEqual(old[0].cards, new[0].cards)


if __name__ == "__main__":
    unittest.main()
