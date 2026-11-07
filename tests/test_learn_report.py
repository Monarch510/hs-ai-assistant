"""calibrate / rebuild 的报告要说清楚「读了多少、剔了哪些、改了什么」。

以前 `meta calibrate` 只打印「修正 0 个原型」—— schema 1 的老数据静默错过
一个月都没人发现。这里锁住报告的口径，同时确认校准算法本身没被打印改动带偏。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import cli, learn, meta

class CalibrateReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = cards_mod.load()
        pool = [c.id for c in self.db.standard_pool("WARRIOR")
                if c.name and not self.db.is_token(c.id)]
        self.a_ids, self.b_ids = pool[:3], pool[3:6]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "obs.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _meta(self) -> meta.MetaDB:
        # 两个原型：单原型时 normalize_priors() 会把分数洗成 1.0，看不出前后变化
        return meta.MetaDB([
            meta.Archetype(id="a", name="战士·甲", card_class="WARRIOR", prior=0.5,
                           cards={c: 2 for c in self.a_ids}),
            meta.Archetype(id="b", name="战士·乙", card_class="WARRIOR", prior=0.5,
                           cards={c: 2 for c in self.b_ids}),
        ])

    def _write(self, rows: list[dict]) -> Path:
        self.path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        return self.path

    def _trusted(self, n: int) -> list[dict]:
        return [{"opp_class": "WARRIOR", "revealed": self.a_ids} for _ in range(n)]

    def test_sample_counts_and_exclusions(self) -> None:
        rows = self._trusted(4)
        rows += [{"revealed": self.a_ids}]                                     # 无职业
        rows += [{"opp_class": "WARRIOR",                                      # 真值不可信
                  "truth": {"arch": "a", "trusted": False}}]
        report = learn.calibrate_priors(self._meta(), self.db, self._write(rows))
        self.assertEqual(report["total_rows"], 6)
        self.assertEqual(report["rows"], 6)
        self.assertEqual(report["trusted"], 4)
        self.assertEqual(report["no_class"], 1)
        self.assertEqual(report["untrusted"], 1)
        self.assertEqual(report["updated"], [])          # 4 < 5，本职业不校准

    def test_bad_lines_count_in_total_rows_only(self) -> None:
        path = self._write(self._trusted(4))
        with path.open("a", encoding="utf-8") as f:
            f.write("{坏行\n")
        report = learn.calibrate_priors(self._meta(), self.db, path)
        self.assertEqual(report["total_rows"], 5)        # 坏行也算「读过」
        self.assertEqual(report["rows"], 4)              # 但没进统计

    def test_low_samples_are_listed_as_skipped(self) -> None:
        report = learn.calibrate_priors(self._meta(), self.db, self._write(self._trusted(3)))
        self.assertEqual(report["updated"], [])
        self.assertEqual(len(report["skipped"]), 2)
        for item in report["skipped"]:
            self.assertEqual(item["samples"], 3)
            self.assertIn("< 5", item["reason"])

    def test_before_and_after_match_the_meta(self) -> None:
        m = self._meta()
        before = {a.id: a.prior for a in m}
        report = learn.calibrate_priors(m, self.db, self._write(self._trusted(6)))
        self.assertEqual(len(report["updated"]), 2)
        for item in report["updated"]:
            self.assertEqual(item["before"], before[item["id"]])
            self.assertEqual(item["after"], m.get(item["id"]).prior)
        changed = [i for i in report["updated"] if i["after"] != i["before"]]
        self.assertTrue(changed, "校准应当真的改动先验")

    def test_repeated_report_starts_from_prior_base(self) -> None:
        """第二次的 before 必须等于第一次的 after，且结果不再漂移。"""
        m = self._meta()
        path = self._write(self._trusted(6))
        first = learn.calibrate_priors(m, self.db, path)
        second = learn.calibrate_priors(m, self.db, path)
        for a, b in zip(first["updated"], second["updated"]):
            self.assertAlmostEqual(b["before"], a["after"], places=6)
            self.assertAlmostEqual(b["after"], a["after"], places=6)

    def test_empty_observations(self) -> None:
        for path in (self.tmp / "missing.jsonl", self._write([])):
            with self.subTest(path=path.name):
                report = learn.calibrate_priors(self._meta(), self.db, path)
                self.assertEqual(report["rows"], 0)
                self.assertEqual(report["total_rows"], 0)
                self.assertEqual(report["updated"], [])
                self.assertEqual(report["skipped"], [])

    def test_cli_print_says_why_nothing_changed(self) -> None:
        report = {
            "rows": 6, "total_rows": 6, "trusted": 6, "no_class": 0, "untrusted": 0,
            "updated": [{"id": "a", "name": "战士·甲", "class": "WARRIOR",
                         "before": 0.5, "after": 0.65, "samples": 6, "weight": 0.3}],
            "skipped": [{"id": "b", "name": "战士·乙", "class": "WARRIOR",
                         "samples": 3, "reason": "本职业可信样本 3 < 5"}],
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_calibrate_report(report)
        out = buf.getvalue()
        for word in ("可信", "跳过", "->", "没有对局记录"):
            self.assertIn(word, out)
        self.assertIn("战士·甲", out)
        self.assertIn("0.50 -> 0.65", out)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_calibrate_report({"rows": 0, "total_rows": 0, "trusted": 0,
                                        "no_class": 0, "untrusted": 0,
                                        "updated": [], "skipped": []})
        self.assertIn("没有对局记录，先验不变", buf.getvalue())

    def test_rebuild_report_counts_observations(self) -> None:
        # 空原型库：已有原型重合度 >= 0.6 时 absorb 会跳过，那会让 added 变成 0
        m = meta.MetaDB([])
        path = self._write(self._trusted(3))
        report = learn.rebuild_learned(self.db, m, min_games=2, observations_path=path)
        self.assertEqual(report["observations"], 3)
        self.assertEqual(report["added"], 1)

if __name__ == "__main__":
    unittest.main()
