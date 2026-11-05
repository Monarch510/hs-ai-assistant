"""收件箱协议的并发安全：抢不到、重复收尾、超时回收。

这几个行为是「多个 AI 并行消费同一批任务」的地基：
抢得不原子 → 两个 AI 改同一批文件；没有超时回收 → 一个崩掉的 worker 把任务卡死。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from scripts import inbox


class InboxClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "inbox"
        self.root.mkdir(parents=True)
        (self.root / "01-foo.md").write_text("# 01 foo\n做点事\n", encoding="utf-8")
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

    def test_second_claim_fails(self) -> None:
        self.assertEqual(self._run("claim", "01-foo", "--as", "worker-a"), 0)
        self.assertEqual(self._run("claim", "01-foo", "--as", "worker-b"), 3,
                         "第二个 worker 抢同一个任务必须失败，否则两边会改同一批文件")

    def test_release_returns_to_pool(self) -> None:
        self._run("claim", "01-foo", "--as", "worker-a")
        self.assertEqual(self._run("release", "01-foo", "--as", "worker-a"), 0)
        self.assertEqual(self._run("claim", "01-foo", "--as", "worker-b"), 0,
                         "释放之后别人应该能领")

    def test_release_by_other_worker_rejected(self) -> None:
        self._run("claim", "01-foo", "--as", "worker-a")
        self.assertEqual(self._run("release", "01-foo", "--as", "worker-b"), 3,
                         "不能释放别人领的任务")

    def test_finish_archives_and_writes_result(self) -> None:
        self._run("claim", "01-foo", "--as", "worker-a")
        self.assertEqual(self._run("finish", "01-foo", "--as", "worker-a",
                                   "--status", "ok", "--note", "改了 x.py"), 0)
        self.assertFalse((self.root / "01-foo.md").exists(), "原文件要移出收件箱")
        self.assertTrue((self.root / "archive" / "01-foo.md").exists())
        result = (self.root / "done" / "01-foo.md").read_text(encoding="utf-8")
        self.assertIn("状态：ok", result)
        self.assertIn("改了 x.py", result)
        self.assertFalse((self.root / ".claims" / "01-foo.json").exists(),
                         "收尾后凭据要清掉")

    def test_finish_requires_ownership(self) -> None:
        self._run("claim", "01-foo", "--as", "worker-a")
        self.assertEqual(self._run("finish", "01-foo", "--as", "worker-b"), 3)
        self.assertEqual(self._run("finish", "01-foo", "--as", "worker-b", "--force"), 0)

    def test_reap_only_expired(self) -> None:
        self._run("claim", "01-foo", "--as", "worker-a")
        claim = self.root / ".claims" / "01-foo.json"
        self.assertEqual(self._run("reap", "--ttl", "3600"), 0)
        self.assertTrue(claim.exists(), "没超时不该被回收")
        old = time.time() - 7200
        os.utime(claim, (old, old))
        self.assertEqual(self._run("reap", "--ttl", "3600"), 0)
        self.assertFalse(claim.exists(), "超时凭据要回收，否则任务永远卡在已领取")

    def test_name_with_suffix_works_end_to_end(self) -> None:
        """list 显示的名字带 .md，复制过去也要能收尾。

        曾经 claim 会把名字规范化成 stem、finish 却直接用参数查凭据，
        于是 claim 成功、finish 失败，任务卡在「已领」且原文件留在收件箱。
        """
        self.assertEqual(self._run("claim", "01-foo.md", "--as", "worker-a"), 0)
        self.assertEqual(self._run("finish", "01-foo.md", "--as", "worker-a",
                                   "--status", "ok", "--note", "带后缀收尾"), 0)
        self.assertFalse((self.root / "01-foo.md").exists())
        self.assertTrue((self.root / "archive" / "01-foo.md").exists())
        result = (self.root / "done" / "01-foo.md").read_text(encoding="utf-8")
        self.assertIn("带后缀收尾", result)
        self.assertFalse((self.root / ".claims" / "01-foo.json").exists())

    def test_stem_and_suffix_are_interchangeable(self) -> None:
        """不带后缀 claim、带后缀 release，凭据要认得出来。"""
        self.assertEqual(self._run("claim", "01-foo", "--as", "worker-a"), 0)
        self.assertEqual(self._run("release", "01-foo.md", "--as", "worker-a"), 0)
        self.assertEqual(self._run("claim", "01-foo.md", "--as", "worker-b"), 0)

    def test_hidden_and_underscore_files_ignored(self) -> None:
        (self.root / "_README.md").write_text("说明", encoding="utf-8")
        listed = []
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            self._run("list")
        listed = buf.getvalue()
        self.assertIn("01-foo.md", listed)
        self.assertNotIn("_README.md", listed, "下划线开头的是说明文件，不是任务")


if __name__ == "__main__":
    unittest.main()
