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


class LessonsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()) / "lessons.jsonl"
        self._orig = ai.LESSONS_PATH
        ai.LESSONS_PATH = self.tmp

    def tearDown(self) -> None:
        ai.LESSONS_PATH = self._orig

    def test_append_and_read_back(self) -> None:
        self.assertEqual(ai.append_lessons(["别铺满", "留解牌"]), 2)
        self.assertEqual(ai.recent_lessons(), ["别铺满", "留解牌"])

    def test_only_last_n(self) -> None:
        # 每次最多记 3 条（一局的上限），所以要写两次
        ai.append_lessons([f"教训{i}" for i in range(3)])
        ai.append_lessons([f"教训{i}" for i in range(3, 6)])
        self.assertEqual(ai.recent_lessons(3), ["教训3", "教训4", "教训5"])

    def test_empty_input_writes_nothing(self) -> None:
        self.assertEqual(ai.append_lessons([]), 0)
        self.assertEqual(ai.recent_lessons(), [])

    def test_prompt_contains_lessons(self) -> None:
        ai.append_lessons(["对面 6 费前别铺满"])
        prompt = ai.build_decision_prompt(
            {"turn": 5, "our": {}, "opp": {}}, ["打脸", "结束回合"]
        )
        self.assertIn("对面 6 费前别铺满", prompt)
        self.assertIn("1. 结束回合", prompt)


if __name__ == "__main__":
    unittest.main()
