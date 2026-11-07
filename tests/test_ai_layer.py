"""AI 层里不依赖网络的部分：决策解析、教训存储、prompt 组装。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hsai import ai


class DecisionParseTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(
            ai.parse_decision('{"pick": 2, "reason": "解场"}', 3),
            {"pick": 2, "reason": "解场"},
        )

    def test_out_of_range_falls_back(self) -> None:
        self.assertIsNone(ai.parse_decision('{"pick": 9}', 3))

    def test_garbage_falls_back(self) -> None:
        self.assertIsNone(ai.parse_decision("我觉得应该打脸", 3))

    def test_empty(self) -> None:
        self.assertIsNone(ai.parse_decision("", 3))


class JsonArrayTest(unittest.TestCase):
    """模型回复的三种常见形态：裸数组、包在 ``` 里、前后带解释。"""

    def test_plain_array(self) -> None:
        self.assertEqual(ai.json_array('[1, 2]'), [1, 2])

    def test_fenced_block(self) -> None:
        self.assertEqual(ai.json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])

    def test_surrounded_by_prose(self) -> None:
        self.assertEqual(ai.json_array('结果如下：\n["x"]\n以上。'), ["x"])

    def test_invalid_returns_empty(self) -> None:
        self.assertEqual(ai.json_array("[不是 JSON]"), [])

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(ai.json_array(""), [])
        self.assertEqual(ai.json_array(None), [])  # type: ignore[arg-type]


class LessonsTest(unittest.TestCase):
    def setUp(self) -> None:
        # lesson 库现在只由 memory 管（ai 里那套读写函数已删）
        from hsai import memory

        self.tmp = Path(tempfile.mkdtemp()) / "lessons.jsonl"
        self._orig = memory.LESSONS_PATH
        memory.LESSONS_PATH = self.tmp

    def tearDown(self) -> None:
        from hsai import memory

        memory.LESSONS_PATH = self._orig

    def test_prompt_contains_matched_lessons(self) -> None:
        from hsai import memory

        memory.LESSONS_PATH = self.tmp
        memory.add_lessons(
            ["对面 6 费前别铺满"],
            {"our_class": "SHAMAN", "opp_class": "DEATHKNIGHT", "archetype": "掘骨恶鬼"},
        )
        prompt = ai.build_decision_prompt(
            {"turn": 5,
             "our": {"class_en": "SHAMAN"},
             "opp": {"class_en": "DEATHKNIGHT"},
             "prediction": {"name": "掘骨恶鬼"}},
            ["打脸", "结束回合"],
        )
        self.assertIn("对面 6 费前别铺满", prompt)
        self.assertIn("1. 结束回合", prompt)

    def test_unrelated_lesson_is_not_injected(self) -> None:
        from hsai import memory

        memory.LESSONS_PATH = self.tmp
        memory.add_lessons(["对控制留解牌"], {"opp_class": "PRIEST"})
        prompt = ai.build_decision_prompt(
            {"turn": 5, "our": {}, "opp": {"class_en": "ROGUE"}}, ["结束回合"]
        )
        self.assertNotIn("对控制留解牌", prompt)


class ReviewParseTest(unittest.TestCase):
    """局末复盘的结构化输出：教训和参数建议必须分开，且参数名不能是模型编的。"""

    def test_lesson_and_param_split(self) -> None:
        lessons, tunings = ai.parse_review(
            '[{"lesson": "对快攻先解场"}, '
            '{"param": "tempo", "delta": 0.2, "why": "场面落后"}]'
        )
        self.assertEqual(lessons, ["对快攻先解场"])
        self.assertEqual(tunings, [
            {"kind": "param", "param": "tempo", "delta": 0.2, "why": "场面落后"}
        ])

    def test_bare_string_still_accepted(self) -> None:
        """模型按旧格式吐裸字符串时别整局复盘都丢掉。"""
        lessons, tunings = ai.parse_review('["对控制留解牌"]')
        self.assertEqual(lessons, ["对控制留解牌"])
        self.assertEqual(tunings, [])

    def test_unknown_param_is_dropped(self) -> None:
        lessons, tunings = ai.parse_review(
            '[{"param": "aggression", "delta": 5, "why": "编的参数"}]'
        )
        self.assertEqual((lessons, tunings), ([], []))

    def test_delta_is_clamped(self) -> None:
        _, tunings = ai.parse_review('[{"param": "face", "delta": 99, "why": "越界"}]')
        self.assertEqual(tunings[0]["delta"], 0.3)

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(ai.parse_review("不是 JSON"), ([], []))

    def test_fabricated_lethal_is_filtered(self) -> None:
        """没漏斩却说「3 点斩杀」是脑补，不该进教训。"""
        lessons, _ = ai.parse_review('["第 5 回合有 3 点斩杀"]', missed_lethal=False)
        self.assertEqual(lessons, [])


class TuningsTest(unittest.TestCase):
    def setUp(self) -> None:
        from hsai import memory

        self.tmp = Path(tempfile.mkdtemp()) / "advice_tuning.jsonl"
        self._orig = memory.ADVICE_TUNING_PATH
        memory.ADVICE_TUNING_PATH = self.tmp

    def tearDown(self) -> None:
        from hsai import memory

        memory.ADVICE_TUNING_PATH = self._orig

    def test_below_threshold_is_not_reported(self) -> None:
        from hsai import memory

        memory.add_tunings(
            [{"kind": "param", "param": "tempo", "delta": 0.1, "why": "a"}],
            {"opp_class": "MAGE"},
        )
        self.assertEqual(memory.summarize_tunings(), {})

    def test_consistent_direction_is_summarized(self) -> None:
        from hsai import memory

        memory.add_tunings(
            [{"kind": "param", "param": "tempo", "delta": d, "why": "a"}
             for d in (0.1, 0.2, 0.15)],
            {"opp_class": "MAGE"},
        )
        row = memory.summarize_tunings()["tempo"]
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["direction"], "up")
        self.assertEqual(row["agree"], 1.0)

    def test_mixed_direction_is_flagged(self) -> None:
        from hsai import memory

        memory.add_tunings(
            [{"kind": "param", "param": "face", "delta": d, "why": "a"}
             for d in (0.2, -0.2, 0.1, -0.1)],
            {},
        )
        row = memory.summarize_tunings()["face"]
        self.assertEqual(row["direction"], "mixed")

    def _seed(self, *rows: tuple[str, float]) -> None:
        from hsai import memory

        memory.add_tunings(
            [{"kind": "param", "param": p, "delta": d, "why": "a"} for p, d in rows],
            {},
        )

    def test_read_only_does_not_write_weights(self) -> None:
        """不带 --apply 时，哪怕建议全达标也一个文件都不能碰。"""
        from unittest import mock
        from argparse import Namespace
        from hsai import cli, cli_stats

        self._seed(*[("tempo", 0.1)] * 3)
        with tempfile.TemporaryDirectory() as d:
            pw, tw = Path(d) / "p.json", Path(d) / "t.json"
            with mock.patch.object(cli_stats, "POLICY_WEIGHTS_JSON", pw), \
                 mock.patch.object(cli_stats, "TAG_WEIGHTS_JSON", tw):
                cli.cmd_tunings(Namespace(min_count=3, apply=False))
            self.assertFalse(pw.exists())
            self.assertFalse(tw.exists())

    def test_mixed_direction_is_not_applied(self) -> None:
        """方向摇摆的参数即使达标也不写。"""
        from unittest import mock
        from argparse import Namespace
        from hsai import cli, cli_stats

        self._seed(("face", 0.2), ("face", -0.2), ("face", 0.1), ("face", -0.1))
        with tempfile.TemporaryDirectory() as d:
            pw, tw = Path(d) / "p.json", Path(d) / "t.json"
            with mock.patch.object(cli_stats, "POLICY_WEIGHTS_JSON", pw), \
                 mock.patch.object(cli_stats, "TAG_WEIGHTS_JSON", tw):
                cli.cmd_tunings(Namespace(min_count=3, apply=True))
            self.assertFalse(pw.exists())
            self.assertFalse(tw.exists())

    def test_apply_keeps_user_keys_and_backs_up(self) -> None:
        """--apply 只改目标键，保留用户手写的其它键，并备份原文件。"""
        import json
        from unittest import mock
        from argparse import Namespace
        from hsai import cli, cli_stats

        self._seed(*[("tempo", 0.1)] * 3)
        with tempfile.TemporaryDirectory() as d:
            pw, tw = Path(d) / "p.json", Path(d) / "t.json"
            pw.write_text(json.dumps({"weights": {"tempo": 1.0, "face": 2.0},
                                      "note": "用户手写"}), encoding="utf-8")
            with mock.patch.object(cli_stats, "POLICY_WEIGHTS_JSON", pw), \
                 mock.patch.object(cli_stats, "TAG_WEIGHTS_JSON", tw), \
                 mock.patch.object(cli_stats.learn, "load_weights",
                                   return_value={"tempo": 1.0, "face": 2.0}):
                cli.cmd_tunings(Namespace(min_count=3, apply=True))
            data = json.loads(pw.read_text(encoding="utf-8"))
            self.assertEqual(data["weights"]["face"], 2.0)   # 没被冲掉
            self.assertGreater(data["weights"]["tempo"], 1.0)  # 被调高
            self.assertEqual(data["note"], "用户手写")
            self.assertTrue(pw.with_suffix(".json.bak").exists())


if __name__ == "__main__":
    unittest.main()
