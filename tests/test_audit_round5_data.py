"""第 5 轮审查「数据 / JSONL / AI / 权重层」修复的回归护栏。

只依赖标准库；落盘全用 tempfile / mock 改道，绝不碰真实 data/。
对应审查项：6 脏 revealed、7 回填坏字节、18 坏字节行不吞整份、
19 Unicode 行边界、20 AI 解析边角、26 数值字段反射、14 min_count 下界。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import ai, cli_stats, effects_ai, feedback, learn_calibrate, memory, review
from hsai.cards import Card, CardDB
from hsai.infer import DeckPredictor
from hsai.meta import Archetype, MetaDB

SEP = chr(0x2028)


def _predictor() -> DeckPredictor:
    cards = CardDB([
        Card(id="M1", name="火球", card_class="MAGE", type="SPELL"),
        Card(id="M2", name="寒冰箭", card_class="MAGE", type="SPELL"),
        Card(id="GAME_005", name="幸运币", collectible=False),
    ])
    arch = Archetype(id="a", name="法师·甲", card_class="MAGE", prior=1.0,
                     cards={"M1": 2, "M2": 2})
    return DeckPredictor(MetaDB([arch], cards), cards)


# 6：脏 revealed 不能让 retrospect 抛异常，也不能让整局学习断掉
class DirtyRevealedTest(unittest.TestCase):
    def test_dirty_items_do_not_raise(self) -> None:
        p = _predictor()
        for bad in ([[5, 1]], [[]], [{"x": 1}], [None]):
            with self.subTest(bad=bad):
                t = p.retrospect("MAGE", bad)
                self.assertIsNotNone(t)

    def test_schema1_and_schema2_still_count(self) -> None:
        p = _predictor()
        t = p.retrospect("MAGE", ["M1", "M2", "GAME_005"])
        self.assertEqual(t.archetype_id, "a")
        self.assertAlmostEqual(t.coverage, 1.0)
        t2 = p.retrospect("MAGE", [["M1", 2], ["M2", 3], ["GAME_005", 4]])
        self.assertEqual(t2.archetype_id, "a")
        self.assertAlmostEqual(t2.coverage, 1.0)


# 7：backfill_truth 坏字节行不崩、null 行原样保留、两条好行都处理
class BackfillTruthResilienceTest(unittest.TestCase):
    def test_bad_bytes_and_null_row(self) -> None:
        good1 = json.dumps({"opp_class": "MAGE", "revealed": []}, ensure_ascii=False)
        good2 = json.dumps({"opp_class": "MAGE", "revealed": []}, ensure_ascii=False)
        raw = (good1.encode("utf-8") + b"\n" + b"\xff\xfe bad utf8\n"
               + b"null\n" + good2.encode("utf-8") + b"\n")
        cards = CardDB([])
        meta = MetaDB([])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "obs.jsonl"
            path.write_bytes(raw)
            rep = learn_calibrate.backfill_truth(cards, meta, observations_path=path)
            self.assertEqual(rep["total"], 2)
            out_lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
            self.assertIn("null", out_lines)
            self.assertIn("\ufffd", "\n".join(out_lines))
            good_rows = [json.loads(x) for x in out_lines
                         if x.strip().startswith("{")]
            self.assertEqual(len(good_rows), 2)


# 18：好+坏+好 → 两条好行都还在（坏字节不再吞整份）
class BadByteLineIsolationTest(unittest.TestCase):
    def _path(self, d: str) -> Path:
        path = Path(d) / "x.jsonl"
        g = json.dumps({"a": 1}, ensure_ascii=False)
        raw = (g.encode("utf-8") + b"\n" + b"\xff\xfe bad\n"
               + json.dumps({"b": 2}, ensure_ascii=False).encode("utf-8") + b"\n")
        path.write_bytes(raw)
        return path

    def test_memory_read_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rows = memory._read_jsonl(self._path(d))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].get("a"), 1)
            self.assertEqual(rows[1].get("b"), 2)

    def test_feedback_load_recent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            good = json.dumps({"items": [{"lesson": "甲", "rule": "乙"}]},
                              ensure_ascii=False)
            raw = (good.encode("utf-8") + b"\n" + b"\xff\xfe bad\n"
                   + good.encode("utf-8") + b"\n")
            path.write_bytes(raw)
            out = feedback.load_recent(limit=3, path=path)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["lesson"], "甲")

    def test_append_truncation_keeps_broken_line(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            path.write_text('{"a":1}\n{"b":2}\n{BROKEN hand-edited line\n', encoding="utf-8")
            memory._append_jsonl(path, [{"d": 4}], cap=2)
            content = path.read_text(encoding="utf-8")
            self.assertIn("{BROKEN hand-edited line", content)
            self.assertIn('"d": 4', content)
            self.assertNotIn('"a":1', content)


# 19：含 U+2028 的合法 JSON 整条读回（splitlines 会把它切成两段）
class UnicodeLineSeparatorTest(unittest.TestCase):
    def _line(self) -> bytes:
        return (json.dumps({"note": "前" + SEP + "后"}, ensure_ascii=False) + "\n").encode("utf-8")

    def test_memory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            path.write_bytes(self._line())
            rows = memory._read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["note"], "前" + SEP + "后")

    def test_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            payload = {"items": [{"lesson": "前" + SEP + "后", "rule": "乙"}]}
            path.write_bytes((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            out = feedback.load_recent(limit=3, path=path)
            self.assertEqual(len(out), 1)
            self.assertIn(SEP, out[0]["lesson"])

    def test_review(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.jsonl"
            path.write_bytes(self._line())
            rows = review.load_games(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["note"], "前" + SEP + "后")


# 20：AI 解析三处边角
class AiParseEdgeTest(unittest.TestCase):
    def test_json_array_prose_bracket_retry(self) -> None:
        out = ai.json_array('[说明] 结果：\n[{"a": 1}]\n以上')
        self.assertEqual(out, [{"a": 1}])

    def test_parse_decision_bool_pick(self) -> None:
        self.assertIsNone(ai.parse_decision('{"pick": true, "reason": "x"}', 3))
        self.assertIsNone(ai.parse_decision('{"pick": false, "reason": "x"}', 3))

    def test_parse_review_small_delta_keeps_lesson(self) -> None:
        text = json.dumps([{"param": "tempo", "delta": 0.01, "lesson": "前期别贪脸"}],
                          ensure_ascii=False)
        lessons, tunings = ai.parse_review(text)
        self.assertEqual(tunings, [])
        self.assertIn("前期别贪脸", lessons)


# 26：闸门④ 要校验 Effect 反射出的全部 int 字段
class NumericFieldsReflectionTest(unittest.TestCase):
    def _cards(self) -> CardDB:
        return CardDB([
            Card(id="A", name="甲", type="SPELL", text="造成2点伤害，抽一张牌"),
        ])

    def _rule(self, field: str, value) -> dict:
        return {"regex": "造成(\\d+)点伤害", "effect": {field: value}}

    def test_four_missing_fields_reject_string(self) -> None:
        cards = self._cards()
        for field in ("discover", "opp_hand_delta", "pending_buff", "set_hp"):
            with self.subTest(field=field):
                with mock.patch.object(effects_ai, "EFFECTS_PATH", Path("no-such-file")):
                    ok, _ = effects_ai.validate_rule(cards, self._rule(field, "二"), ["A"])
                self.assertFalse(ok)

    def test_damage_numeric_passes(self) -> None:
        cards = self._cards()
        with mock.patch.object(effects_ai, "EFFECTS_PATH", Path("no-such-file")):
            ok, _ = effects_ai.validate_rule(cards, self._rule("damage", 2), ["A"])
        self.assertTrue(ok)


# 14：--min-count 下界钳到 3；无 --apply 不写盘
class TuningMinCountTest(unittest.TestCase):
    def test_min_count_clamped_to_three(self) -> None:
        args = argparse.Namespace(min_count=1, apply=False)
        with mock.patch.object(memory, "summarize_tunings", return_value={}) as m, \
             mock.patch("builtins.print"):
            cli_stats.cmd_tunings(args)
        self.assertEqual(m.call_args.kwargs["min_count"], 3)

    def test_no_apply_does_not_write(self) -> None:
        args = argparse.Namespace(min_count=3, apply=False)
        with mock.patch.object(cli_stats.memory, "summarize_tunings",
                               return_value={"tempo": {"count": 3, "mean": 0.1,
                                                       "direction": "up", "agree": 1.0}}), \
             mock.patch.object(cli_stats, "_apply_one") as apply_one, \
             mock.patch("builtins.print"):
            cli_stats.cmd_tunings(args)
        apply_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
