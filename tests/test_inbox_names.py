"""回归：从 `list` 复制来的名字（带 .md）也能 release / finish。

以前 claim 自己补后缀再取 stem，而 release/finish 直接把参数当 stem，
于是「从 list 复制文件名 → claim 成功 → finish 失败」，任务卡在 [已领]。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

# 目录名带连字符，不能 from auto-task import；直接按文件路径加载
_SPEC = importlib.util.spec_from_file_location(
    "inbox", Path(__file__).resolve().parents[1] / "auto-task" / "inbox.py")
inbox = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inbox)


class NameNormalizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "inbox"
        self.root.mkdir(parents=True)
        (self.root / "02-b.md").write_text("# 02\n", encoding="utf-8")
        self._orig = os.environ.get(inbox._ROOT_ENV)
        os.environ[inbox._ROOT_ENV] = str(self.root)

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop(inbox._ROOT_ENV, None)
        else:
            os.environ[inbox._ROOT_ENV] = self._orig
        self._tmp.cleanup()

    def _run(self, *argv: str) -> int:
        return inbox.main(list(argv))

    def test_finish_accepts_name_with_suffix(self) -> None:
        self.assertEqual(self._run("claim", "02-b.md", "--as", "w"), 0)
        self.assertEqual(
            self._run("finish", "02-b.md", "--as", "w", "--status", "ok", "--note", "x"), 0,
            "从 list 复制的名字带 .md，finish 必须认得",
        )
        self.assertFalse((self.root / "02-b.md").exists(), "收尾后原文件要移走")
        self.assertTrue((self.root / "done" / "02-b.md").exists())

    def test_release_accepts_name_with_suffix(self) -> None:
        self.assertEqual(self._run("claim", "02-b", "--as", "w"), 0)
        self.assertEqual(self._run("release", "02-b.md", "--as", "w"), 0)
        self.assertEqual(self._run("claim", "02-b.md", "--as", "other"), 0,
                         "释放后别人应该能领")

    def test_mixed_forms_are_same_task(self) -> None:
        self._run("claim", "02-b.md", "--as", "w")
        # 不带后缀来 finish 也要能对上同一份凭据
        self.assertEqual(self._run("finish", "02-b", "--as", "w"), 0)

    def test_other_worker_still_rejected(self) -> None:
        self._run("claim", "02-b.md", "--as", "w")
        self.assertEqual(self._run("finish", "02-b.md", "--as", "intruder"), 3,
                         "规范化不能把归属校验一起放宽")


if __name__ == "__main__":
    unittest.main()
