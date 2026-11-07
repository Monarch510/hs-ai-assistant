"""AUDIT_REPORT「必须修」B 组（启动/降级/数据）复现用例。

缺陷 4/5/6/7/8：造最小输入，先复现修复前的崩溃或谎报成功，再锁住修复后的行为。
每个用例都对应规格里的一条「修复前会怎样」，跑测试时先红后绿才说明修对了。
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hsai import cards, logutil, logwatch, meta
from hsai.advisor import Advisor
from hsai.cli_setup_log import cmd_setup_log
from hsai.live import ReplaySource
from hsai.state import ZONE_PLAY, Entity, GameState

DB = cards.CardDB([])   # 离线空库：不触发卡牌下载、不写 data/ 缓存


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="hsai_audit_startup_"))


def _game_over_state() -> GameState:
    """结算帧：照抄 test_advice_gameover 的构造，能一路跑完 snapshot 到记录分支。"""
    st = GameState(DB)
    st.turn = 9
    st.turn_owner = 1
    st.phase = "game_over"
    st.game_started = True
    st.game_over = True
    st.winner = 1
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10, 2: 11}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[11] = Entity(11, tags={"PLAYER_ID": 2, "HERO_ENTITY": 21})
    st.entities[20] = Entity(20, card_id="HERO_10", controller=1, zone=ZONE_PLAY,
                             tags={"HEALTH": 30})
    st.entities[21] = Entity(21, card_id="HERO_10", controller=2, zone=ZONE_PLAY,
                             tags={"HEALTH": 2})
    return st


class Defect4ReplayNonUtf8(unittest.TestCase):
    def test_replay_with_invalid_utf8_does_not_raise(self) -> None:
        p = _tmp_dir() / "bad.log"
        p.write_bytes(b"first line\n\xff bad bytes\nlast line\n")
        # 修复前 read_text 不带 errors，0xff 直接抛 UnicodeDecodeError
        src = ReplaySource(p, DB)
        self.assertEqual(len(src.lines), 3)
        self.assertIn("last line", src.lines)


class Defect5SetupLogFailure(unittest.TestCase):
    def _run(self, results: list[tuple[Path, str]]) -> tuple[int, str]:
        args = argparse.Namespace(auto=True, dry_run=False, dir=None)
        buf = io.StringIO()
        with mock.patch.object(logwatch, "log_config_targets", return_value=[Path("p")]), \
                mock.patch.object(logwatch, "setup_log_config", return_value=results), \
                mock.patch.object(logwatch, "find_power_log", return_value=None), \
                redirect_stdout(buf):
            rc = cmd_setup_log(args)
        return rc, buf.getvalue()

    def test_write_failure_is_not_reported_as_enabled(self) -> None:
        # 修复前 changed 为空就走快速路径，谎报「已开启」并 return 0
        _, out = self._run([(Path("p"), "写入失败：权限不足")])
        self.assertNotIn("日志开关：已开启", out)
        self.assertIn("写入失败", out, "要落到逐条打印，把失败原因摆出来")

    def test_normal_create_still_returns_zero(self) -> None:
        # 收紧条件不能破坏正常创建分支的返回码
        rc, out = self._run([(Path("p"), "已创建")])
        self.assertEqual(rc, 0)
        self.assertIn("生效方式", out)


class Defect6TailStartOffset(unittest.TestCase):
    def test_no_create_game_returns_file_size(self) -> None:
        p = _tmp_dir() / "no_anchor.log"
        p.write_bytes(b"D 00:00:00.0 some line\n" * 3)
        # 修复前找不到锚点返回 0，等于从头重放好几局旧对局
        self.assertEqual(logwatch.tail_start_offset(p), p.stat().st_size)


class Defect7LogErrImport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        patcher = mock.patch.object(logutil, "LOG_FILE", self.tmp / "hsai.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_finish_game_failure_does_not_break_snapshot(self) -> None:
        adv = Advisor(DB, meta_path=self.tmp / "meta")
        adv._record_result = lambda *a, **k: None  # type: ignore[method-assign]
        st = _game_over_state()
        with mock.patch.object(adv, "_finish_game", side_effect=RuntimeError("boom")):
            # 修复前 _log_err 引用未导入的 logutil，NameError 冒出 snapshot
            adv.snapshot(st)
        self.assertTrue(adv._recorded, "记录开关必须可达，否则每帧重复入库")
        self.assertIn("finish_game 失败",
                      (self.tmp / "hsai.log").read_text(encoding="utf-8"))


class Defect8BadMetaFields(unittest.TestCase):
    def test_bad_numeric_fields_are_kept_and_do_not_raise(self) -> None:
        d = _tmp_dir() / "meta"
        d.mkdir()
        # 一个坏字段一个文件，免得前一个字段先把整份判失败、测不到后一个
        bad = {
            "prior.json": {"prior": "高"},
            "count.json": {"cards": [{"id": "X", "count": "x"}]},
            "prior_base.json": {"prior_base": []},
        }
        for name, extra in bad.items():
            payload = {"id": name, "name": "坏", "class": "WARRIOR",
                       "prior": 1.0, "cards": []}
            payload.update(extra)
            (d / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 修复前 float/int 在 try 块外抛穿，load 与 save 都会崩
        loaded = meta.MetaDB.load(DB, d)
        self.assertEqual(len(loaded), 0)
        meta.MetaDB([], DB).save(d)
        for name in bad:
            self.assertTrue((d / name).exists(), f"{name} 不该被当成已删除原型清掉")


if __name__ == "__main__":
    unittest.main()
