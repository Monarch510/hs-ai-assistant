"""「不赞同复盘」的落盘读取：只认最后一条，坏行和缺文件都要降级成空。

写盘在 ui._feedback_worker，这里只验读；测试全用临时文件，不碰 data/。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hsai.feedback import load_recent


class LoadRecentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hsai-feedback-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "advice_feedback.jsonl"

    def _write(self, *rows: object) -> None:
        self.path.write_text(
            "\n".join(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def test_returns_items_of_last_record(self) -> None:
        self._write(
            {"ts": "旧", "items": [{"lesson": "上一局的教训", "rule": "上一局的规则"}]},
            {"ts": "新", "items": [{"lesson": "A", "rule": "B"}]},
        )
        self.assertEqual(load_recent(path=self.path), [{"lesson": "A", "rule": "B"}])

    def test_filters_empty_lesson_and_honors_limit(self) -> None:
        self._write({"ts": "新", "items": [
            {"lesson": "", "rule": "没有教训"},
            {"lesson": "A", "rule": "B"},
            {"lesson": "C", "rule": "D"},
        ]})
        self.assertEqual(load_recent(limit=2, path=self.path),
                         [{"lesson": "A", "rule": "B"}, {"lesson": "C", "rule": "D"}])
        self.assertEqual(load_recent(limit=1, path=self.path), [{"lesson": "A", "rule": "B"}])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_recent(path=self.path), [])
        self._write("")
        self.assertEqual(load_recent(path=self.path), [])

    def test_bad_line_does_not_hide_last_valid_record(self) -> None:
        self._write(
            {"ts": "第一局", "items": [{"lesson": "A", "rule": "B"}]},
            '{"ts": "半截"',          # 写了一半就崩掉的行
        )
        self.assertEqual(load_recent(path=self.path), [{"lesson": "A", "rule": "B"}])


if __name__ == "__main__":
    unittest.main()
