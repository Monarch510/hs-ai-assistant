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

def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path




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
        """谁都不认识的牌（偷来的牌）不该拉低所有原型的覆盖率。

        幸运币/衍生物在 retrospect 里已按 is_token 滤掉，不再进 seen；
        这里用一个真正陌生的牌验证「不认识也不罚」。
        """
        t = self.p.retrospect("MAGE", self.a_ids + ["STEAL_001"])
        self.assertAlmostEqual(t.coverage, 1.0)
        self.assertIn("STEAL_001", t.unexplained)

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
    """先验校准只认可信真值，且真值按**当前**原型库重算。"""

    def setUp(self) -> None:
        self.db = cards_mod.load()
        pool = [c.id for c in self.db.standard_pool("WARRIOR")
                if c.name and not self.db.is_token(c.id)]
        self.a_ids, self.b_ids = pool[:3], pool[3:6]
        self.meta = meta.MetaDB([
            meta.Archetype(id="a", name="战士·甲", card_class="WARRIOR", prior=0.5,
                           cards={c: 2 for c in self.a_ids}),
            meta.Archetype(id="b", name="战士·乙", card_class="WARRIOR", prior=0.5,
                           cards={c: 2 for c in self.b_ids}),
        ])
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rows: list[dict]) -> Path:
        return _write_jsonl(self.tmp / "obs.jsonl", rows)

    def test_revealed_wins_over_stale_stored_truth(self) -> None:
        """存下来的 truth 指向一个已经不存在的 id，但 revealed 明显是甲。

        原型库重建过之后这是常态：id 变了、存的标签就指向死链。校准必须按
        **当前**原型库重算，否则这些局全部作废，先验永远学不动。
        """
        rows = [{"opp_class": "WARRIOR", "revealed": self.a_ids,
                 "truth": {"arch": "learned_战士_已删除", "trusted": True}}] * 6
        learn.calibrate_priors(self.meta, self.db, self._write(rows))
        a, b = self.meta.get("a"), self.meta.get("b")
        self.assertGreater(a.prior, b.prior * 1.5,
                           "应该按 revealed 重算成甲，而不是信那个死链 id")

    def test_rows_without_revealed_fall_back_to_stored_truth(self) -> None:
        """连 revealed 都没有的老行，只能退回读存下来的 truth。"""
        rows = [{"opp_class": "WARRIOR", "truth": {"arch": "a", "trusted": True}}] * 6
        learn.calibrate_priors(self.meta, self.db, self._write(rows))
        self.assertGreater(self.meta.get("a").prior, self.meta.get("b").prior * 1.5)

    def test_prediction_only_rows_are_ignored(self) -> None:
        """只有预测值（老格式）的行一律跳过 —— 拿预测当真值会自我确认。"""
        rows = [{"opp_class": "WARRIOR", "opp_archetype": "b"}] * 6
        rows += [{"opp_class": "WARRIOR", "truth": {"arch": "a", "trusted": True}}] * 6
        learn.calibrate_priors(self.meta, self.db, self._write(rows))
        a, b = self.meta.get("a"), self.meta.get("b")
        self.assertGreater(a.prior, b.prior * 1.5,
                           "老格式里的预测值不该被当成真值统计")

    def test_untrusted_truth_is_ignored(self) -> None:
        rows = [{"opp_class": "WARRIOR", "truth": {"arch": "a", "trusted": False}}] * 8
        self.assertEqual(learn.calibrate_priors(self.meta, self.db, self._write(rows))["updated"], [])

    def test_repeated_calibration_is_idempotent(self) -> None:
        """同一个命令跑三遍，先验不能一遍比一遍更偏。

        以前每次都在「当前先验」上再混一次，跑三遍就把先验洗成观测频率
        （实测 0.76 → 0.87 → 0.91 → 0.92），而且没有任何提示。
        """
        path = self._write([{"opp_class": "WARRIOR", "revealed": self.a_ids}] * 6)
        learn.calibrate_priors(self.meta, self.db, path)
        once = {a.id: a.prior for a in self.meta}
        for _ in range(2):
            learn.calibrate_priors(self.meta, self.db, path)
        again = {a.id: a.prior for a in self.meta}
        for aid in once:
            self.assertAlmostEqual(once[aid], again[aid], places=6,
                                   msg=f"{aid} 的先验在重复校准中继续漂移")

    def test_prior_base_is_frozen_at_original_value(self) -> None:
        """校准起点要钉在原始先验上，不能钉在上一轮的校准结果上。"""
        path = self._write([{"opp_class": "WARRIOR", "revealed": self.a_ids}] * 6)
        learn.calibrate_priors(self.meta, self.db, path)
        a = self.meta.get("a")
        self.assertAlmostEqual(a.prior_base, 0.5, places=6)
        self.assertNotAlmostEqual(a.prior, a.prior_base, places=3,
                                  msg="校准应该真的改变了 prior")

    def test_schema2_list_revealed_is_accepted(self) -> None:
        """schema 2 的 revealed 是 [[id, 回合]]，从 json 读出来是 list 不是 tuple。

        只判 tuple 会把 list 当成 card_id 塞进 Counter，抛 unhashable。
        """
        rows = [{"opp_class": "WARRIOR",
                 "revealed": [[cid, 2] for cid in self.a_ids]} for _ in range(6)]
        path = self._write(rows)
        t = infer.DeckPredictor(self.meta, self.db).retrospect(
            "WARRIOR", json.loads(path.read_text(encoding="utf-8").splitlines()[0])["revealed"])
        self.assertEqual(t.archetype_id, "a")
        self.assertTrue(t.trusted)


