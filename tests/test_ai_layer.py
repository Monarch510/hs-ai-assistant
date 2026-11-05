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


if __name__ == "__main__":
    unittest.main()
