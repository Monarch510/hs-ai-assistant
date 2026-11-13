"""第 4 轮审查「数据 / 学习 / AI / 元信息层」修复的回归护栏。

只依赖标准库；所有落盘都用 tempfile / mock 改道，绝不碰真实 data/。
对应审查项：D1 按局去重、D2 保留真值、D3 留牌优化目标、D4 原子写+窗口、
D6 读盘守卫、D7 截断保留坏行、D9 原子轮转、D10 predict 兼容 list、
D11 retrospect 过滤 token。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor, cli_learn, learn_absorb, learn_calibrate, learn_optimize
from hsai import learn_weights, logutil, memory, mulligan, review
from hsai import learn as learn_facade
from hsai import meta as meta_mod
from hsai.cards import Card, CardDB
from hsai.infer import DeckPredictor
from hsai.meta import Archetype, MetaDB
from hsai.sim import GameOutcome


# D1：tunings 的 min_count 数是「局」不是「行」，同批（同 game_id）只算一局
class TuningsGameIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            memory, "ADVICE_TUNING_PATH", Path(self._tmp.name) / "tuning.jsonl"
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _row(param: str, delta: float, **extra):
        row = {"kind": "param", "param": param, "delta": delta, "why": "a"}
        row.update(extra)
        return row

    def test_same_game_id_three_rows_is_one_game(self) -> None:
        p = memory.ADVICE_TUNING_PATH
        p.write_text(
            "".join(json.dumps(self._row("tempo", 0.2, game_id="G1")) + "\n" for _ in range(3)),
            encoding="utf-8",
        )
        self.assertNotIn("tempo", memory.summarize_tunings(min_count=3))

    def test_add_tunings_one_call_is_one_game(self) -> None:
        # 审查实跑：一局 add_tunings 三次同 param，不能凑够 3 局
        memory.add_tunings(
            [self._row("tempo", 0.1), self._row("tempo", 0.2), self._row("tempo", 0.15)], {}
        )
        self.assertNotIn("tempo", memory.summarize_tunings(min_count=3))

    def test_three_distinct_games_pass(self) -> None:
        for d in (0.1, 0.2, 0.15):
            memory.add_tunings([self._row("tempo", d)], {})
        out = memory.summarize_tunings(min_count=3)
        self.assertEqual(out["tempo"]["count"], 3)
        self.assertEqual(out["tempo"]["direction"], "up")
        self.assertEqual(out["tempo"]["agree"], 1.0)

    def test_mixed_direction_agree_is_correct(self) -> None:
        for d in (0.2, -0.2, 0.1):
            memory.add_tunings([self._row("tempo", d)], {})
        out = memory.summarize_tunings(min_count=3)
        self.assertEqual(out["tempo"]["count"], 3)
        self.assertEqual(out["tempo"]["direction"], "up")
        self.assertAlmostEqual(out["tempo"]["agree"], 0.67)


# D2：backfill_truth 算不出归属时保留原 truth，别抹成 None
class BackfillTruthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "obs.jsonl"
        self.cards = CardDB([
            Card(id="M1", name="火球", card_class="MAGE", type="SPELL"),
            Card(id="M2", name="寒冰箭", card_class="MAGE", type="SPELL"),
        ])
        self.meta = MetaDB([
            Archetype(id="a", name="法师·甲", card_class="MAGE", prior=1.0,
                      cards={"M1": 2, "M2": 2}),
        ], self.cards)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read_first(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])

    def test_existing_truth_is_preserved_when_no_attribution(self) -> None:
        row = {"opp_class": "MAGE", "truth": {"arch": "xxx", "trusted": True},
               "revealed": [], "unexpected_cards": ["OLD"]}
        self.path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        rep = learn_calibrate.backfill_truth(self.cards, self.meta, observations_path=self.path)
        self.assertTrue(rep["written"])
        got = self._read_first()
        self.assertEqual(got["truth"], {"arch": "xxx", "trusted": True})
        self.assertEqual(got["unexpected_cards"], ["OLD"])

    def test_truth_is_filled_when_revealed_can_be_attributed(self) -> None:
        row = {"opp_class": "MAGE", "revealed": [["M1", 2], ["M2", 3]]}
        self.path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        rep = learn_calibrate.backfill_truth(self.cards, self.meta, observations_path=self.path)
        self.assertEqual(rep["filled"], 1)
        got = self._read_first()
        self.assertTrue(got.get("truth"))
        self.assertEqual(got["truth"]["arch"], "a")


# D4①：meta.save() 用 tmp + os.replace，写一半失败不破坏已存的原型文件
class MetaAtomicWriteTest(unittest.TestCase):
    def test_replace_failure_keeps_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            cards = CardDB([])
            MetaDB([
                Archetype(id="keep", name="保留", card_class="MAGE", prior=1.0,
                          cards={"M1": 1}),
            ], cards).save(path)
            files = sorted(path.glob("*.json"))
            self.assertEqual(len(files), 1)
            before = files[0].read_text(encoding="utf-8")

            changed = MetaDB([
                Archetype(id="keep", name="改过", card_class="MAGE", prior=1.0,
                          cards={"M2": 1}),
            ], cards)
            with mock.patch.object(meta_mod.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    changed.save(path)

            self.assertEqual(files[0].read_text(encoding="utf-8"), before)
            loaded = MetaDB.load(cards, path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded.get("keep").name, "保留")


# D4③：全量重聚类 O(n²)，只取最近 K 局参与
class AbsorbRecentWindowTest(unittest.TestCase):
    def test_only_recent_window_is_clustered(self) -> None:
        rows = [{"opp_class": "MAGE"} for _ in range(1000)]
        captured: dict[str, int] = {}

        def fake_build(obs, cards, *, min_games, threshold):
            captured["len"] = len(obs)
            return []

        with mock.patch.object(learn_absorb, "load_observations", return_value=rows), \
             mock.patch.object(learn_absorb, "build_from_observations", side_effect=fake_build):
            stats_out: dict = {}
            learn_absorb.absorb_observations(CardDB([]), MetaDB([]), stats_out=stats_out)
        self.assertEqual(captured["len"], 500)
        self.assertEqual(stats_out["observations"], 500)


# D6：坏行 isinstance 守卫 + JSONL 坏字节行只让那一行坏掉
class JsonlReadGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "obs.jsonl"
        good = json.dumps(
            {"opp_class": "MAGE", "revealed": [], "truth": {"arch": "x", "trusted": False}},
            ensure_ascii=False,
        )
        raw = (b"null\n[]\n" + b'"a stray string"\n' + b"{not json}\n"
               + good.encode("utf-8") + b"\n" + b"\xff\xfe bad utf8\n"
               + good.encode("utf-8") + b"\n")
        self.path.write_bytes(raw)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loaders_return_only_dict_rows(self) -> None:
        obs = advisor.load_observations(self.path)
        self.assertEqual(len(obs), 2)
        self.assertIsInstance(obs[0], dict)
        games = review.load_games(self.path)
        self.assertEqual(len(games), 2)
        self.assertIsInstance(games[0], dict)
        # 坏字节行只让那一行坏掉，两条好行都还在
        self.assertEqual(len(memory._read_jsonl(self.path)), 2)

    def test_calibrate_priors_does_not_crash_on_bad_bytes(self) -> None:
        rep = learn_calibrate.calibrate_priors(MetaDB([]), CardDB([]), self.path)
        self.assertEqual(rep["rows"], 2)
        self.assertGreaterEqual(rep["total_rows"], 1)


# D7：_append_jsonl 截断按原始行切尾，尾窗内的坏行原样保留
class AppendJsonlTruncationTest(unittest.TestCase):
    def test_truncation_keeps_broken_line_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            path.write_text('{"a":1}\n{"b":2}\n{BROKEN hand-edited line\n', encoding="utf-8")
            memory._append_jsonl(path, [{"d": 4}], cap=2)
            content = path.read_text(encoding="utf-8")
            self.assertIn("{BROKEN hand-edited line", content)
            self.assertIn('"d": 4', content)
            self.assertNotIn('"a":1', content)


# D9：logutil 轮转 tmp + os.replace，写一半失败不破坏原日志
class LogutilAtomicRotateTest(unittest.TestCase):
    def test_replace_failure_keeps_original_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "hsai.log"
            log.write_text("line1\nline2\n", encoding="utf-8")
            original = log.read_text(encoding="utf-8")
            with mock.patch.object(logutil, "LOG_FILE", log), \
                 mock.patch.object(logutil, "MAX_BYTES", 0), \
                 mock.patch.object(logutil.os, "replace", side_effect=OSError("no space")):
                logutil._rotate_if_needed()
            self.assertEqual(log.read_text(encoding="utf-8"), original)


# D10：predict() 的 observed 兼容 schema-2 的 list-of-list
class PredictListObservedTest(unittest.TestCase):
    @staticmethod
    def _predictor() -> DeckPredictor:
        cards = CardDB([Card(id="CS2_029", name="火球", card_class="MAGE", type="SPELL")])
        arch = Archetype(id="a", name="法师·火球", card_class="MAGE", prior=1.0,
                         cards={"CS2_029": 2})
        return DeckPredictor(MetaDB([arch], cards), cards)

    def test_list_observed_is_normalised_to_tuple(self) -> None:
        p = self._predictor()
        with mock.patch.object(p, "posteriors", return_value=[]) as m:
            p.predict("MAGE", [["CS2_029", 2]])
        self.assertEqual(m.call_args[0][1], [("CS2_029", 2)])

    def test_list_observed_does_not_crash(self) -> None:
        p = self._predictor()
        with mock.patch("hsai.memory.class_counts", return_value={}):
            pred = p.predict("MAGE", [["CS2_029", 2]])
        self.assertTrue(hasattr(pred, "name"))


# D11：retrospect() 把 token 从 seen 里滤掉，不进 unexplained、不抬高 coverage
class RetrospectTokenFilterTest(unittest.TestCase):
    def test_tokens_are_filtered_from_seen(self) -> None:
        cards = CardDB([
            Card(id="M1", name="火球", card_class="MAGE", type="SPELL"),
            Card(id="M2", name="寒冰箭", card_class="MAGE", type="SPELL"),
            Card(id="GAME_005", name="幸运币", collectible=False),
            Card(id="ICC_026t", name="冰霜衍生物", collectible=False),
            Card(id="FIRE_TOK", name="火球", collectible=False),  # 同名 token：旧口径会认成模板牌
        ])
        arch = Archetype(id="a", name="法师·甲", card_class="MAGE", prior=1.0,
                         cards={"M1": 2, "M2": 2})
        p = DeckPredictor(MetaDB([arch], cards), cards)
        control = p.retrospect("MAGE", ["M1", "M2"])

        for revealed in (
            ["M1", "M2", "GAME_005", "ICC_026t", "FIRE_TOK"],
            [["M1", 2], ["M2", 3], ["GAME_005", 4], ["ICC_026t", 5], ["FIRE_TOK", 6]],
        ):
            t = p.retrospect("MAGE", revealed)
            self.assertEqual(t.archetype_id, "a")
            self.assertAlmostEqual(t.coverage, 1.0)
            self.assertEqual(t.coverage, control.coverage)
            self.assertEqual(t.considered, control.considered)
            for tok in ("GAME_005", "ICC_026t", "FIRE_TOK"):
                self.assertNotIn(tok, t.unexplained)


# D3①：mirror_winrate 的目标随 candidate_mulligan 变化；爬山不再空转
class MulliganObjectiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cards = CardDB([])
        self.meta = MetaDB([
            Archetype(id="a", name="甲", card_class="MAGE", prior=1.0,
                      cards={"C1": 1, "C2": 1, "C3": 1, "C4": 1}),
        ], self.cards)
        self.base = dict(mulligan.DEFAULT_WEIGHTS)

    def _fake_run_game(self):
        base = self.base

        def fake(deck_a, deck_b, cards, **kw):
            # 候选方是「留牌权重不等于固定参考」的那一侧
            mwa, mwb = kw.get("mulligan_weights_a"), kw.get("mulligan_weights_b")
            cand_on_a = mwa is not None and mwa != base
            cand = mwa if cand_on_a else mwb
            good = bool(cand and cand.get("cost_1", 0) > 0)
            if good:
                winner = 0 if cand_on_a else 1
            else:
                winner = 1 if cand_on_a else 0
            return GameOutcome(winner=winner, turns=1)

        return fake

    def test_mirror_winrate_responds_to_candidate_mulligan(self) -> None:
        with mock.patch.object(learn_optimize.sim, "make_deck", return_value=[]), \
             mock.patch.object(learn_optimize.sim, "run_game", side_effect=self._fake_run_game()):
            bad, _ = learn_optimize.mirror_winrate(
                self.cards, self.meta, {}, {}, games=1,
                mulligan_weights=self.base, candidate_mulligan={"cost_1": 0.0}, seed=7)
            good, _ = learn_optimize.mirror_winrate(
                self.cards, self.meta, {}, {}, games=1,
                mulligan_weights=self.base, candidate_mulligan={"cost_1": 5.0}, seed=7)
        self.assertNotEqual(bad, good)

    def test_optimize_mulligan_accepts_a_candidate(self) -> None:
        best0 = {"cost_1": 0.0}  # 与 DEFAULT_WEIGHTS 不同 → 候选方能被识别
        with mock.patch.object(learn_optimize, "load_mulligan_weights", return_value=best0), \
             mock.patch.object(learn_optimize, "load_weights", return_value={}), \
             mock.patch.object(learn_optimize.sim, "make_deck", return_value=[]), \
             mock.patch.object(learn_optimize.sim, "run_game", side_effect=self._fake_run_game()):
            _w, report = learn_optimize.optimize_mulligan(
                self.cards, self.meta, games=1, rounds=1, seed=7)
        self.assertTrue(report.accepts, "目标随 mw 变化后，爬山应接受至少一个候选")


# D3②：写盘保留用户手写 note；无改善不写盘
class MulliganWriteGateTest(unittest.TestCase):
    def test_save_preserves_user_note_and_other_keys(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mulligan.json"
            path.write_text(
                json.dumps({"weights": {"a": 1}, "note": "我手写的说明", "custom": 42},
                           ensure_ascii=False), encoding="utf-8",
            )
            learn_weights.save_mulligan_weights({"a": 9}, path=path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["weights"], {"a": 9})
            self.assertEqual(payload["note"], "我手写的说明")
            self.assertEqual(payload["custom"], 42)

    def test_cmd_learn_mulligan_no_write_without_improvement(self) -> None:
        args = argparse.Namespace(
            backfill=False, dry_run=False, mulligan=True,
            games=1, rounds=1, seed=7, force=False,
        )
        report = mock.Mock()
        report.as_dict.return_value = {}
        report.accepts = []
        fake_meta = mock.Mock()
        fake_meta.load.return_value = MetaDB([])
        with mock.patch.object(cli_learn, "_db", return_value=CardDB([])), \
             mock.patch.object(cli_learn, "MetaDB", fake_meta), \
             mock.patch.object(learn_facade, "optimize_mulligan", return_value=({}, report)), \
             mock.patch.object(learn_facade, "mirror_winrate", return_value=(0.45, 2)), \
             mock.patch.object(learn_facade, "load_weights", return_value={}), \
             mock.patch.object(learn_facade, "save_mulligan_weights") as save, \
             mock.patch("builtins.print"):
            rc = cli_learn.cmd_learn(args)
        self.assertEqual(rc, 0)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