class BackfillTruthTest(unittest.TestCase):
    """历史回填：把 schema 2 之前那些没有 truth 的行补上。"""

    def setUp(self) -> None:
        self.db = cards_mod.load()
        pool = [c.id for c in self.db.standard_pool("WARRIOR")
                if c.name and not self.db.is_token(c.id)]
        self.a_ids = pool[:3]
        self.meta = meta.MetaDB([
            meta.Archetype(id="a", name="战士·甲", card_class="WARRIOR", prior=1.0,
                           cards={c: 2 for c in self.a_ids}),
        ])
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "observations.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _old_rows(self, n: int = 3) -> None:
        """老格式：只有 opp_archetype（预测值），没有 truth，revealed 是纯 id 数组。"""
        rows = [{"ts": "2026-01-01T00:00:00", "opp_class": "WARRIOR",
                 "opp_archetype": "b", "result": "win", "revealed": self.a_ids}
                for _ in range(n)]
        _write_jsonl(self.path, rows)

    def test_fills_missing_truth(self) -> None:
        self._old_rows()
        report = learn.backfill_truth(self.db, self.meta, observations_path=self.path)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["filled"], 3)
        self.assertTrue(report["written"])

        row = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["truth"]["arch"], "a", "回填应该按 revealed 重算，不是抄预测值")
        self.assertTrue(row["truth"]["trusted"])

    def test_other_fields_are_preserved(self) -> None:
        self._old_rows()
        learn.backfill_truth(self.db, self.meta, observations_path=self.path)
        row = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["ts"], "2026-01-01T00:00:00")
        self.assertEqual(row["opp_archetype"], "b", "老字段原样留着，不替用户删")
        self.assertEqual(row["result"], "win")

    def test_dry_run_does_not_write(self) -> None:
        self._old_rows()
        before = self.path.read_text(encoding="utf-8")
        report = learn.backfill_truth(self.db, self.meta, observations_path=self.path, dry_run=True)
        self.assertFalse(report["written"])
        self.assertEqual(before, self.path.read_text(encoding="utf-8"))

    def test_backup_is_kept(self) -> None:
        self._old_rows()
        before = self.path.read_text(encoding="utf-8")
        learn.backfill_truth(self.db, self.meta, observations_path=self.path)
        backup = self.path.with_suffix(".jsonl.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(before, backup.read_text(encoding="utf-8"))

    def test_broken_line_is_preserved(self) -> None:
        self._old_rows(1)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("{ 这不是合法 JSON\n")
        learn.backfill_truth(self.db, self.meta, observations_path=self.path)
        self.assertIn("这不是合法 JSON", self.path.read_text(encoding="utf-8"),
                      "解析不了的行要原样留着，不能替用户丢数据")


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


class RebuildTest(unittest.TestCase):
    """重建只动学出来的原型，且不再把衍生物当成卡组牌。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        pool = [c.id for c in cls.db.standard_pool("WARRIOR")
                if c.name and not cls.db.is_token(c.id)]
        cls.deck, cls.seed_cards = pool[:6], pool[6:8]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _obs(self, rows: list[dict]) -> Path:
        return _write_jsonl(self.tmp / "obs.jsonl", rows)

    def test_drops_only_learned_archetypes(self) -> None:
        """导入的卡组是外部输入，重建不该碰；老文件里遗留的 seed 也不该碰。"""
        m = meta.MetaDB([
            meta.Archetype(id="seed_x", name="旧种子", card_class="WARRIOR",
                           prior=0.5, cards={c: 2 for c in self.seed_cards},
                           source="seed"),
            meta.Archetype(id="deck_y", name="导入的", card_class="WARRIOR",
                           prior=0.3, cards={"STALE": 2}, source="deckcode"),
            meta.Archetype(id="learned_warrior_9", name="旧学出来的",
                           card_class="WARRIOR", prior=0.2, cards={"STALE": 2},
                           source="learned"),
        ])
        path = self._obs([{"opp_class": "WARRIOR", "revealed": self.deck}] * 3)
        report = learn.rebuild_learned(self.db, m, min_games=2, observations_path=path)

        ids = {a.id for a in m}
        self.assertIn("seed_x", ids, "老数据里可能还留着 source=seed 的文件，重建不该删")
        self.assertIn("deck_y", ids)
        self.assertNotIn("learned_warrior_9", ids)
        self.assertEqual(len(report["removed"]), 1)
        self.assertGreaterEqual(report["added"], 1, "重建应该从记录里重新长出原型")

    def test_tokens_never_enter_the_template(self) -> None:
        """历史数据里 28% 的 revealed 是召唤物，重建时必须滤掉。"""
        rows = [{"opp_class": "WARRIOR",
                 "revealed": self.deck + ["ICC_026t", "GAME_005"]}] * 3
        m = meta.MetaDB([])
        learn.rebuild_learned(self.db, m, min_games=2,
                              observations_path=self._obs(rows))
        built = next(a for a in m if a.source == "learned")
        self.assertNotIn("ICC_026t", built.cards)
        self.assertNotIn("GAME_005", built.cards)
        self.assertEqual(set(built.cards), set(self.deck))

    def test_ids_are_content_based_and_stable(self) -> None:
        """重建两次，学出来的 id 必须一模一样。

        以前 id 是「同职业第几个」，删/加一个原型就让其余全部平移 ——
        而 observations 里的 truth.arch 是按 id 记的，历史真值当场对不上。
        """
        rows = [{"opp_class": "WARRIOR", "revealed": self.deck}] * 3
        first = meta.build_from_observations(rows, self.db, min_games=2)
        second = meta.build_from_observations(rows, self.db, min_games=2)
        self.assertEqual([a.id for a in first], [a.id for a in second])
        self.assertRegex(first.archetypes[0].id, r"^warrior_[0-9a-f]{8}$")

    def test_different_decks_get_different_ids(self) -> None:
        """内容不同 id 就必须不同，否则两套牌会互相覆盖。"""
        a = meta.content_id("WARRIOR", {self.deck[0]: 2})
        b = meta.content_id("WARRIOR", {self.deck[1]: 2})
        self.assertNotEqual(a, b)

    def test_id_ignores_dict_order(self) -> None:
        a = meta.content_id("WARRIOR", {self.deck[0]: 2, self.deck[1]: 1})
        b = meta.content_id("WARRIOR", {self.deck[1]: 1, self.deck[0]: 2})
        self.assertEqual(a, b, "同一套牌，插入顺序不同不能算出两个 id")


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
