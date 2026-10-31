from __future__ import annotations

import unittest

from hsai import ai_mulligan


class ParseMulliganTest(unittest.TestCase):
    def test_valid(self) -> None:
        rows = ai_mulligan.parse_mulligan('[{"name":"寒冰箭","keep":true,"reason":"低费解场"}]')
        self.assertEqual(rows, [{"name": "寒冰箭", "keep": True, "reason": "低费解场"}])

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(ai_mulligan.parse_mulligan("都留着吧"), [])

    def test_prompt_contains_hand(self) -> None:
        snap = {"our": {"class_cn": "术士", "hand": [{"name": "火球术", "cost": 4}]},
                "opp": {"class_cn": "死亡骑士"}, "deck": {"name": "快攻术"}}
        prompt = ai_mulligan.build_mulligan_prompt(snap)
        self.assertIn("火球术(4费)", prompt)
        self.assertIn("死亡骑士", prompt)


if __name__ == "__main__":
    unittest.main()
