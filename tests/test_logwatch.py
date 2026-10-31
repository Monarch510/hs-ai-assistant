"""日志位置发现 + log.config 写入。

这一块是为真实环境写的：炉石的日志目录不止一个位置，而且会按会话分子目录。
测试用临时目录模拟三种布局，确保 find_power_log 都能认出来。

注意：测试一律传 include_ambient=False，否则会读到本机真实安装目录，
结果依赖「跑测试的这台机器装没装炉石」——那种测试没有意义。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from hsai import logwatch


def _session_log(root: Path, name: str) -> Path:
    logs = root / "Logs" / name
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "Power.log"
    p.write_text("D 00:00:00.0 GameState.DebugPrintPower() - CREATE_GAME\n", encoding="utf-8")
    return p


def _only_this(p: Path) -> Path | None:
    return logwatch.find_power_log([p], include_ambient=False)


class FindLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsai_logs_"))

    def test_flat_layout(self) -> None:
        logs = self.tmp / "Logs"
        logs.mkdir()
        (logs / "Power.log").write_text("D 00:00:00.0 GameState.DebugPrintPower() - CREATE_GAME\n",
                                        encoding="utf-8")
        found = _only_this(self.tmp)
        self.assertIsNotNone(found)
        self.assertEqual(found, logs / "Power.log")

    def test_session_subdir_layout(self) -> None:
        """实测遇到过的布局：Logs\\Hearthstone_2026_10_31_12_09_15\\Power.log"""
        old = _session_log(self.tmp, "Hearthstone_2026_10_30_20_37_45")
        new = _session_log(self.tmp, "Hearthstone_2026_10_31_12_09_15")
        old_time = time.time() - 3600
        os.utime(old, (old_time, old_time))
        self.assertEqual(_only_this(self.tmp), new, "应该选最新的那一份")

    def test_nothing_found(self) -> None:
        self.assertIsNone(_only_this(self.tmp))

    def test_root_level_power_log(self) -> None:
        p = self.tmp / "Power.log"
        p.write_text("", encoding="utf-8")
        self.assertEqual(_only_this(self.tmp), p)

    def test_numbered_power_log(self) -> None:
        p = self.tmp / "Logs" / "Power_1.log"
        p.parent.mkdir(parents=True)
        p.write_text("x", encoding="utf-8")
        self.assertEqual(_only_this(self.tmp), p)


class SetupLogConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsai_hs_"))
        (self.tmp / "Hearthstone.exe").write_text("", encoding="utf-8")

    def _setup(self, **kw):
        kw.setdefault("include_ambient", False)
        return logwatch.setup_log_config([self.tmp], **kw)

    def test_creates_config(self) -> None:
        results = self._setup()
        target = self.tmp / "log.config"
        self.assertTrue(target.exists())
        text = target.read_text(encoding="utf-8")
        self.assertIn("[Power]", text)
        self.assertIn("LogLevel=1", text)
        self.assertTrue(any("已创建" in msg for _, msg in results))

    def test_idempotent(self) -> None:
        self._setup()
        results = self._setup()
        self.assertTrue(any("已开启" in msg for _, msg in results), results)

    def test_backs_up_foreign_config(self) -> None:
        target = self.tmp / "log.config"
        target.write_text("[Other]\nLogLevel=2\n", encoding="utf-8")
        results = self._setup()
        self.assertTrue((self.tmp / "log.config.bak").exists(), "覆盖前必须备份")
        self.assertIn("[Power]", target.read_text(encoding="utf-8"))
        self.assertTrue(any("备份" in msg for _, msg in results))

    def test_dry_run_writes_nothing(self) -> None:
        self._setup(dry_run=True)
        self.assertFalse((self.tmp / "log.config").exists())

    def test_ignores_non_hearthstone_dir(self) -> None:
        empty = Path(tempfile.mkdtemp(prefix="hsai_empty_"))
        targets = logwatch.log_config_targets([empty], include_ambient=False)
        self.assertNotIn(empty, targets)
        self.assertEqual(targets, [])

    def test_targets_require_exe_or_data_dir(self) -> None:
        no_exe = Path(tempfile.mkdtemp(prefix="hsai_noexe_"))
        (no_exe / "Hearthstone_Data").mkdir()
        self.assertIn(no_exe, logwatch.log_config_targets([no_exe], include_ambient=False))


if __name__ == "__main__":
    unittest.main()
