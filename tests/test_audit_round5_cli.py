"""审计第 5 轮 CLI/入口层复现测试：全部用 tempfile + mock 隔离，不碰真实 data/。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import cli as cli_mod
from hsai import cli_deck, cli_live, cli_meta, cli_sim, cli_tags
from hsai.meta import MetaDB, import_archetypes


class _Card:
    def __init__(self, cid: str) -> None:
        self.id = cid


class _FakeDB:
    """by_name_or_id 只认字符串：给非字符串就抛，用来证明调用方先挡了一层。"""

    def by_name_or_id(self, key):
        if not isinstance(key, str):
            raise AssertionError(f"非字符串不该进卡牌库：{type(key).__name__}")
        return _Card(key)


# ⑤ 备份目录不该跟着内存裁剪
class BackupPruneTest(unittest.TestCase):
    def test_backup_keeps_file_removed_from_meta(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hsai-r5-backup-") as d:
            meta_dir = Path(d) / "meta"
            backup_dir = Path(d) / "meta_backup"
            meta_dir.mkdir()
            (meta_dir / "deck_1.json").write_text("{}", encoding="utf-8")
            (meta_dir / "learned_a.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(cli_meta, "META_DIR", meta_dir), \
                 mock.patch.object(cli_meta, "META_BACKUP_DIR", backup_dir):
                cli_meta._backup_meta()
                self.assertTrue((backup_dir / "deck_1.json").is_file())
                self.assertTrue((backup_dir / "learned_a.json").is_file())
                # MetaDB.save() 会删掉 data/meta/ 里不在内存中的文件
                (meta_dir / "learned_a.json").unlink()
                cli_meta._backup_meta()
                self.assertTrue((backup_dir / "learned_a.json").is_file(),
                                "第二次 rebuild 把 learned_a 的唯一备份删了")


# ⑮ import / calibrate 的 --dry-run 不该写盘
class DryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r5-dry-")
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "archetypes.json"
        self.file.write_text(json.dumps({"archetypes": []}), encoding="utf-8")
        self.fake_meta = MetaDB([], None)
        self.save_patch = mock.patch.object(MetaDB, "save")
        self.save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        for p in (
            mock.patch.object(cli_meta, "_db", return_value=object()),
            mock.patch.object(MetaDB, "load", return_value=self.fake_meta),
            mock.patch.object(cli_meta, "import_archetypes", return_value=self.fake_meta),
            mock.patch.object(cli_meta.learn, "calibrate_priors", return_value={}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, action: str, dry_run: bool) -> int:
        args = argparse.Namespace(action=action, file=str(self.file), dry_run=dry_run)
        with contextlib.redirect_stdout(io.StringIO()):
            return cli_meta.cmd_meta(args)

    def test_import_dry_run_does_not_save(self) -> None:
        self.assertEqual(self._run("import", True), 0)
        self.save.assert_not_called()

    def test_import_without_dry_run_saves_once(self) -> None:
        self.assertEqual(self._run("import", False), 0)
        self.save.assert_called_once()

    def test_calibrate_dry_run_does_not_save(self) -> None:
        self.assertEqual(self._run("calibrate", True), 0)
        self.save.assert_not_called()

    def test_calibrate_without_dry_run_saves_once(self) -> None:
        self.assertEqual(self._run("calibrate", False), 0)
        self.save.assert_called_once()

    def test_dry_run_help_covers_all_actions(self) -> None:
        src = Path(cli_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("rebuild 时只演练", src)
        self.assertIn("import / rebuild / calibrate", src)


# ⑯ 合法 JSON 但结构错：中文提示 + 非 0，不裸 traceback
class BadImportPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r5-shape-")
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "payload.json"
        for p in (
            mock.patch.object(cli_meta, "_db", return_value=object()),
            mock.patch.object(MetaDB, "load", return_value=MetaDB([], None)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_bad_structures_do_not_raise(self) -> None:
        payloads = [
            [1, 2, 3],
            {"archetypes": [{"share": "N/A", "cards": {}}]},
            {"archetypes": [{"cards": ["x"]}]},
        ]
        for payload in payloads:
            self.file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = cli_meta.cmd_meta(argparse.Namespace(
                        action="import", file=str(self.file), dry_run=False))
            except Exception as exc:  # 旧代码在这里裸抛 AttributeError / ValueError
                self.fail(f"坏结构不该抛异常：{payload} -> {type(exc).__name__}: {exc}")
            self.assertNotEqual(rc, 0, payload)
            self.assertIn("卡组表", buf.getvalue(), payload)

    def test_import_archetypes_tolerates_bad_rows(self) -> None:
        payload = {"archetypes": [
            {"name": "坏 share", "share": "N/A", "cards": {"x": 1}},
            {"name": "坏 cards", "cards": ["x"]},
            {"name": "好", "share": 1.0, "cards": {"y": 2}},
        ]}
        fake = _FakeDB()
        try:
            db = import_archetypes(payload, fake, MetaDB([], fake))
        except Exception as exc:
            self.fail(f"import_archetypes 不该抛异常：{type(exc).__name__}: {exc}")
        self.assertEqual(len(db), 2)  # 两条坏 share/cards 降级，好那条照常导入


# ㉑ 卡组文件 BOM + cards 里混非字符串
class DeckFileRobustnessTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r5-deck-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.deck_path = self.root / "our_deck.json"
        for p in (
            mock.patch.object(cli_deck, "OUR_DECK_PATH", self.deck_path),
            mock.patch.object(cli_deck, "DATA_DIR", self.root),
            mock.patch.object(cli_deck, "_db", return_value=_FakeDB()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, p: Path) -> tuple[int, str]:
        args = argparse.Namespace(action="mine", code="", name="", file=str(p))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_deck.cmd_deck(args)
        return rc, buf.getvalue()

    def test_utf8_bom_file_parses(self) -> None:
        p = self.root / "bom.json"
        body = json.dumps({"name": "测试", "cards": ["火球术"]}, ensure_ascii=False)
        p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        rc, out = self._run(p)
        self.assertEqual(rc, 0, out)
        self.assertIn("已保存 1 张牌", out)
        self.assertEqual(
            json.loads(self.deck_path.read_text(encoding="utf-8"))["cards"], ["火球术"])

    def test_non_string_card_is_skipped(self) -> None:
        p = self.root / "mixed.json"
        p.write_text(json.dumps({"cards": ["火球术", {"oops": 1}]}, ensure_ascii=False),
                     encoding="utf-8")
        try:
            rc, out = self._run(p)
        except Exception as exc:  # 旧代码 dict 会在 card_id.upper() 抛 AttributeError
            self.fail(f"cards 里混非字符串不该崩：{type(exc).__name__}: {exc}")
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            json.loads(self.deck_path.read_text(encoding="utf-8"))["cards"], ["火球术"])


# ㉒ cmd_log 坏路径 / cmd_sim 非正局数
class ArgumentGuardTest(unittest.TestCase):
    def test_cmd_log_rejects_bad_path_without_spinning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hsai-r5-log-") as d:
            bad = Path(d) / "nope.log"
            buf = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(cli_live, "_db", return_value=object()))
                stack.enter_context(mock.patch.object(cli_live, "find_power_log", return_value=None))
                stack.enter_context(mock.patch.object(cli_live, "Advisor"))
                watcher = stack.enter_context(mock.patch.object(cli_live, "PowerLogWatcher"))
                stack.enter_context(mock.patch.object(cli_live, "_print_live"))
                # 旧代码会 while True 空转；用它打断循环，避免测试挂住
                stack.enter_context(mock.patch("time.sleep", side_effect=KeyboardInterrupt))
                with contextlib.redirect_stdout(buf):
                    rc = cli_live.cmd_log(argparse.Namespace(log=str(bad)))
            self.assertEqual(rc, 2, buf.getvalue())
            self.assertIn("不存在", buf.getvalue())
            watcher.assert_not_called()

    def _sim(self, games: int) -> tuple[int, str, object]:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli_sim, "_db", return_value=object()))
            stack.enter_context(mock.patch.object(MetaDB, "load", return_value=MetaDB([], None)))
            match = stack.enter_context(mock.patch.object(cli_sim.sim, "match"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_sim.cmd_sim(argparse.Namespace(
                    a="x", b="y", games=games, seed=1, trace=False, max_turns=40))
            return rc, buf.getvalue(), match

    def test_cmd_sim_rejects_non_positive_games(self) -> None:
        for games in (0, -3):
            rc, out, match = self._sim(games)
            self.assertEqual(rc, 2, f"games={games}: {out}")
            self.assertIn("局数必须是正整数", out, f"games={games}")
            match.assert_not_called()


# ㉓㉔ 死代码清理
class DeadCodeTest(unittest.TestCase):
    def test_cli_no_db_reexport(self) -> None:
        self.assertFalse(hasattr(cli_mod, "_db"))

    def test_cli_no_main_guard(self) -> None:
        src = Path(cli_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn('if __name__ == "__main__"', src)

    def test_metadb_no_to_json(self) -> None:
        self.assertFalse(hasattr(MetaDB, "to_json"))

    def test_no_unreachable_return_1(self) -> None:
        for mod, name in ((cli_deck, "cmd_deck"), (cli_meta, "cmd_meta"),
                          (cli_tags, "cmd_tags")):
            lines = [ln.strip() for ln in Path(mod.__file__).read_text(
                encoding="utf-8").splitlines()]
            self.assertNotIn("return 1", lines, f"{name} 仍留着不可达的 return 1")


if __name__ == "__main__":
    unittest.main()
